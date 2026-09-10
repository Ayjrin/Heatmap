"""ETL integration tests. All invented inputs stay inside pytest temp directories."""
import copy
import json
import random
import sys
import threading
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import make_fixture
import dev_server
import proleague.pipeline as pipeline
from proleague.config import Config, Paths
from proleague.curated import LocalObjects, ValidationError, ingest_match, json_bytes
from proleague.dataset import build_release
from proleague.extract.riot_client import AuthError, RiotAPIError
from proleague.extract.routing import Region
from proleague.pipeline_state import LocalState, OwnershipError


PLAYER_MARK = "PLAYER#americas#test-player"


class Histories:
    def __init__(self, count):
        self.ids = [f"NA1_{i}" for i in range(count)]
        self.created = {mid: 0 for mid in self.ids}
        self.history_calls = 0
        self.windows = []
        self.expired = False

    def apex_league(self, *_):
        if self.expired:
            raise AuthError("expired test key")
        return {"entries": [{"puuid": "test-player"}]}

    def played(self, match_id, created_at):
        self.ids.append(match_id)
        self.created[match_id] = created_at

    def match_ids_by_puuid(self, *_, start, count, start_time=None, **kwargs):
        # match-v5 bounds a history server-side, so the double honours startTime.
        # endTime is ignored: a test's runs share one wall-clock second, and a
        # game "played after the last run" would otherwise be unreachable.
        self.history_calls += 1
        self.windows.append(start_time)
        visible = [m for m in self.ids if start_time is None or self.created[m] > start_time]
        return visible[start:start + count]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = Config(tiers=["CHALLENGER"], matches_per_player=500,
                 paths=Paths(root=tmp_path, data=tmp_path / "data"))
    state = LocalState(tmp_path / "state")
    store, site = LocalObjects(tmp_path / "data"), LocalObjects(tmp_path / "site")
    commits = {}

    def ingest(client, region, mid, store, *, run_id, **kwargs):
        if mid not in commits:
            commits[mid] = {"run_id": run_id, "row_count": 0}
        return commits[mid], None

    monkeypatch.setattr(pipeline, "ingest_match", ingest)

    def publish(*_, before_publish, **kwargs):
        before_publish()
        return {"dataset_id": "test-release", "count": len(commits), "rows": 0}

    def run(client, mode, run_id=None, resume=False, stop=None):
        return pipeline.Collection(cfg, state, store, site, client=client, mode=mode,
            run_id=run_id or str(uuid.uuid4()), resume=resume, stop=stop,
            publisher=publish).execute()

    return state, commits, run


def test_smoke_then_small_are_new_successes_and_full_refreshes(setup):
    state, commits, run = setup
    client = Histories(320)
    first = run(client, "smoke")
    assert (first["status"], first["new_games"], len(commits)) == ("succeeded", 50, 50)
    second = run(client, "small")
    assert (second["status"], second["new_games"], len(commits)) == ("succeeded", 250, 300)
    full = run(client, "full")
    assert (full["status"], full["new_games"], len(commits)) == ("succeeded", 20, 320)
    calls = client.history_calls
    client.played("NA1_999", full["started_at"] + 1)
    refreshed = run(client, "full")
    # The walked history is now watermarked, so the refresh reads one page of
    # genuinely new IDs rather than paging back over 320 games it already owns.
    assert refreshed["new_games"] == 1 and client.history_calls == calls + 1
    assert state.get("ACTIVE") is None


def test_second_run_lists_only_games_newer_than_the_scanned_window(setup):
    state, commits, run = setup
    client = Histories(3)
    first = run(client, "full")
    assert (first["new_games"], first["discovered_games"]) == (3, 3)
    assert client.windows == [None] and state.get(PLAYER_MARK)["scanned_to"] == first["started_at"]
    client.played("NA1_999", first["started_at"] + 1)
    second = run(client, "full")
    # Discovery asks Riot only for the window it has never scanned, and the
    # candidate pool holds new games alone -- no request is spent re-listing or
    # re-fetching the three games already committed.
    assert client.windows[1:] == [first["started_at"]]
    assert (second["new_games"], second["discovered_games"]) == (1, 1)
    assert set(commits) == {"NA1_0", "NA1_1", "NA1_2", "NA1_999"}
    assert state.get(PLAYER_MARK)["scanned_to"] == second["started_at"]


