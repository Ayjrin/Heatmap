"""Roster, seeding, budget, harvest, ranking and rounds. Fakes only; no network."""
import sys
import threading
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import proleague.pipeline as pipeline
from proleague.config import Config, Paths, TIER_UNRANKED
from proleague.curated import LocalObjects
from proleague.extract.limiter import DEV_KEY_LIMITS, KEY_LIMITS, PROD_KEY_LIMITS
from proleague.pipeline_state import LocalState


class Ladder:
    """A Riot double with a ladder, per-division pages, lookups and histories."""

    def __init__(self):
        self.call_count = 0
        self.apex = {"CHALLENGER": [], "GRANDMASTER": [], "MASTER": []}
        self.pages = {}            # (tier, division) -> [page1 entries, page2 entries, ...]
        self.ranks = {}            # puuid -> (tier, division, lp) for by-puuid lookups
        self.histories = {}        # puuid -> [match ids]
        self.participants = {}     # match id -> ten puuids
        self.patches = {}          # match id -> gameVersion
        self.exp_calls, self.lookup_calls, self.history_calls = [], [], []
        self.stop, self.stop_after = None, None

    def entry(self, puuid, division="I", lp=50):
        return {"puuid": puuid, "rank": division, "leaguePoints": lp}

    def apex_league(self, platform, tier):
        self.call_count += 1
        return {"entries": list(self.apex[tier])}

    def league_exp_entries(self, platform, tier, *, division, page, **_):
        self.call_count += 1
        self.exp_calls.append((tier, division, page))
        if self.stop is not None and (tier, division, page) == self.stop_after:
            self.stop.set()
        pages = self.pages.get((tier, division), [])
        return [dict(e, tier=tier) for e in pages[page - 1]] if page - 1 < len(pages) else []

    def league_entries_by_puuid(self, platform, puuid):
        self.call_count += 1
        self.lookup_calls.append(puuid)
        rank = self.ranks.get(puuid)
        if rank is None:
            return []
        return [{"queueType": "RANKED_FLEX_SR", "tier": "GOLD", "rank": "IV", "leaguePoints": 1},
                {"queueType": "RANKED_SOLO_5x5", "tier": rank[0], "rank": rank[1], "leaguePoints": rank[2]}]

    def match_ids_by_puuid(self, region, puuid, *, start, count, **_):
        self.call_count += 1
        self.history_calls.append(puuid)
        return self.histories.get(puuid, [])[start:start + count]

    def game(self, mid, players, patch="16.17.1"):
        self.participants[mid] = list(players)
        self.patches[mid] = patch
        for puuid in players:
            self.histories.setdefault(puuid, []).append(mid)


@pytest.fixture
def world(tmp_path, monkeypatch):
    state = LocalState(tmp_path / "state")
    store, site = LocalObjects(tmp_path / "data"), LocalObjects(tmp_path / "site")
    commits, published = {}, []

    def ingest(client, region, mid, store, *, run_id, on_context=None, **kwargs):
        client.call_count += 2      # match + timeline
        context = {"metadata": {"matchId": mid}, "info": {
            "gameVersion": client.patches[mid],
            "participants": [{"puuid": p} for p in client.participants[mid]]}}
        if on_context is not None:
            veto = on_context(context)
            if veto:
                return None, veto
        commits.setdefault(mid, {"run_id": run_id, "row_count": 0})
        return commits[mid], None

    monkeypatch.setattr(pipeline, "ingest_match", ingest)

    def publish(*_, before_publish, roster=None, **kwargs):
        before_publish()
        published.append(roster)
        return {"dataset_id": "test-release", "count": len(commits), "rows": 0}

    def run(client, mode, cfg, run_id=None, resume=False, stop=None):
        client.call_count = 0       # a resume constructs a fresh RiotClient
        return pipeline.Collection(cfg, state, store, site, client=client, mode=mode,
                                   run_id=run_id or str(uuid.uuid4()), resume=resume,
                                   stop=stop, publisher=publish).execute()

    def config(**overrides):
        base = {"tiers": ["CHALLENGER"], "matches_per_player": 500, "rank_lookup_budget": 0,
                "paths": Paths(root=tmp_path, data=tmp_path / "data")}
        return Config(**{**base, **overrides})

    return state, commits, published, run, config


