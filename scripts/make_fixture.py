#!/usr/bin/env python3
"""Generate structurally-faithful synthetic MatchDto + TimelineDto payloads.

No RIOT_API_KEY is required to exercise this project end to end. The crawl is
the only stage that needs one; everything downstream -- transform, bundle,
renderer, and every check in PROJECT_PLAN §11 -- runs against these fixtures.

What is faithful:
  * the exact JSON shape the transform reads (frames, participantFrames keyed
    by stringified participantId, the flat event union, teamPosition, etc.)
  * spatial structure: kills cluster on lanes, jungle camps and objective pits,
    so face-validity checks are meaningful rather than uniform noise
  * the traps: ~1% blank teamPosition, some killerId==0 executions, a remake,
    an ARAM game, and gameDuration in BOTH unit conventions

What is NOT faithful: the numbers are invented. Fixtures are clearly labelled
and written to their own directory so they can never mix with real bronze.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proleague.config import MAP_MAX_X, MAP_MIN_X, MAP_SPAN_X, MAP_SPAN_Y, Paths  # noqa: E402
from proleague.extract.bronze import write_json_gz  # noqa: E402

ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
CHAMPS = [266, 103, 84, 12, 32, 34, 1, 22, 136, 268, 432, 53, 63, 201, 51,
          164, 69, 31, 42, 122, 131, 119, 36, 245, 60, 28, 81, 9, 114, 105]
PATCHES = ["16.17.481.9107", "16.16.470.2201"]

# Kill hot-spots in normalized coords, with a weight and a spread. Drawn to the
# Rift's real geography so "deaths trace the lanes" is actually testable.
HOTSPOTS = [
    (0.05, 0.45, 6, 0.030),   # blue top lane
    (0.05, 0.65, 5, 0.030),
    (0.45, 0.05, 6, 0.030),   # blue bot lane
    (0.65, 0.05, 5, 0.030),
    (0.30, 0.30, 7, 0.035),   # mid
    (0.50, 0.50, 8, 0.040),
    (0.70, 0.70, 7, 0.035),
    (0.95, 0.55, 6, 0.030),   # red top lane
    (0.55, 0.95, 6, 0.030),
    (0.68, 0.24, 9, 0.028),   # dragon pit
    (0.24, 0.68, 9, 0.028),   # baron pit
    (0.30, 0.40, 5, 0.030),   # blue jungle
    (0.40, 0.28, 5, 0.030),
    (0.70, 0.60, 5, 0.030),   # red jungle
    (0.60, 0.72, 5, 0.030),
    (0.50, 0.58, 4, 0.035),   # river skirmishes
    (0.58, 0.50, 4, 0.035),
]
_TOTAL_W = sum(h[2] for h in HOTSPOTS)


def _sample_point(rng: random.Random) -> tuple[int, int]:
    r = rng.uniform(0, _TOTAL_W)
    acc = 0.0
    for u, v, w, s in HOTSPOTS:
        acc += w
        if r <= acc:
            break
    du = rng.gauss(0, s)
    dv = rng.gauss(0, s)
    uu = min(max(u + du, 0.002), 0.998)
    vv = min(max(v + dv, 0.002), 0.998)
    return (int(uu * MAP_SPAN_X + MAP_MIN_X), int(vv * MAP_SPAN_Y - 120))


def _participants(rng: random.Random, blank_role: bool) -> list[dict]:
    champs = rng.sample(CHAMPS, 10)
    blue_wins = rng.random() < 0.5
    out = []
    for i in range(10):
        team = 100 if i < 5 else 200
        role = ROLES[i % 5]
        won = (team == 100) == blue_wins
        # ~0.9% of ranked games have a blank teamPosition (dev-rel #554).
        pos = "" if (blank_role and i == 3) else role
        out.append({
            "participantId": i + 1,
            "puuid": f"SYNTH-PUUID-{i:02d}-" + "x" * 60,
            "championId": champs[i],
            "championName": f"Champ{champs[i]}",
            "teamId": team,
            "teamPosition": pos,
            "individualPosition": role if pos else "Invalid",
            "lane": role,
            "role": "SOLO",
            "win": won,
            "riotIdGameName": f"Player{i:02d}",
            "riotIdTagline": "NA1",
            "kills": rng.randint(0, 12), "deaths": rng.randint(0, 10),
            "assists": rng.randint(0, 18),
            "totalMinionsKilled": rng.randint(20, 260),
            "neutralMinionsKilled": rng.randint(0, 160),
            "goldEarned": rng.randint(6000, 20000),
            "visionScore": rng.randint(8, 90),
            "wardsPlaced": rng.randint(3, 40),
        })
    return out


def make_match(rng: random.Random, match_id: str, *, patch: str,
               duration_s: int, map_id: int = 11, queue_id: int = 420,
               complete: bool = True, blank_role: bool = False,
               ms_duration: bool = False) -> dict:
    parts = _participants(rng, blank_role)
    info = {
        "gameId": int(match_id.split("_")[1]),
        "gameVersion": patch,
        "mapId": map_id,
        "queueId": queue_id,
        "gameMode": "CLASSIC",
        "gameType": "MATCHED_GAME",
        "gameCreation": 1_760_000_000_000,
        "gameStartTimestamp": 1_760_000_000_000,
        "endOfGameResult": "GameComplete" if complete else "Abort_Unexpected",
        "participants": parts,
        "teams": [
            {"teamId": 100, "win": parts[0]["win"]},
            {"teamId": 200, "win": parts[5]["win"]},
        ],
        "platformId": "NA1",
    }
    # gameDuration's unit is conditional: seconds when gameEndTimestamp is
    # present, milliseconds when it is not. Emit both conventions so the
    # rejection filter is genuinely exercised.
    if ms_duration:
        info["gameDuration"] = duration_s * 1000
    else:
        info["gameDuration"] = duration_s
        info["gameEndTimestamp"] = info["gameStartTimestamp"] + duration_s * 1000
    if not complete:
        for p in parts:
            p["teamEarlySurrendered"] = True
    return {"metadata": {"matchId": match_id, "dataVersion": "2",
                         "participants": [p["puuid"] for p in parts]},
            "info": info}


def make_timeline(rng: random.Random, match: dict) -> dict:
    """Frames every 60s with positions/gold/cs/level, plus the event stream."""
    mid = match["metadata"]["matchId"]
    info = match["info"]
    dur = info.get("gameDuration")
    if "gameEndTimestamp" not in info:
        dur //= 1000
    n_frames = max(int(dur // 60) + 1, 2)
    parts = info["participants"]

    levels = {p["participantId"]: 1 for p in parts}
    frames, events = [], []

    n_kills = max(2, int(rng.gauss(28, 6)))
    kill_ts = sorted(rng.randint(90, max(120, dur - 5)) * 1000
                     for _ in range(n_kills))

    for fi in range(n_frames):
        t = fi * 60_000
        pf = {}
        for p in parts:
            pid = p["participantId"]
            # Level roughly tracks game time, capped at 18.
            lv = min(18, 1 + int(fi * 0.75) + rng.randint(-1, 1))
            lv = max(1, lv)
            while levels[pid] < lv:
                levels[pid] += 1
                events.append({"type": "LEVEL_UP", "timestamp": t,
                               "participantId": pid, "level": levels[pid]})
            x, y = _sample_point(rng)
            pf[str(pid)] = {
                "participantId": pid,
                "position": {"x": x, "y": y},
                "totalGold": 500 + fi * rng.randint(280, 420),
                "currentGold": rng.randint(0, 1500),
                "minionsKilled": int(fi * rng.uniform(4, 9)),
                "jungleMinionsKilled": int(fi * rng.uniform(0, 5)),
                "level": levels[pid],
                "xp": fi * rng.randint(300, 500),
            }
        frames.append({"timestamp": t, "participantFrames": pf, "events": []})

    # objectives
    for t in range(300_000, dur * 1000, 300_000):
        if rng.random() < 0.6:
            events.append({
                "type": "ELITE_MONSTER_KILL", "timestamp": t,
                "killerId": rng.randint(1, 10),
                "monsterType": rng.choice(["DRAGON", "RIFTHERALD", "BARON_NASHOR"]),
                "monsterSubType": rng.choice(["FIRE_DRAGON", "OCEAN_DRAGON", ""]),
                "position": {"x": _sample_point(rng)[0], "y": _sample_point(rng)[1]},
            })

    # champion kills
    for t in kill_ts:
        vid = rng.randint(1, 10)
        v_team = 100 if vid <= 5 else 200
        execution = rng.random() < 0.06   # killerId == 0
        if execution:
            kid = 0
        else:
            foes = [i for i in range(1, 11) if (i <= 5) != (vid <= 5)]
            kid = rng.choice(foes)
        allies = [i for i in range(1, 11) if (i <= 5) == (kid <= 5) and i != kid] \
            if kid else []
        x, y = _sample_point(rng)
        ev = {
            "type": "CHAMPION_KILL", "timestamp": t,
            "victimId": vid, "killerId": kid,
            "position": {"x": x, "y": y},
            "assistingParticipantIds": rng.sample(allies, rng.randint(0, min(3, len(allies))))
            if allies else [],
            "bounty": rng.choice([0, 0, 150, 300, 450]),
            "shutdownBounty": rng.choice([0, 0, 0, 300]),
            "killStreakLength": rng.randint(0, 4),
            "victimTeamId": v_team,
            "killerTeamId": 0 if not kid else (100 if kid <= 5 else 200),
        }
        if execution:
            ev["victimDamageReceived"] = [{
                "type": rng.choice(["TOWER", "MINION", "MONSTER"]),
                "name": "synthetic", "participantId": 0,
                "physicalDamage": rng.randint(200, 900),
                "magicDamage": 0, "trueDamage": 0,
            }]
        else:
            ev["victimDamageReceived"] = [{
                "type": "OTHER", "name": "spell", "spellName": "SynthQ",
                "spellSlot": 0, "participantId": kid,
                "physicalDamage": rng.randint(200, 1400),
                "magicDamage": rng.randint(0, 900), "trueDamage": 0,
            }]
        events.append(ev)

    events.sort(key=lambda e: e["timestamp"])
    for ev in events:
        fi = min(ev["timestamp"] // 60_000, n_frames - 1)
        frames[fi]["events"].append(ev)

    return {"metadata": {"matchId": mid, "dataVersion": "2",
                         "participants": [p["puuid"] for p in parts]},
            "info": {"frameInterval": 60_000, "gameId": info["gameId"],
                     "frames": frames,
                     "participants": [{"participantId": p["participantId"],
                                       "puuid": p["puuid"]} for p in parts],
                     "endOfGameResult": info["endOfGameResult"]}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--matches", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=None,
                    help="defaults to data/bronze-fixture")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    paths = Paths()
    out = args.out or (paths.data / "bronze-fixture")
    (out / "raw" / "match").mkdir(parents=True, exist_ok=True)
    (out / "raw" / "timeline").mkdir(parents=True, exist_ok=True)

    written = 0
    for i in range(args.matches):
        mid = f"NA1_{5_000_000_000 + i}"
        # Deliberate bad apples so the rejection rules are exercised:
        #   i % 23 == 0 -> remake      i % 29 == 0 -> ARAM
        #   i % 31 == 0 -> ms-duration convention
        remake = (i % 23 == 0 and i > 0)
        aram = (i % 29 == 0 and i > 0)
        match = make_match(
            rng, mid,
            patch=PATCHES[0] if i % 5 else PATCHES[1],
            duration_s=rng.randint(200, 300) if remake else rng.randint(1300, 2300),
            map_id=12 if aram else 11,
            queue_id=450 if aram else 420,
            complete=not remake,
            blank_role=(i % 101 == 0),
            ms_duration=(i % 31 == 0),
        )
        write_json_gz(out / "raw" / "match" / f"{mid}.json.gz", match)
        write_json_gz(out / "raw" / "timeline" / f"{mid}.json.gz",
                      make_timeline(rng, match))
        written += 1

    print(f"wrote {written} synthetic match+timeline pairs to {out}")
    print("SYNTHETIC DATA -- never mix with real bronze.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