def test_unsettled_match_holds_the_watermark_until_a_later_run_retries_it(setup, monkeypatch):
    state, commits, run = setup
    original = pipeline.ingest_match
    flaky = ["NA1_1"]

    def transient(client, region, mid, store, **kwargs):
        if mid in flaky:
            flaky.remove(mid)
            raise RiotAPIError("temporary")
        return original(client, region, mid, store, **kwargs)

    monkeypatch.setattr(pipeline, "ingest_match", transient)
    client = Histories(3)
    first = run(client, "full")
    assert (first["status"], first["stage"], first["new_games"]) == ("paused", "retry_required", 2)
    # A history holding an unaccounted match must not be watermarked past it,
    # or the retry would never be rediscovered by a later run.
    assert state.get(PLAYER_MARK) is None
    second = run(client, "full")
    assert (second["status"], second["new_games"]) == ("succeeded", 1)
    assert set(commits) == {"NA1_0", "NA1_1", "NA1_2"}
    assert state.get(PLAYER_MARK)["scanned_to"] == second["started_at"]


def test_exhaustion_reports_actual_count_and_publishes(setup):
    state, commits, run = setup
    result = run(Histories(3), "smoke")
    assert (result["status"], result["stage"], result["new_games"]) == ("paused", "exhausted", 3)
    assert state.get("PUBLISHED")["count"] == 3


def test_expiration_before_collection_requires_manual_retry(setup):
    state, commits, run = setup
    client = Histories(60)
    client.expired = True
    result = run(client, "smoke")
    assert result["status"] == "auth_required" and not commits
    assert state.get("ACTIVE") is None and client.history_calls == 0
    client.expired = False
    resumed = run(client, "smoke", result["run_id"], resume=True)
    assert resumed["status"] == "succeeded" and resumed["new_games"] == 50


def test_expiration_midrun_preserves_quota_on_manual_resume(setup, monkeypatch):
    state, commits, run = setup
    original = pipeline.ingest_match
    armed = [True]

    def expire(*args, **kwargs):
        if len(commits) == 4 and armed[0]:
            armed[0] = False
            raise AuthError("expired")
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "ingest_match", expire)
    client = Histories(70)
    first = run(client, "smoke")
    assert first["status"] == "auth_required" and first["new_games"] == 4
    resumed = run(client, "smoke", first["run_id"], resume=True)
    assert resumed["status"] == "succeeded" and len(commits) == resumed["new_games"] == 50


def test_interrupted_run_publishes_the_games_it_collected(setup, monkeypatch):
    state, commits, run = setup
    stop = threading.Event()
    original = pipeline.ingest_match

    def interrupt(*args, **kwargs):
        result = original(*args, **kwargs)
        if len(commits) == 3:
            stop.set()
        return result

    monkeypatch.setattr(pipeline, "ingest_match", interrupt)
    result = run(Histories(60), "smoke", stop=stop)
    # A stop signal ends the collecting, not the games. They are already durable
    # under the curated prefix and publication is the only step that puts them
    # in front of anyone, so an interrupt that skipped it would strand the lot.
    assert (result["status"], result["stage"]) == ("paused", "interrupted")
    assert (state.get("PUBLISHED")["count"], result["published_version"]) == (3, "test-release")
    assert state.get("ACTIVE") is None


def test_expired_key_publishes_the_games_already_collected(setup, monkeypatch):
    state, commits, run = setup
    original = pipeline.ingest_match

    def expire(*args, **kwargs):
        if len(commits) == 4:
            raise AuthError("expired")
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "ingest_match", expire)
    result = run(Histories(70), "smoke")
    # A development key dies 24 hours after it is issued, mid-run as often as
    # not. The games collected before that are unaffected by it.
    assert (result["status"], result["new_games"]) == ("auth_required", 4)
    assert (state.get("PUBLISHED")["count"], result["published_version"]) == (4, "test-release")