def roster(state, puuid):
    return state.get(f"PLAYER#americas#{puuid}") or {}


def test_key_limits_selected_by_kind():
    assert KEY_LIMITS["dev"] == KEY_LIMITS["personal"] == DEV_KEY_LIMITS
    assert KEY_LIMITS["production"] == PROD_KEY_LIMITS
    with pytest.raises(ValueError, match="key_kind"):
        Config(key_kind="platinum").validate()
    with pytest.raises(ValueError, match="tiers"):
        Config(tiers=["WOOD"]).validate()


def test_seeding_pages_every_division_and_resumes_at_the_next_page(world):
    state, commits, published, run, config = world
    client = Ladder()
    client.apex["CHALLENGER"] = [client.entry("c1", lp=900), client.entry("c2", lp=800)]
    client.pages[("DIAMOND", "I")] = [[client.entry("d1", "I", 70), client.entry("d2", "I", 60),
                                      client.entry("d3", "I", 50)], [client.entry("d4", "I", 40)]]
    client.pages[("DIAMOND", "II")] = [[client.entry("d5", "II", 10)]]
    for i, puuid in enumerate(["c1", "c2", "d1", "d2", "d3", "d4", "d5"]):
        client.game(f"NA1_{i}", [puuid] + [f"other-{i}-{j}" for j in range(9)])
    cfg = config(tiers=["CHALLENGER", "DIAMOND"], seed_pages_per_division=5)
    client.stop, client.stop_after = threading.Event(), ("DIAMOND", "I", 1)

    first = run(client, "full", cfg, stop=client.stop)
    # Interrupted right after the first Diamond page. The page cursor is
    # durable, the entries already seen are on the roster, and the run paused.
    assert (first["status"], first["stage"], first["requests_used"]) == ("paused", "interrupted", 2)
    assert client.exp_calls == [("DIAMOND", "I", 1)]
    marker = state.get(f"LADDER#{first['run_id']}#DIAMOND#I")
    assert (marker["done"], marker["next_page"]) == (False, 2)
    assert roster(state, "d1")["tier"] == "DIAMOND" and roster(state, "c1")["lp"] == 900
    client.stop = None

    resumed = run(client, "full", cfg, run_id=first["run_id"], resume=True)
    assert resumed["status"] == "succeeded"
    # Page one is never asked for again; every division runs to its terminator.
    assert client.exp_calls[1:] == [("DIAMOND", "I", 2), ("DIAMOND", "I", 3), ("DIAMOND", "II", 1),
                                    ("DIAMOND", "II", 2), ("DIAMOND", "III", 1), ("DIAMOND", "IV", 1)]
    assert resumed["roster_players"] == 7 + 7 * 9 and resumed["players"] == 7
    assert set(commits) == {f"NA1_{i}" for i in range(7)}
    d5 = roster(state, "d5")
    assert (d5["tier"], d5["division"], d5["lp"], d5["rank_source"]) == ("DIAMOND", "II", 10, "ladder")
    # The watermark write merged into the roster record instead of replacing it.
    assert d5["scanned_to"] == resumed["started_at"] and d5["listed_at"] == resumed["started_at"]
    assert published[-1] and any(r["puuid"] == "d5" for r in published[-1])
    # Harvested participants carry appearances but no rank until a lookup.
    other = roster(state, "other-0-0")
    assert other["appearances"] == 1 and "tier" not in other


