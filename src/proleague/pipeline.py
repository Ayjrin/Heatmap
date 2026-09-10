"""Manual, resumable Riot ETL collection, locally or in one Fargate task."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import threading
import time
import uuid
from dataclasses import replace

from .config import Config
from .curated import (SCHEMA_VERSION, LocalObjects, S3Objects, ValidationError,
                      ingest_match, json_bytes, match_prefix, read_complete)
from .dataset import build_release
from .extract.riot_client import AuthError, RiotAPIError, RiotClient
from .extract.routing import Platform, Region
from .pipeline_state import DynamoState, LocalState, OwnershipError

MODES = {"smoke": 50, "small": 250, "full": None}
TERMINAL = {"succeeded", "failed", "auth_required", "paused"}
PUBLIC_FIELDS = ("run_id", "mode", "status", "stage", "new_games", "target",
                 "started_at", "updated_at", "published_version", "discovered_games")
SETTINGS = ("platform", "region", "tiers", "patches", "start_time_epoch", "matches_per_player")
log = logging.getLogger(__name__)


class Paused(RuntimeError):
    pass


def now():
    return int(time.time())


def plain(value):
    # DynamoDB deserializes integral numbers as Decimal.
    return json.loads(json.dumps(value, default=int))


def snapshot(state):
    pointer = state.get("ACTIVE") or state.get("LATEST")
    run = state.get("RUN#" + pointer["run_id"]) if pointer else None
    public = {k: run[k] for k in PUBLIC_FIELDS if k in run} if run else None
    if public and public["status"] in {"auth_required", "failed", "paused"}:
        public["error"] = {
            "auth_required": "The Riot API key needs to be added or refreshed before another manual run.",
            "failed": "Collection stopped. Completed games were saved; check the private run logs.",
            "paused": ("The available histories contained fewer new eligible games than the target."
                       if public.get("stage") == "exhausted" else
                       "Collection paused. Completed games were saved and can be resumed manually."),
        }[public["status"]]
    published = state.get("PUBLISHED")
    dataset = {k: published[k] for k in ("dataset_id", "count") if k in published} if published else None
    return plain({"run": public, "dataset": dataset})


def load_key(cfg, cloud, ssm=None):
    if not cloud:
        if not cfg.api_key:
            raise AuthError("RIOT_API_KEY is missing")
        return cfg.api_key
    if ssm is None:
        import boto3
        ssm = boto3.client("ssm")
    from botocore.exceptions import ClientError
    name = os.environ.get("SSM_PARAMETER")
    if not name:
        raise AuthError("SSM_PARAMETER is missing")
    try:
        key = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"].strip()
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ParameterNotFound":
            raise AuthError("The Riot API key has not been configured") from None
        raise
    if not key:
        raise AuthError("The Riot API key is empty")
    return key


def runtime(cfg, local=False):
    cloud = not local and bool(os.environ.get("DATA_BUCKET"))
    if cloud:
        if not all(os.environ.get(k) for k in ("SITE_BUCKET", "STATE_TABLE")):
            raise ValueError("Cloud mode requires DATA_BUCKET, SITE_BUCKET, and STATE_TABLE")
        return (DynamoState(os.environ["STATE_TABLE"]), S3Objects(os.environ["DATA_BUCKET"]),
                S3Objects(os.environ["SITE_BUCKET"]), True)
    return (LocalState(cfg.paths.data / "state"), LocalObjects(cfg.paths.data),
            LocalObjects(cfg.paths.root / "web"), False)


class Collection:
    """Run ownership protects checkpoints and publication from concurrent workers.

    RUNMATCH records reconcile counts after an interrupted write. Each new run
    gets fresh ladder/history checkpoints, while MATCH records dedupe globally.
    Raw ladder, match, and timeline responses are never written to storage.
    """

    def __init__(self, cfg, state, store, site, *, run_id, mode, client=None,
                 cloud=False, resume=False, stop=None, heartbeat_seconds=20,
                 publisher=build_release):
        self.cfg, self.state, self.store, self.site = cfg, state, store, site
        self.run_id, self.mode = run_id, mode
        self.client, self.cloud, self.resume = client, cloud, resume
        self.stop = stop or threading.Event()
        self.heartbeat_seconds, self.publisher = heartbeat_seconds, publisher
        self.heartbeat_stop = threading.Event()
        self.ownership_lost = threading.Event()
        self.run = {}
        self.players, self.candidates, self.known = {}, {}, {}
        self.successes = set()
        self.preflight_ladder = None
        self.hb = None

    def patch(self, **fields):
        fields["updated_at"] = now()
        self.state.update("RUN#" + self.run_id, **fields)
        self.run.update(fields)

    def check(self):
        if self.ownership_lost.is_set():
            raise OwnershipError("Collection ownership was lost")
        if self.stop.is_set():
            raise Paused("Collection interrupted")
        active = self.state.get("ACTIVE")
        if not active or active.get("run_id") != self.run_id:
            raise OwnershipError("Collection ownership was lost")

    def heartbeat(self):
        while not self.heartbeat_stop.wait(self.heartbeat_seconds):
            try:
                self.state.heartbeat(self.run_id)
                self.state.update("RUN#" + self.run_id, heartbeat_at=now(), updated_at=now())
            except Exception:
                self.ownership_lost.set()
                return

    def mark_success(self, mid, complete):
        item = {"match_id": mid, "status": "complete", "run_id": complete["run_id"],
                "row_count": complete["row_count"], "schema_version": SCHEMA_VERSION}
        self.state.put("MATCH#" + mid, item)
        self.known[mid] = item
        if complete["run_id"] == self.run_id and mid not in self.successes:
            self.state.put(f"RUNMATCH#{self.run_id}#{mid}", {"match_id": mid, "run_id": self.run_id})
            self.successes.add(mid)
            self.patch(new_games=len(self.successes))

    def recover(self):
        self.successes = {r["match_id"] for r in self.state.scan(f"RUNMATCH#{self.run_id}#")}
        self.known = {r["match_id"]: r for r in self.state.scan("MATCH#")}
        mid = self.run.get("inflight_match_id")
        if mid:
            complete = read_complete(self.store, mid)
            if complete:
                self.mark_success(mid, complete)
        self.patch(new_games=len(self.successes), inflight_match_id=None)
        self.players = {r["puuid"]: plain(r) for r in self.state.scan(f"RUNPLAYER#{self.run_id}#")}
        self.candidates = {r["match_id"]: plain(r) for r in self.state.scan(f"DISCOVERY#{self.run_id}#")}

    def target_reached(self):
        return self.run["target"] is not None and len(self.successes) >= self.run["target"]

    def load_ladder(self):
        self.patch(stage="ladder")
        for tier in self.cfg.tiers:
            self.check()
            tier = tier.upper()
            pk = f"LADDER#{self.run_id}#{tier}"
            if (self.state.get(pk) or {}).get("done"):
                continue
            payload = (self.preflight_ladder if tier == "CHALLENGER" else None)
            if payload is None:
                payload = self.client.apex_league(Platform(self.cfg.platform), tier)
            if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
                raise RiotAPIError("The ladder response is missing entries")
            records = []
            for entry in payload["entries"]:
                puuid = entry.get("puuid")
                if not isinstance(puuid, str) or not puuid:
                    raise ValidationError("The ladder response is missing a player PUUID")
                if puuid in self.players:
                    continue
                player = {"puuid": puuid, "tier": tier, "next_start": 0, "done": False}
                self.players[puuid] = player
                records.append((f"RUNPLAYER#{self.run_id}#{puuid}", player))
            self.state.put_many(records)
            self.state.put(pk, {"done": True})
            del payload
        self.preflight_ladder = None
        if not self.players:
            raise RiotAPIError("The configured ladder has no players")
        self.patch(players=len(self.players))

    def discover(self, player):
        self.check()
        self.patch(stage="discovering")
        start = player["next_start"]
        count = min(100, self.cfg.matches_per_player - start)
        if count <= 0:
            player["done"] = True
        else:
            ids = self.client.match_ids_by_puuid(Region(self.cfg.region), player["puuid"],
                queue=420, start=start, count=count, start_time=self.cfg.start_time_epoch,
                end_time=self.run["started_at"])
            if not isinstance(ids, list):
                raise ValidationError("Match history is not a list")
            records = []
            for mid in ids:
                match_prefix(mid)
                if mid not in self.candidates:
                    candidate = {"match_id": mid, "status": "pending"}
                    self.candidates[mid] = candidate
                    records.append((f"DISCOVERY#{self.run_id}#{mid}", candidate))
            # IDs are durable before advancing the history cursor.
            self.state.put_many(records)
            player["next_start"] += len(ids)
            player["done"] = len(ids) < count or player["next_start"] >= self.cfg.matches_per_player
        self.state.put(f"RUNPLAYER#{self.run_id}#{player['puuid']}", player)
        self.patch(discovered_games=len(self.candidates))

    def ordered(self, values, field):
        return sorted(values, key=lambda r: hashlib.sha256(
            (self.run_id + r[field]).encode()).digest())

    def process(self, candidates):
        self.patch(stage="collecting")
        scope = hashlib.sha256(json_bytes({"patches": self.cfg.patches, "schema": SCHEMA_VERSION})).hexdigest()
        for candidate in self.ordered(candidates, "match_id"):
            if self.target_reached():
                return
            if candidate["status"] in {"complete", "cached", "rejected"}:
                continue
            self.check()
            mid = candidate["match_id"]
            known = self.known.get(mid, {})
            if known.get("status") == "complete" and known.get("schema_version") == SCHEMA_VERSION:
                status = "cached"
            elif known.get("status") == "rejected" and known.get("scope") == scope:
                status = "rejected"
            else:
                # Save the identity before ingestion, so a commit/count crash is recoverable.
                self.patch(inflight_match_id=mid)
                try:
                    complete, why = ingest_match(self.client, Region(self.cfg.region), mid, self.store,
                        patches=self.cfg.patches, run_id=self.run_id)
                    self.check()
                    if complete:
                        self.mark_success(mid, complete)
                        status = "complete" if complete["run_id"] == self.run_id else "cached"
                    elif why in {"match_404", "timeline_404"}:
                        status = "error"  # retry on a later manual run, never discard an unfinished timeline
                    else:
                        status = "rejected"
                        item = {"match_id": mid, "status": status, "reason": why, "scope": scope}
                        self.state.put("MATCH#" + mid, item)
                        self.known[mid] = item
                except AuthError:
                    raise
                except RiotAPIError as exc:
                    status = "error"
                    log.warning("%s", json.dumps({"event": "match_retry_required", "run_id": self.run_id,
                                "match_id": mid, "type": type(exc).__name__}))
                self.patch(inflight_match_id=None)
            candidate["status"] = status
            self.state.put(f"DISCOVERY#{self.run_id}#{mid}", candidate)
            if status == "complete":
                log.info("%s", json.dumps({"event": "game_complete", "run_id": self.run_id,
                         "match_id": mid, "new_games": len(self.successes), "target": self.run["target"]}))

    def collect(self):
        self.load_ladder()
        # A retry visits prior transient failures once, then continues its cursor.
        visited = set()
        if self.mode != "full":
            self.process(list(self.candidates.values()))
            visited.update(self.candidates)
        for player in self.ordered(self.players.values(), "puuid"):
            while not player["done"] and not self.target_reached():
                self.discover(player)
                if self.mode != "full":
                    fresh = [r for mid, r in self.candidates.items() if mid not in visited]
                    self.process(fresh)
                    visited.update(self.candidates)
            if self.target_reached():
                break
        if self.mode == "full":
            self.process(list(self.candidates.values()))

    def execute(self):
        saved = self.state.get("RUN#" + self.run_id)
        if saved and saved.get("status") == "succeeded":
            return plain(saved)
        if saved and saved.get("mode") != self.mode:
            raise ValueError("A run's collection mode cannot change")
        if self.resume and not saved:
            raise ValueError("The requested run does not exist")
        self.state.claim(self.run_id)
        try:
            settings = plain((saved or {}).get("settings") or {k: getattr(self.cfg, k) for k in SETTINGS})
            self.cfg = replace(self.cfg, **settings)
            self.run = plain(saved or {"run_id": self.run_id, "mode": self.mode,
                              "target": MODES[self.mode], "new_games": 0, "started_at": now()})
            self.run.update(status="running", stage="preflight", updated_at=now(),
                            heartbeat_at=now(), settings=settings)
            self.state.put("RUN#" + self.run_id, self.run)
            self.state.put("LATEST", {"run_id": self.run_id})
            self.hb = threading.Thread(target=self.heartbeat, daemon=True, name="run-heartbeat")
            self.hb.start()
            self.recover()
            if self.client is None:
                self.client = RiotClient(load_key(self.cfg, self.cloud))
            self.check()
            self.preflight_ladder = self.client.apex_league(Platform(self.cfg.platform), "CHALLENGER")
            if not isinstance(self.preflight_ladder, dict):
                raise RiotAPIError("Riot key validation did not return a ladder")
            self.collect()
            self.check()
            if any(r.get("status") == "complete" for r in self.known.values()):
                self.patch(stage="publishing")
                published = self.publisher(self.store, self.site, run_id=self.run_id, before_publish=self.check)
                self.state.put("PUBLISHED", dict(published, run_id=self.run_id, updated_at=now()))
                self.patch(published_version=published["dataset_id"])
            errors = sum(r["status"] == "error" for r in self.candidates.values())
            if self.run["target"] is not None and not self.target_reached():
                self.patch(status="paused", stage="retry_required" if errors else "exhausted")
            elif errors and self.mode == "full":
                self.patch(status="paused", stage="retry_required")
            else:
                self.patch(status="succeeded", stage="complete")
        except AuthError:
            self.patch(status="auth_required", stage="auth_required")
        except Paused:
            self.patch(status="paused", stage="interrupted")
        except OwnershipError:
            # Another owner can now control state and publication; do not overwrite it.
            raise
        except Exception as exc:
            if self.run:
                self.patch(status="failed", error_type=type(exc).__name__)
            log.error("%s", json.dumps({"event": "collection_failed", "run_id": self.run_id,
                       "stage": self.run.get("stage"), "type": type(exc).__name__}))
        finally:
            self.heartbeat_stop.set()
            if self.hb:
                self.hb.join(timeout=10)
            self.state.release(self.run_id)
        return plain(self.run)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--run-id", type=lambda value: str(uuid.UUID(value)))
    parser.add_argument("--resume", type=lambda value: str(uuid.UUID(value)))
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = Config.load()
    state, store, site, cloud = runtime(cfg, args.local)
    if args.status:
        print(json.dumps(snapshot(state)))
        return 0
    if args.run_id and args.resume:
        parser.error("Use --run-id or --resume, not both")
    saved = state.get("RUN#" + args.resume) if args.resume else None
    if args.resume and not saved:
        parser.error("The requested run does not exist")
    mode = args.mode or (saved or {}).get("mode")
    if not mode:
        parser.error("--mode is required for a new collection")
    stop = threading.Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: stop.set())
    collection = Collection(cfg, state, store, site, run_id=args.resume or args.run_id or str(uuid.uuid4()),
                            mode=mode, resume=bool(args.resume), cloud=cloud, stop=stop)
    try:
        result = collection.execute()
    except (OwnershipError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps({"event": "run_finished", **{k: result[k] for k in PUBLIC_FIELDS if k in result}}))
    return 0 if result["status"] == "succeeded" else 3 if result["status"] == "auth_required" else 1


if __name__ == "__main__":
    raise SystemExit(main())