def test_lost_ownership_publishes_nothing_and_leaves_the_new_owner_alone(setup, monkeypatch):
    state, commits, run = setup
    original = pipeline.ingest_match

    def steal(*args, **kwargs):
        result = original(*args, **kwargs)
        if len(commits) == 2:
            state.put("ACTIVE", {"run_id": "someone-else"})
        return result

    monkeypatch.setattr(pipeline, "ingest_match", steal)
    # Salvage must not become a way for an evicted run to overwrite the release
    # or the status record that its successor now owns.
    with pytest.raises(OwnershipError):
        run(Histories(60), "smoke")
    assert state.get("PUBLISHED") is None
    assert state.get("ACTIVE")["run_id"] == "someone-else"


def test_exclusive_local_worker_ownership(tmp_path):
    one, two = LocalState(tmp_path), LocalState(tmp_path)
    one.claim("one")
    try:
        with pytest.raises(OwnershipError):
            two.claim("two")
        one.heartbeat("one")
        assert one.get("ACTIVE")["run_id"] == "one"
    finally:
        one.release("one")


def test_timeline_retry_commit_replay_and_zero_event_release(tmp_path):
    mid = "NA1_12345"
    match = make_fixture.make_match(random.Random(7), mid, patch="16.17.1", duration_s=1800)
    timeline = {"metadata": {"matchId": mid}, "info": {"frames": [{"timestamp": 0, "events": [], "participantFrames": {}}]}}
    for participant in match["info"]["participants"]:
        participant["kills"] = participant["deaths"] = 0
    client = Mock()
    client.match.return_value = match
    client.timeline.side_effect = [RiotAPIError("temporary"), timeline]
    store, site = LocalObjects(tmp_path / "store"), LocalObjects(tmp_path / "site")
    with pytest.raises(RiotAPIError):
        ingest_match(client, Region("americas"), mid, store, patches=[], run_id="first")
    assert not store.get(f"curated/v1/{mid}/complete.json")
    complete, _ = ingest_match(client, Region("americas"), mid, store, patches=[], run_id="second")
    assert complete["row_count"] == 0
    ingest_match(client, Region("americas"), mid, store, patches=[], run_id="third")
    assert client.match.call_count == 1 and client.timeline.call_count == 2
    published = build_release(store, site)
    assert (published["count"], published["rows"]) == (1, 0)
    players = json.loads(site.get(f'data/releases/{published["dataset_id"]}/players.json'))
    assert all(set(p) == {"id", "name"} for p in players)
    assert build_release(store, site)["dataset_id"] == published["dataset_id"]
    assert all(not key.endswith(".gz") for key in store.keys("curated/v1/"))
    pointer = site.get("data/current.json")
    with pytest.raises(ValidationError):
        site.put(f'data/releases/{published["dataset_id"]}/players.json', b'[]', immutable=True)
    assert site.get("data/current.json") == pointer


def test_local_api_preflight_and_concurrent_request_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_server, "ROOT", tmp_path)
    process = Mock()
    process.poll.return_value = None
    spawn = Mock(return_value=process)
    controller = dev_server.Controller(tmp_path / "state", check=lambda: "auth_required", spawn=spawn)
    failed_id = str(uuid.uuid4())
    assert controller.start({"mode": "smoke", "requestId": failed_id})["run"]["status"] == "auth_required"
    spawn.assert_not_called()
    controller.check = lambda: "ok"
    first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
    controller.start({"mode": "smoke", "requestId": first_id})
    assert controller.start({"mode": "small", "requestId": second_id})["run"]["run_id"] == first_id
    controller.state.update("RUN#" + first_id, status="succeeded")
    process.poll.return_value = 0
    assert controller.start({"mode": "small", "requestId": second_id})["run"]["run_id"] == first_id
    assert spawn.call_count == 1