def test_budget_exhaustion_publishes_and_a_resume_continues_the_same_run(world):
    state, commits, published, run, config = world
    client = Ladder()
    client.apex["CHALLENGER"] = [client.entry("c1")]
    for i in range(60):
        client.game(f"NA1_{i}", ["c1"] + [f"p{i}-{j}" for j in range(9)])
    cfg = config(request_budget=6)
    first = run(client, "smoke", cfg)
    # 1 preflight + 1 history page + 2 x 2 games = 6, then the check trips.
    assert (first["status"], first["stage"], first["new_games"]) == ("paused", "budget_exhausted", 2)
    assert first["requests_used"] == 6 and first["request_budget"] == 6
    assert state.get("PUBLISHED")["count"] == 2 and first["published_version"] == "test-release"
    assert state.get("ACTIVE") is None
    # The history still holds unfetched games, so its watermark did not move.
    assert "scanned_to" not in roster(state, "c1")
    resumed = run(client, "smoke", cfg, run_id=first["run_id"], resume=True)
    # A fresh client means a fresh allotment: preflight plus three games.
    assert (resumed["status"], resumed["stage"], resumed["new_games"]) == ("paused", "budget_exhausted", 5)
    again = run(client, "smoke", Config(**{**cfg.__dict__, "request_budget": 10_000}),
                run_id=first["run_id"], resume=True)
    # The budget is frozen on the run like every other setting: the same run
    # keeps pausing every six requests until a new run is started with more.
    assert (again["status"], again["new_games"], again["request_budget"]) == ("paused", 8, 6)


def test_harvest_ranks_the_most_seen_unknown_players_first_and_records_unranked(world):
    state, commits, published, run, config = world
    client = Ladder()
    client.apex["CHALLENGER"] = [client.entry("c1")]
    client.game("NA1_1", ["c1", "u1", "u2"] + [f"x{j}" for j in range(7)])
    client.game("NA1_2", ["c1", "u1", "u2"] + [f"y{j}" for j in range(7)])
    client.game("NA1_3", ["c1", "u1"] + [f"z{j}" for j in range(8)])
    client.ranks["u1"] = ("DIAMOND", "I", 33)
    for j in range(7):
        client.ranks[f"x{j}"] = ("GOLD", "IV", 1)
    result = run(client, "full", config(rank_lookup_budget=2))
    assert result["status"] == "succeeded" and result["rank_lookups"] == 2
    assert client.lookup_calls == ["u1", "u2"]
    u1, u2 = roster(state, "u1"), roster(state, "u2")
    assert (u1["tier"], u1["division"], u1["lp"], u1["rank_source"], u1["appearances"]) == ("DIAMOND", "I", 33, "lookup", 3)
    assert (u2["tier"], u2["division"], u2["appearances"]) == (TIER_UNRANKED, None, 2)
    assert "tier" not in roster(state, "x0") and roster(state, "x0")["appearances"] == 1
    assert roster(state, "c1")["appearances"] == 3 and roster(state, "c1")["tier"] == "CHALLENGER"
    # A second run does not spend lookups on players already resolved this week;
    # the two it spends go to still-unknown players (all tied at one appearance).
    again = run(client, "full", config(rank_lookup_budget=2))
    assert again["rank_lookups"] == 2 and len(client.lookup_calls) == 4
    assert all(p[0] in "xyz" and roster(state, p)["appearances"] == 1 for p in client.lookup_calls[2:])


def test_rank_history_records_tier_changes_and_the_ttl_reobserves(world, monkeypatch):
    state, commits, published, run, config = world
    client = Ladder()
    client.apex["CHALLENGER"] = [client.entry("c1", lp=1200)]
    client.game("NA1_1", ["c1", "u1"] + [f"x{j}" for j in range(8)])
    client.game("NA1_2", ["c1", "u1"] + [f"y{j}" for j in range(8)])
    client.ranks["u1"] = ("DIAMOND", "II", 5)
    cfg = config(tiers=["CHALLENGER", "GRANDMASTER"], rank_lookup_budget=1)
    first = run(client, "full", cfg)
    assert roster(state, "c1")["tier"] == "CHALLENGER" and "rank_history" not in roster(state, "c1")
    assert client.lookup_calls == ["u1"]

    client.apex = {"CHALLENGER": [], "GRANDMASTER": [client.entry("c1", lp=400)], "MASTER": []}
    second = run(client, "full", cfg)
    c1 = roster(state, "c1")
    assert (c1["tier"], c1["lp"]) == ("GRANDMASTER", 400)
    assert [(h["tier"], h["division"], h["lp"]) for h in c1["rank_history"]] == [("CHALLENGER", "I", 1200)]
    assert c1["rank_history"][0]["at"] >= first["started_at"]
    # u1 was looked up last run and is still fresh, so the one lookup this run
    # goes to a player nobody has resolved yet.
    assert len(client.lookup_calls) == 2 and client.lookup_calls[1] != "u1"

    later = pipeline.now() + cfg.rank_ttl_seconds + 1
    monkeypatch.setattr(pipeline, "now", lambda: later)
    client.ranks["u1"] = ("MASTER", "I", 0)
    run(client, "full", cfg)
    u1 = roster(state, "u1")
    assert client.lookup_calls[-1] == "u1" and u1["tier"] == "MASTER"
    assert [h["tier"] for h in u1["rank_history"]] == ["DIAMOND"]


