"""One match + timeline -> one flat row per CHAMPION_KILL.

This is the collapse: ~2 MB of timeline JSON becomes ~28 rows of 54 bytes.
Everything a filter can ask about is a scalar column computed exactly once,
here, offline, against immutable inputs.

The row is SYMMETRIC. Every actor-scoped feature exists twice -- once for the
victim, once for the killer -- because the Kills layer is not a second-class
view of the Deaths layer, and the Danger ratio D/(D+K) is only a ratio of
comparable things if both sides are filterable by the same predicates.

Pure stdlib. Runs per event, so numpy would only add import latency.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import (CAUSE_CHAMPION, CAUSE_MINION, CAUSE_MONSTER, CAUSE_TURRET,
                      CAUSE_UNKNOWN, F_FIRST_BLOOD, F_POSITION_IMPUTED, F_TRADE,
                      F_UNDER_TURRET, F_VICTIM_TEAM_RED, F_VICTIM_WON,
                      ISOLATION_BUCKET, NEARBY_RADIUS, NULL_I16, NULL_U8,
                      NULL_U16, OBJ_BARON, OBJ_DRAGON, OBJ_ELDER, OBJ_HERALD,
                      ROLE_TO_SK, ROLE_UNKNOWN, SINCE_DEATH_STEP,
                      SINCE_OBJECTIVE_STEP, TRADE_WINDOW_S, respawn_seconds)
from .geometry import distance, under_turret
from .regions import region_sk

CHAMPION_KILL = "CHAMPION_KILL"
LEVEL_UP = "LEVEL_UP"
ELITE_MONSTER_KILL = "ELITE_MONSTER_KILL"
BUILDING_KILL = "BUILDING_KILL"

# Objective spawn model. Riot does not return spawn timers, so these are the
# published values and are a documented modelling assumption -- they gate a
# bitmask, not a measurement. Patch-sensitive; see PROJECT_PLAN §10.
DRAGON_FIRST_SPAWN_S = 300
DRAGON_RESPAWN_S = 300
HERALD_SPAWN_S = 840
HERALD_DESPAWN_S = 1500
BARON_SPAWN_S = 1200
BARON_RESPAWN_S = 360


def _u8(v, step=1, null=NULL_U8):
    """Clamp into a uint8 column, reserving 255 as the null sentinel."""
    if v is None:
        return null
    q = int(v // step)
    return 0 if q < 0 else (254 if q > 254 else q)


def _i16(v):
    if v is None:
        return NULL_I16
    return -32767 if v < -32767 else (32767 if v > 32767 else int(v))


@dataclass
class Participant:
    pid: int
    puuid: str
    champ_id: int
    role_sk: int
    team_id: int
    riot_id: str
    won: bool
    imputed_role: bool = False


@dataclass
class _Actor:
    """Mutable per-participant state replayed forward through the timeline."""
    level: int = 1
    dead_until_ms: int = -1
    deaths: int = 0
    kills: int = 0
    last_death_ms: int | None = None


@dataclass
class MatchContext:
    match_id: str
    patch: str
    duration_s: float
    participants: dict[int, Participant]
    frames: list[dict] = field(default_factory=list)
    frame_interval_ms: int = 60_000


def build_participants(match: dict) -> dict[int, Participant]:
    """Index MatchDto participants by participantId.

    teamPosition is preferred over individualPosition because it enforces one
    player per position per team. It is blank in ~0.9% of ranked games (when
    individualPosition is INVALID, dev-rel #554); we fall back and flag rather
    than dropping the game, so team-level features stay valid.
    """
    out: dict[int, Participant] = {}
    for p in match["info"]["participants"]:
        pos = (p.get("teamPosition") or "").strip()
        imputed = False
        if not pos:
            pos = (p.get("individualPosition") or "").strip()
            imputed = True
            if pos in ("", "Invalid", "INVALID"):
                pos = ""
        name = p.get("riotIdGameName") or ""
        tag = p.get("riotIdTagline") or ""
        out[p["participantId"]] = Participant(
            pid=p["participantId"],
            puuid=p.get("puuid", ""),
            champ_id=p.get("championId", 0),
            role_sk=ROLE_TO_SK.get(pos, ROLE_UNKNOWN),
            team_id=p.get("teamId", 100),
            riot_id=f"{name}#{tag}" if name else "",
            won=bool(p.get("win")),
            imputed_role=imputed or not pos,
        )
    return out


def lane_opponents(parts: dict[int, Participant]) -> dict[int, int | None]:
    """participantId -> the opposing participantId in the same teamPosition."""
    by_role: dict[tuple[int, int], int] = {}
    for p in parts.values():
        if p.role_sk != ROLE_UNKNOWN:
            by_role[(p.team_id, p.role_sk)] = p.pid
    out: dict[int, int | None] = {}
    for p in parts.values():
        other = 200 if p.team_id == 100 else 100
        out[p.pid] = by_role.get((other, p.role_sk))
    return out


def _frame_state(frame: dict, pid: int) -> dict | None:
    pf = frame.get("participantFrames") or {}
    return pf.get(str(pid)) or pf.get(pid)


def _as_of_index(frames: list[dict], t_ms: int) -> int:
    """Index of the last frame at or BEFORE t_ms.

    Backward-only. The frame *after* a kill already contains the gold the
    killer earned for it and reflects the victim being dead; interpolating
    across the event leaks the outcome into the feature and manufactures a
    spurious 'being behind causes deaths' result.
    """
    lo, hi, best = 0, len(frames) - 1, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if frames[mid].get("timestamp", 0) <= t_ms:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best


def _cs(state: dict | None) -> int | None:
    if not state:
        return None
    return int(state.get("minionsKilled") or 0) + int(state.get("jungleMinionsKilled") or 0)


def _gold(state: dict | None) -> int | None:
    if not state:
        return None
    v = state.get("totalGold")
    return int(v) if v is not None else None


def _pos(state: dict | None):
    if not state:
        return None
    p = state.get("position") or {}
    if "x" not in p or "y" not in p:
        return None
    return float(p["x"]), float(p["y"])


def classify_cause(ev: dict) -> int:
    """killerId == 0 is an execution, not a champion kill.

    Pooling executions with champion kills inflates turret-adjacent and
    jungle-camp cells with deaths that have no killer -- and they have no valid
    killer block at all, so they must never enter the Kills layer.
    """
    if ev.get("killerId"):
        return CAUSE_CHAMPION
    dmg = ev.get("victimDamageReceived") or []
    if not dmg:
        return CAUSE_UNKNOWN
    best = max(dmg, key=lambda d: (d.get("physicalDamage", 0) or 0)
               + (d.get("magicDamage", 0) or 0) + (d.get("trueDamage", 0) or 0))
    t = (best.get("type") or "").upper()
    if "TURRET" in t or "TOWER" in t:
        return CAUSE_TURRET
    if "MINION" in t:
        return CAUSE_MINION
    if "MONSTER" in t:
        return CAUSE_MONSTER
    return CAUSE_UNKNOWN


class _Objectives:
    """Replay ELITE_MONSTER_KILL history into an up/down bitmask over time."""

    def __init__(self) -> None:
        self.dragon_next = DRAGON_FIRST_SPAWN_S * 1000
        self.baron_next = BARON_SPAWN_S * 1000
        self.herald_taken = False
        self.elder = False
        self.last_objective_ms: int | None = None

    def observe(self, ev: dict) -> None:
        t = ev.get("timestamp", 0)
        self.last_objective_ms = t
        mt = (ev.get("monsterType") or "").upper()
        sub = (ev.get("monsterSubType") or "").upper()
        if "DRAGON" in mt:
            if "ELDER" in sub or "ELDER" in mt:
                self.elder = True
            self.dragon_next = t + DRAGON_RESPAWN_S * 1000
        elif "BARON" in mt:
            self.baron_next = t + BARON_RESPAWN_S * 1000
        elif "HERALD" in mt or "RIFTHERALD" in mt:
            self.herald_taken = True

    def mask(self, t_ms: int) -> int:
        m = 0
        if t_ms >= self.dragon_next:
            m |= OBJ_ELDER if self.elder else OBJ_DRAGON
        if t_ms >= self.baron_next:
            m |= OBJ_BARON
        if (not self.herald_taken
                and HERALD_SPAWN_S * 1000 <= t_ms < HERALD_DESPAWN_S * 1000):
            m |= OBJ_HERALD
        return m


def _alive_ids(actors: dict[int, _Actor], parts, team_id: int, t_ms: int) -> list[int]:
    return [pid for pid, p in parts.items()
            if p.team_id == team_id and actors[pid].dead_until_ms <= t_ms]


def transform_match(match: dict, timeline: dict, match_sk: int,
                    patch_sk: int, player_sk: dict[str, int]) -> list[dict]:
    """Emit one dict per CHAMPION_KILL. Caller assigns surrogate keys."""
    parts = build_participants(match)
    opp = lane_opponents(parts)
    info = timeline.get("info") or {}
    frames = info.get("frames") or []
    # frameInterval is a payload field. Never hardcode 60000 -- if Riot changes
    # the cadence, a hardcoded value silently mis-scales every derived rate.
    interval = int(info.get("frameInterval") or 60_000)

    # Preserve the source location before sorting: timestamps are not unique.
    indexed = [(fi, ei, ev) for fi, f in enumerate(frames)
               for ei, ev in enumerate(f.get("events") or [])]
    indexed.sort(key=lambda item: item[2].get("timestamp", 0))
    events = [ev for _, _, ev in indexed]

    actors = {pid: _Actor() for pid in parts}
    objectives = _Objectives()
    kill_times: list[int] = [e["timestamp"] for e in events
                             if e.get("type") == CHAMPION_KILL]
    first_blood_t = kill_times[0] if kill_times else None

    rows: list[dict] = []
    for source_frame, source_event, ev in indexed:
        etype = ev.get("type")

        if etype == LEVEL_UP:
            pid = ev.get("participantId")
            if pid in actors and ev.get("level"):
                actors[pid].level = int(ev["level"])
            continue

        if etype in (ELITE_MONSTER_KILL, BUILDING_KILL):
            if etype == ELITE_MONSTER_KILL:
                objectives.observe(ev)
            else:
                objectives.last_objective_ms = ev.get("timestamp", 0)
            continue

        if etype != CHAMPION_KILL:
            continue

        t = int(ev.get("timestamp") or 0)
        vid = ev.get("victimId")
        kid = ev.get("killerId") or 0
        if vid not in parts:
            continue

        pos = ev.get("position") or {}
        if "x" not in pos or "y" not in pos:
            continue
        x, y = float(pos["x"]), float(pos["y"])

        cause = classify_cause(ev)
        fi = _as_of_index(frames, t)
        frame = frames[fi] if frames else {}

        victim = parts[vid]
        killer = parts.get(kid) if cause == CAUSE_CHAMPION else None

        # --- alive counts, from the replayed death/respawn state -------------
        blue_alive = len(_alive_ids(actors, parts, 100, t))
        red_alive = len(_alive_ids(actors, parts, 200, t))

        row = {
            "match_id": match.get("metadata", {}).get("matchId", ""),
            "frame_index": source_frame,
            "event_index": source_event,
            "event_ms": t,
            "victim_pid": vid,
            "killer_pid": kid if killer is not None else 0,
            "match_sk": match_sk,
            "x": int(round(x)),
            "y": int(round(y)),
            "second": min(int(t // 1000), 65535),
            "region_sk": region_sk(x, y),
            "patch_sk": patch_sk,
            "cause": cause,
            "assists": len(ev.get("assistingParticipantIds") or []),
            "bounty": min(int((ev.get("bounty") or 0)
                              + (ev.get("shutdownBounty") or 0)), 65535),
            "team_gold_diff": _i16(_team_gold_diff(frame, parts)),
            "alive_counts": (min(blue_alive, 15) << 4) | min(red_alive, 15),
            "objectives_up": objectives.mask(t),
            "since_objective": _u8(
                None if objectives.last_objective_ms is None
                else (t - objectives.last_objective_ms) / 1000.0,
                SINCE_OBJECTIVE_STEP),
        }

        flags = 0
        if victim.team_id == 200:
            flags |= F_VICTIM_TEAM_RED
        if victim.won:
            flags |= F_VICTIM_WON
        if first_blood_t is not None and t == first_blood_t:
            flags |= F_FIRST_BLOOD
        if under_turret(x, y):
            flags |= F_UNDER_TURRET
        if any(abs(kt - t) <= TRADE_WINDOW_S * 1000 and kt != t for kt in kill_times):
            flags |= F_TRADE
        if victim.imputed_role or (killer and killer.imputed_role):
            flags |= F_POSITION_IMPUTED
        row["flags"] = flags

        _fill_actor(row, "victim", victim, actors, parts, frame, opp, t,
                    player_sk, interval)
        _fill_actor(row, "killer", killer, actors, parts, frame, opp, t,
                    player_sk, interval)

        rows.append(row)

        # --- advance replay state AFTER the row is emitted -------------------
        a = actors[vid]
        a.deaths += 1
        a.last_death_ms = t
        a.dead_until_ms = t + int(
            respawn_seconds(a.level, t / 60_000.0) * 1000)
        if killer is not None:
            actors[killer.pid].kills += 1

    return rows


def _team_gold_diff(frame: dict, parts: dict[int, Participant]) -> int | None:
    """Blue minus red, a FIXED frame so the diverging slider means the same
    thing on every row regardless of which side the subject was on."""
    if not frame:
        return None
    blue = red = 0
    seen = False
    for pid, p in parts.items():
        g = _gold(_frame_state(frame, pid))
        if g is None:
            continue
        seen = True
        if p.team_id == 100:
            blue += g
        else:
            red += g
    return (blue - red) if seen else None


def _fill_actor(row: dict, prefix: str, actor: Participant | None,
                actors: dict[int, _Actor], parts: dict[int, Participant],
                frame: dict, opp: dict[int, int | None], t_ms: int,
                player_sk: dict[str, int], interval: int) -> None:
    """Populate one 16-byte actor block. Nulls out cleanly for executions."""
    if actor is None:
        row.update({
            f"{prefix}_champ": 0,
            f"{prefix}_role": ROLE_UNKNOWN,
            f"{prefix}_player": NULL_U16,
            f"{prefix}_level": NULL_U8,
            f"{prefix}_gold_diff_lane": NULL_I16,
            f"{prefix}_cs_diff_lane": NULL_I16,
            f"{prefix}_isolation": NULL_U8,
            f"{prefix}_enemies_near": NULL_U8,
            f"{prefix}_ordinal": NULL_U8,
            f"{prefix}_since_prev_death": NULL_U8,
            f"{prefix}_lane_opp_champ": 0,
        })
        return

    pid = actor.pid
    st = _frame_state(frame, pid)
    o_pid = opp.get(pid)
    o_st = _frame_state(frame, o_pid) if o_pid else None

    gold = _gold(st)
    o_gold = _gold(o_st)
    cs = _cs(st)
    o_cs = _cs(o_st)

    my_pos = _pos(st)
    isolation = enemies_near = None
    if my_pos:
        mates = [p for p in _alive_ids(actors, parts, actor.team_id, t_ms) if p != pid]
        dists = [distance(*my_pos, *_pos(_frame_state(frame, m)))
                 for m in mates if _pos(_frame_state(frame, m))]
        if dists:
            isolation = min(dists) / ISOLATION_BUCKET
        foes = _alive_ids(actors, parts, 200 if actor.team_id == 100 else 100, t_ms)
        enemies_near = sum(
            1 for f in foes
            if _pos(_frame_state(frame, f))
            and distance(*my_pos, *_pos(_frame_state(frame, f))) <= NEARBY_RADIUS)

    a = actors[pid]
    since = None if a.last_death_ms is None else (t_ms - a.last_death_ms) / 1000.0
    ordinal = a.deaths if prefix == "victim" else a.kills

    row.update({
        f"{prefix}_champ": actor.champ_id,
        f"{prefix}_role": actor.role_sk,
        f"{prefix}_player": player_sk.get(actor.riot_id, NULL_U16)
        if actor.riot_id else NULL_U16,
        f"{prefix}_level": _u8(a.level),
        f"{prefix}_gold_diff_lane": NULL_I16 if (gold is None or o_gold is None)
        else _i16(gold - o_gold),
        f"{prefix}_cs_diff_lane": NULL_I16 if (cs is None or o_cs is None)
        else _i16(cs - o_cs),
        f"{prefix}_isolation": _u8(isolation),
        f"{prefix}_enemies_near": _u8(enemies_near),
        f"{prefix}_ordinal": _u8(ordinal),
        f"{prefix}_since_prev_death": _u8(since, SINCE_DEATH_STEP),
        f"{prefix}_lane_opp_champ": parts[o_pid].champ_id if o_pid else 0,
    })


# Column order is the wire format. serve/bundle.py and web/app.js both depend
# on it, and tests/test_schema.py asserts the three agree.
COLUMNS: list[tuple[str, str]] = [
    ("match_sk", "u32"),
    ("x", "i16"), ("y", "i16"),
    ("second", "u16"),
    ("region_sk", "u8"), ("patch_sk", "u8"),
    ("cause", "u8"), ("assists", "u8"), ("flags", "u8"),
    ("alive_counts", "u8"), ("objectives_up", "u8"), ("since_objective", "u8"),
    ("bounty", "u16"), ("team_gold_diff", "i16"),
    ("victim_champ", "u16"), ("victim_role", "u8"), ("victim_player", "u16"),
    ("victim_level", "u8"), ("victim_gold_diff_lane", "i16"),
    ("victim_cs_diff_lane", "i16"), ("victim_isolation", "u8"),
    ("victim_enemies_near", "u8"), ("victim_ordinal", "u8"),
    ("victim_since_prev_death", "u8"), ("victim_lane_opp_champ", "u16"),
    ("killer_champ", "u16"), ("killer_role", "u8"), ("killer_player", "u16"),
    ("killer_level", "u8"), ("killer_gold_diff_lane", "i16"),
    ("killer_cs_diff_lane", "i16"), ("killer_isolation", "u8"),
    ("killer_enemies_near", "u8"), ("killer_ordinal", "u8"),
    ("killer_since_prev_death", "u8"), ("killer_lane_opp_champ", "u16"),
]

# Columns the default view and every headline filter need. Split out so first
# paint does not wait on the full payload.
CORE_COLUMNS = {
    "match_sk", "x", "y", "second", "region_sk", "patch_sk",
    "cause", "assists", "flags", "team_gold_diff",
    "victim_champ", "victim_role", "victim_player",
    "killer_champ", "killer_role", "killer_player",
}