def test_frontier_is_capped_per_run_and_rotates_by_least_recently_listed(world):
    state, commits, published, run, config = world
    client = Ladder()
    client.apex["CHALLENGER"] = [client.entry("c1"), client.entry("c2")]
    client.game("NA1_1", ["c1"] + [f"a{j}" for j in range(9)])
    client.game("NA1_2", ["c2"] + [f"b{j}" for j in range(9)])
    cfg = config(players_per_run=1, max_rounds=1)
    first = run(client, "full", cfg)
    assert first["players"] == 1 and len(client.history_calls) == 1
    listed = client.history_calls[0]
    assert roster(state, listed)["listed_at"] == first["started_at"]
    second = run(client, "full", cfg)
    assert client.history_calls[1] == ({"c1", "c2"} - {listed}).pop()
    assert set(commits) == {"NA1_1", "NA1_2"}


def test_second_round_walks_the_players_the_first_round_resolved(world):
    state, commits, published, run, config = world
    client = Ladder()
    client.apex["CHALLENGER"] = [client.entry("c1")]
    client.game("NA1_1", ["c1", "u1"] + [f"x{j}" for j in range(8)])
    client.game("NA1_2", ["u1"] + [f"y{j}" for j in range(9)])       # only reachable through u1
    client.game("NA1_3", ["u2"] + [f"z{j}" for j in range(9)])       # u2 is below the floor
    client.ranks["u1"] = ("DIAMOND", "IV", 0)
    client.ranks["u2"] = ("EMERALD", "I", 99)
    client.game("NA1_4", ["c1", "u2", "u1"] + [f"w{j}" for j in range(7)])
    cfg = config(tiers=["CHALLENGER", "DIAMOND"], rank_lookup_budget=20, max_rounds=2)
    result = run(client, "full", cfg)
    assert result["status"] == "succeeded" and result["round"] == 1
    assert client.history_calls == ["c1", "u1"]
    assert set(commits) == {"NA1_1", "NA1_2", "NA1_4"}
    assert state.get(f"RUNPLAYER#{result['run_id']}#u1")["round"] == 1
    assert roster(state, "u2")["tier"] == "EMERALD" and "scanned_to" not in roster(state, "u2")
    single = run(client, "full", Config(**{**cfg.__dict__, "max_rounds": 1}))
    # One round lists the seeded ladder only; the snowball needs the second.
    assert single["round"] == 0


def test_patch_cap_settles_extra_games_without_fetching_their_timelines(world):
    state, commits, published, run, config = world
    client = Ladder()
    client.apex["CHALLENGER"] = [client.entry("c1")]
    for i in range(3):
        client.game(f"NA1_{i}", ["c1"] + [f"p{i}-{j}" for j in range(9)], patch="16.17.1")
    client.game("NA1_9", ["c1"] + [f"q{j}" for j in range(9)], patch="16.16.2")
    result = run(client, "full", config(max_matches_per_patch=1))
    assert result["status"] == "succeeded" and result["new_games"] == 2
    statuses = sorted(r["status"] for r in state.scan(f"DISCOVERY#{result['run_id']}#"))
    assert statuses == ["capped", "capped", "complete", "complete"]
    assert {state.get("MATCH#" + mid)["patch"] for mid in commits} == {"16.17", "16.16"}
    # Capped games are accounted for, so the history's watermark still advances.
    assert roster(state, "c1")["scanned_to"] == result["started_at"]
