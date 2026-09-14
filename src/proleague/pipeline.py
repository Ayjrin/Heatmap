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
from collections import Counter
from dataclasses import replace

from .config import APEX_TIERS, DIVISIONS, TIER_TO_SK, TIER_UNRANKED, TIERS, Config
from .curated import (SCHEMA_VERSION, LocalObjects, S3Objects, ValidationError,
                      ingest_match, match_prefix, read_complete,
                      settled_scope, settled_status)
from .dataset import build_release
from .extract.limiter import KEY_LIMITS
from .extract.riot_client import AuthError, RiotAPIError, RiotClient
from .extract.routing import Platform, Region
from .pipeline_state import DynamoState, LocalState, OwnershipError

MODES = {"smoke": 50, "small": 250, "full": None}
TERMINAL = {"succeeded", "failed", "auth_required", "paused"}
# `capped` is a game left unfetched because its patch already holds
# max_matches_per_patch complete games. It is accounted for, so a history that
# only holds capped games may still advance its watermark.
SETTLED = {"complete", "cached", "rejected", "capped"}
PUBLIC_FIELDS = ("run_id", "mode", "status", "stage", "new_games", "target",
                 "started_at", "updated_at", "published_version", "discovered_games",
                 "players", "scanned_players", "requests_used", "request_budget",
                 "roster_players", "rank_lookups", "round")
# Frozen onto the run record so a resume continues under the settings it
# started with. key_kind is deliberately absent: a swapped key applies on resume.
SETTINGS = ("platform", "region", "tiers", "patches", "start_time_epoch", "matches_per_player",
            "request_budget", "players_per_run", "seed_pages_per_division", "rank_lookup_budget",
            "rank_ttl_seconds", "max_rounds", "max_matches_per_patch")
RANK_HISTORY_LIMIT = 8
ROSTER_FLUSH_EVERY = 50
log = logging.getLogger(__name__)


class Paused(RuntimeError):
    pass


class BudgetExhausted(Paused):
    """The run spent its request_budget. Everything collected is kept."""


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
            "auth_required": ("Riot rejected the API key. Development keys expire 24 hours after they are issued. "
                              "Get a fresh key at developer.riotgames.com and store it in the AWS SSM parameter "
                              "with scripts/aws_key.py before starting another manual run."),
            "failed": "Collection stopped. Completed games were saved; check the private run logs.",
            "paused": ("The available histories contained fewer new eligible games than the target."
                       if public.get("stage") == "exhausted" else
                       "Collection spent its request budget for this run. Completed games were published; "
                       "a resume continues the same run against a fresh budget."
                       if public.get("stage") == "budget_exhausted" else
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
    gets fresh ladder/history checkpoints, while MATCH records dedupe globally
    and PLAYER records are the durable roster: each player's rank as observed
    from the ladder or a lookup, a bounded rank history, how often they appear
    in collected games, and the window of their history already scanned.
    Raw ladder, match, and timeline responses are never written to storage.

    A full run is seed -> (frontier -> collect -> rank) x rounds. Seeding pulls
    every configured tier off the ladder with exact ranks. The frontier is the
    in-tier roster not yet listed this run, highest tier and least recently
    listed first. Collecting harvests all ten participants of every game onto
    the roster; ranking resolves the unknown ones most seen first; the next
    round's frontier is whatever that resolved into a configured tier.
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
        self.players, self.candidates, self.known, self.roster = {}, {}, {}, {}
        self.successes = set()
        self.dirty = set()
        self.context_patch = {}
        self.patch_counts = Counter()
        self.lookups = 0
        self.scope = None
        self.preflight_ladder = None
        self.hb = None

    def patch(self, **fields):
        fields["updated_at"] = now()
        self.state.update("RUN#" + self.run_id, **fields)
        self.run.update(fields)

    def check_ownership(self):
        if self.ownership_lost.is_set():
            raise OwnershipError("Collection ownership was lost")
        active = self.state.get("ACTIVE")
        if not active or active.get("run_id") != self.run_id:
            raise OwnershipError("Collection ownership was lost")

    def used(self):
        return int(getattr(self.client, "call_count", 0) or 0)

    def check(self, budget=True):
        # Ownership first: when a run has lost the lock and been interrupted,
        # the lock is the one that must not be written over.
        self.check_ownership()
        if self.stop.is_set():
            raise Paused("Collection interrupted")
        if budget and self.used() >= self.cfg.request_budget:
            raise BudgetExhausted("Collection spent its request budget")

    def heartbeat(self):
        while not self.heartbeat_stop.wait(self.heartbeat_seconds):
            try:
                self.state.heartbeat(self.run_id)
                self.state.update("RUN#" + self.run_id, heartbeat_at=now(), updated_at=now())
            except Exception:
                self.ownership_lost.set()
                return

    def mark_success(self, mid, complete, patch=None):
        item = {"match_id": mid, "status": "complete", "run_id": complete["run_id"],
                "row_count": complete["row_count"], "schema_version": SCHEMA_VERSION}
        patch = patch or (self.known.get(mid) or {}).get("patch")
        if patch:
            item["patch"] = patch
            if (self.known.get(mid) or {}).get("status") != "complete":
                self.patch_counts[patch] += 1
        self.state.put("MATCH#" + mid, item)
        self.known[mid] = item
        if complete["run_id"] == self.run_id and mid not in self.successes:
            self.state.put(f"RUNMATCH#{self.run_id}#{mid}", {"match_id": mid, "run_id": self.run_id})
            self.successes.add(mid)
            self.patch(new_games=len(self.successes))

    def recover(self):
        self.scope = settled_scope(self.cfg.patches)
        self.successes = {r["match_id"] for r in self.state.scan(f"RUNMATCH#{self.run_id}#")}
        self.known = {r["match_id"]: r for r in self.state.scan("MATCH#")}
        self.patch_counts = Counter(r["patch"] for r in self.known.values()
                                    if r.get("status") == "complete" and r.get("patch"))
        self.roster = {r["puuid"]: plain(r) for r in self.state.scan(self.player_key(""))}
        self.lookups = int(self.run.get("rank_lookups") or 0)
        mid = self.run.get("inflight_match_id")
        if mid:
            complete = read_complete(self.store, mid)
            if complete:
                self.mark_success(mid, complete)
        self.patch(new_games=len(self.successes), inflight_match_id=None)
        self.players = {r["puuid"]: plain(r) for r in self.state.scan(f"RUNPLAYER#{self.run_id}#")}
        self.candidates = {r["match_id"]: plain(r) for r in self.state.scan(f"DISCOVERY#{self.run_id}#")}

    def player_key(self, puuid):
        # Histories are regional, so a watermark only speaks for its own region.
        return f"PLAYER#{self.cfg.region}#{puuid}"

    def since(self, puuid):
        """History floor for one player: the end of the window a previous run
        finished scanning, so Riot lists only games we have never seen.

        A stored mark is honoured only when it starts at or below the configured
        floor. Widening the crawl by lowering start_time_epoch therefore rescans
        the whole window instead of silently keeping the older games hidden.
        """
        floor = self.cfg.start_time_epoch or 0
        mark = self.roster.get(puuid) or {}
        if int(mark.get("scanned_from") or 0) > floor:
            return floor
        return max(floor, int(mark.get("scanned_to") or 0))

    # -- roster ------------------------------------------------------------
    def fresh(self, record, at=None):
        return (at or now()) - int(record.get("rank_observed_at") or 0) < self.cfg.rank_ttl_seconds

    def observe_rank(self, puuid, tier, division, lp, source, at):
        """Record one rank observation in memory; return whether a write is due.

        The current rank is overwritten. The observation it replaces is pushed
        onto a bounded history only when tier or division changed, which is
        what lets a release attribute a game to the rank nearest its start
        rather than the rank at crawl time. Unchanged observations are only
        persisted once the stored one would otherwise go stale, so a 36k-player
        ladder does not cost 36k writes per run.
        """
        record = self.roster.setdefault(puuid, {"puuid": puuid})
        previous = record.get("tier")
        changed = previous != tier or record.get("division") != division
        due = changed or not self.fresh(record, at)
        if changed and previous is not None:
            history = list(record.get("rank_history") or [])
            history.append({"at": int(record.get("rank_observed_at") or 0), "tier": previous,
                            "division": record.get("division"), "lp": record.get("lp")})
            record["rank_history"] = history[-RANK_HISTORY_LIMIT:]
        record.update(tier=tier, division=division, lp=lp, rank_observed_at=int(at), rank_source=source)
        return due

    def roster_flush(self, puuids=None):
        puuids = set(self.dirty if puuids is None else puuids)
        if not puuids:
            return
        self.state.put_many([(self.player_key(p), self.roster[p]) for p in sorted(puuids) if p in self.roster])
        self.dirty -= puuids

    def harvest(self, context):
        """Every participant of a collected game joins the roster.

        This is the snowball's intake: the nine players a seed did not list are
        remembered with how often they appear, so ranking can spend its lookups
        on the players whose games we actually hold. Also vetoes a game whose
        patch is already full when max_matches_per_patch is set, before the
        timeline request is spent.
        """
        info = context.get("info") or {}
        patch = ".".join(str(info.get("gameVersion", "")).split(".")[:2])
        mid = context.get("metadata", {}).get("matchId")
        if mid:
            self.context_patch[mid] = patch
        cap = self.cfg.max_matches_per_patch
        if cap is not None and self.patch_counts[patch] >= cap and \
                (self.known.get(mid) or {}).get("status") != "complete":
            return "capped"
        for participant in info.get("participants") or []:
            puuid = participant.get("puuid")
            if not isinstance(puuid, str) or not puuid:
                continue
            record = self.roster.setdefault(puuid, {"puuid": puuid})
            record["appearances"] = int(record.get("appearances") or 0) + 1
            record.setdefault("first_seen_run", self.run_id)
            record["last_seen_run"] = self.run_id
            self.dirty.add(puuid)
        return None

    def settled(self, match_id):
        """Global dedupe. A match already committed under this schema, or already
        rejected under this patch scope, costs no Riot request in any later run."""
        return settled_status(self.known.get(match_id), self.scope)

    def advance_watermarks(self):
        """Record how far each fully walked history has been scanned.

        A player whose pool still holds an unsettled match keeps its old mark, so
        an interrupted, depth-capped, or retry-required run can never advance
        past a match ID it has not accounted for.
        """
        self.roster_flush()
        floor = self.cfg.start_time_epoch or 0
        unsettled = [c for c in self.candidates.values() if c["status"] not in SETTLED]
        if any("puuid" not in c for c in unsettled):
            return  # A run resumed from before discovery attribution; attribute nothing.
        blocked = {c["puuid"] for c in unsettled}
        records = []
        for p in self.players.values():
            if not p["done"] or p["puuid"] in blocked:
                continue
            # The roster record carries rank fields now, so the watermark is a
            # merge, never a fresh three-field record that would erase them.
            record = self.roster.setdefault(p["puuid"], {"puuid": p["puuid"]})
            record.update(scanned_from=floor, scanned_to=max(p.get("since") or 0, self.run["started_at"]),
                          listed_at=self.run["started_at"])
            records.append((self.player_key(p["puuid"]), record))
        self.state.put_many(records)

    def target_reached(self):
        return self.run["target"] is not None and len(self.successes) >= self.run["target"]

    def ladder_entry(self, entry, tier, division):
        puuid = entry.get("puuid")
        if not isinstance(puuid, str) or not puuid:
            raise ValidationError("The ladder response is missing a player PUUID")
        rank = entry.get("rank") or division
        lp = entry.get("leaguePoints")
        return puuid, tier, str(rank) if rank in DIVISIONS else division, int(lp) if isinstance(lp, int) else None

    def seed(self):
        """Put every configured tier on the roster with its exact ladder rank.

        Apex tiers come whole from league-v4; the rest are paged through
        league-exp-v4 per division (~205 entries a page) up to
        seed_pages_per_division. Each division persists its next page, so an
        interrupted seed resumes where it stopped and a finished division is
        never re-requested by the same run.
        """
        self.patch(stage="seeding")
        platform = Platform(self.cfg.platform)
        for tier in self.cfg.tiers:
            tier = tier.upper()
            if tier in APEX_TIERS:
                pk = f"LADDER#{self.run_id}#{tier}"
                if (self.state.get(pk) or {}).get("done"):
                    continue
                self.check()
                at = now()
                payload = self.preflight_ladder if tier == "CHALLENGER" else None
                if payload is None:
                    payload = self.client.apex_league(platform, tier)
                if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
                    raise RiotAPIError("The ladder response is missing entries")
                dirty = set()
                for entry in payload["entries"]:
                    puuid, *rank = self.ladder_entry(entry, tier, "I")
                    if self.observe_rank(puuid, *rank, "ladder", at):
                        dirty.add(puuid)
                self.roster_flush(dirty)
                self.state.put(pk, {"done": True, "entries": len(payload["entries"])})
                del payload
                continue
            for division in DIVISIONS:
                pk = f"LADDER#{self.run_id}#{tier}#{division}"
                marker = self.state.get(pk) or {}
                if marker.get("done"):
                    continue
                page = int(marker.get("next_page") or 1)
                while page <= self.cfg.seed_pages_per_division:
                    self.check()
                    at = now()
                    entries = self.client.league_exp_entries(platform, tier, division=division, page=page)
                    if not isinstance(entries, list):
                        raise ValidationError("The ladder page is not a list")
                    dirty = set()
                    for entry in entries:
                        puuid, _, rank, lp = self.ladder_entry(entry, tier, division)
                        # The page is the tier; an entry that disagrees is a
                        # ladder move mid-crawl and the entry's own tier wins.
                        seen = str(entry.get("tier") or tier).upper()
                        if self.observe_rank(puuid, seen if seen in TIERS else tier, rank, lp, "ladder", at):
                            dirty.add(puuid)
                    self.roster_flush(dirty)
                    page += 1
                    self.patch(roster_players=len(self.roster), requests_used=self.used())
                    if not entries:
                        break
                    self.state.put(pk, {"done": False, "next_page": page})
                self.state.put(pk, {"done": True, "next_page": page})
        self.preflight_ladder = None
        configured = {t.upper() for t in self.cfg.tiers}
        if not any(r.get("tier") in configured for r in self.roster.values()):
            raise RiotAPIError("The configured ladder has no players")
        self.patch(roster_players=len(self.roster), requests_used=self.used())

    def frontier(self, round_):
        """List the next slice of the roster as this run's players.

        Eligible: in a configured tier, rank observed within the TTL, not yet
        listed this run. Highest tier first, then least recently listed, then a
        per-run shuffle, up to players_per_run for the whole run. Written under
        a marker so a resume reuses the slice instead of drawing another.
        """
        pk = f"LADDER#{self.run_id}#FRONTIER#{round_}"
        marker = self.state.get(pk) or {}
        if marker.get("done"):
            return int(marker.get("added") or 0)
        at = now()
        configured = {t.upper() for t in self.cfg.tiers}
        remaining = max(0, self.cfg.players_per_run - len(self.players))
        eligible = [r for r in self.roster.values() if r.get("tier") in configured
                    and self.fresh(r, at) and r["puuid"] not in self.players]
        eligible.sort(key=lambda r: (TIER_TO_SK[r["tier"]], int(r.get("listed_at") or 0),
                                     hashlib.sha256((self.run_id + r["puuid"]).encode()).digest()))
        records = []
        for record in eligible[:remaining]:
            puuid = record["puuid"]
            player = {"puuid": puuid, "tier": record["tier"], "next_start": 0, "done": False,
                      "since": self.since(puuid), "round": round_}
            self.players[puuid] = player
            records.append((f"RUNPLAYER#{self.run_id}#{puuid}", player))
        self.state.put_many(records)
        self.state.put(pk, {"done": True, "added": len(records)})
        self.patch(players=len(self.players), round=round_)
        return len(records)

    def rank(self):
        """Resolve the exact rank of harvested players, most seen first.

        Candidates are roster players with no rank at all or a rank older than
        the TTL that seeding did not refresh this run (so they are off the
        ladder we page, or below the floor). A lookup with no solo-queue entry
        records UNRANKED, which is an answer and is not asked again before the
        TTL. Each lookup writes its own record: there is no scratch to resume,
        the candidate list is simply recomputed.
        """
        if self.lookups >= self.cfg.rank_lookup_budget:
            return
        self.patch(stage="ranking", rank_lookups=self.lookups)
        self.roster_flush()
        at = now()
        candidates = [r for r in self.roster.values() if r.get("tier") is None or not self.fresh(r, at)]
        candidates.sort(key=lambda r: (-int(r.get("appearances") or 0),
                                       hashlib.sha256((self.run_id + r["puuid"]).encode()).digest()))
        platform = Platform(self.cfg.platform)
        for record in candidates:
            if self.lookups >= self.cfg.rank_lookup_budget:
                break
            self.check()
            puuid = record["puuid"]
            entries = self.client.league_entries_by_puuid(platform, puuid)
            if not isinstance(entries, list):
                raise ValidationError("The league entries response is not a list")
            solo = next((e for e in entries if isinstance(e, dict) and e.get("queueType") == "RANKED_SOLO_5x5"), None)
            observed = now()
            if solo and str(solo.get("tier", "")).upper() in TIERS:
                _, tier, division, lp = self.ladder_entry(dict(solo, puuid=puuid), str(solo["tier"]).upper(), "I")
                self.observe_rank(puuid, tier, division, lp, "lookup", observed)
            else:
                self.observe_rank(puuid, TIER_UNRANKED, None, None, "lookup", observed)
            self.roster_flush([puuid])
            self.lookups += 1
            if self.lookups % ROSTER_FLUSH_EVERY == 0:
                self.patch(rank_lookups=self.lookups, requests_used=self.used())
        self.patch(rank_lookups=self.lookups, requests_used=self.used(), roster_players=len(self.roster))

    def discover(self, player):
        self.check()
        self.patch(stage="discovering")
        start = player["next_start"]
        count = min(100, self.cfg.matches_per_player - start)
        if count <= 0:
            player["done"] = True
        else:
            ids = self.client.match_ids_by_puuid(Region(self.cfg.region), player["puuid"],
                queue=420, start=start, count=count, start_time=player.get("since") or None,
                end_time=self.run["started_at"])
            if not isinstance(ids, list):
                raise ValidationError("Match history is not a list")
            records = []
            for mid in ids:
                match_prefix(mid)
                # Dedupe before the pool, not at fetch time: a match we already
                # own costs no discovery record and no place in the run's work.
                if mid not in self.candidates and not self.settled(mid):
                    candidate = {"match_id": mid, "status": "pending", "puuid": player["puuid"]}
                    self.candidates[mid] = candidate
                    records.append((f"DISCOVERY#{self.run_id}#{mid}", candidate))
            # IDs are durable before advancing the history cursor.
            self.state.put_many(records)
            player["next_start"] += len(ids)
            player["done"] = len(ids) < count or player["next_start"] >= self.cfg.matches_per_player
        self.state.put(f"RUNPLAYER#{self.run_id}#{player['puuid']}", player)
        # A refresh that finds nothing new leaves discovered_games at zero for
        # the whole stage, so walked histories carry the progress signal.
        self.patch(discovered_games=len(self.candidates),
                   scanned_players=sum(1 for p in self.players.values() if p["done"]),
                   requests_used=self.used())

    def ordered(self, values, field):
        return sorted(values, key=lambda r: hashlib.sha256(
            (self.run_id + r[field]).encode()).digest())

    def process(self, candidates):
        self.patch(stage="collecting", requests_used=self.used())
        processed = 0
        for candidate in self.ordered(candidates, "match_id"):
            if self.target_reached():
                break
            if candidate["status"] in SETTLED:
                continue
            self.check()
            mid = candidate["match_id"]
            # Discovery filters settled matches out of the pool, so this only
            # fires for records an earlier run left pending or errored.
            status = self.settled(mid)
            if status is None:
                # Save the identity before ingestion, so a commit/count crash is recoverable.
                self.patch(inflight_match_id=mid)
                try:
                    complete, why = ingest_match(self.client, Region(self.cfg.region), mid, self.store,
                        patches=self.cfg.patches, run_id=self.run_id, on_context=self.harvest)
                    # The requests are spent either way; a game that was just
                    # fetched is marked even when it was the one that tripped the
                    # budget, and the next loop check pauses the run.
                    self.check(budget=False)
                    if complete:
                        self.mark_success(mid, complete, self.context_patch.pop(mid, None))
                        status = "complete" if complete["run_id"] == self.run_id else "cached"
                    elif why == "capped":
                        status = "capped"
                    elif why in {"match_404", "timeline_404"}:
                        status = "error"  # retry on a later manual run, never discard an unfinished timeline
                    else:
                        status = "rejected"
                        item = {"match_id": mid, "status": status, "reason": why, "scope": self.scope}
                        self.state.put("MATCH#" + mid, item)
                        self.known[mid] = item
                except AuthError:
                    raise
                except RiotAPIError as exc:
                    status = "error"
                    log.warning("%s", json.dumps({"event": "match_retry_required", "run_id": self.run_id,
                                "match_id": mid, "type": type(exc).__name__}))
                self.patch(inflight_match_id=None, requests_used=self.used())
                processed += 1
                if processed % ROSTER_FLUSH_EVERY == 0:
                    self.roster_flush()
            candidate["status"] = status
            self.state.put(f"DISCOVERY#{self.run_id}#{mid}", candidate)
            if status == "complete":
                log.info("%s", json.dumps({"event": "game_complete", "run_id": self.run_id,
                         "match_id": mid, "new_games": len(self.successes), "target": self.run["target"]}))
        self.roster_flush()
        self.patch(roster_players=len(self.roster), requests_used=self.used())

    def collect(self):
        self.seed()
        if self.mode != "full":
            # Bounded modes interleave listing and fetching so the first game
            # lands without walking the whole frontier first.
            self.frontier(0)
            visited = set()
            self.process(list(self.candidates.values()))
            visited.update(self.candidates)
            for player in self.ordered(self.players.values(), "puuid"):
                while not player["done"] and not self.target_reached():
                    self.discover(player)
                    fresh = [r for mid, r in self.candidates.items() if mid not in visited]
                    self.process(fresh)
                    visited.update(self.candidates)
                if self.target_reached():
                    break
            self.rank()
            return
        for round_ in range(self.cfg.max_rounds):
            # A later round's frontier is exactly what the previous round
            # harvested and resolved into a configured tier: the snowball. An
            # empty one means nothing new to list, so the run ends there rather
            # than re-processing the pool; transient errors stay for a later run.
            if not self.frontier(round_) and round_:
                break
            for player in self.ordered(self.players.values(), "puuid"):
                while not player["done"]:
                    self.discover(player)
            self.process(list(self.candidates.values()))
            self.rank()
            if len(self.players) >= self.cfg.players_per_run:
                break

    def publish(self, guard):
        """Build and publish a release covering every curated game we can see.

        A release is the cumulative union of the curated prefix, not this run's
        haul, so this is worth doing whenever the run added anything at all.
        """
        if not any(r.get("status") == "complete" for r in self.known.values()):
            return
        self.patch(stage="publishing")
        published = self.publisher(self.store, self.site, run_id=self.run_id, before_publish=guard,
                                   roster=list(self.roster.values()))
        self.state.put("PUBLISHED", dict(published, run_id=self.run_id, updated_at=now()))
        self.patch(published_version=published["dataset_id"])

    def salvage(self, status, stage):
        """Publish before recording a terminal status that ends collection early.

        An expired key or a stop signal ends the collecting, not the games: they
        are already durable under the curated prefix, and publication is the
        only step that makes them visible. Skipping it strands every game the
        run collected until someone rebuilds by hand. The publish itself is
        atomic -- immutable objects first, pointer last -- so an attempt that is
        killed partway leaves orphaned objects and the previous release intact.
        """
        try:
            self.publish(self.check_ownership)
        except OwnershipError:
            raise
        except Exception as exc:
            log.error("%s", json.dumps({"event": "salvage_publish_failed", "run_id": self.run_id,
                      "status": status, "type": type(exc).__name__}))
        self.patch(status=status, stage=stage)

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
                            heartbeat_at=now(), settings=settings, request_budget=self.cfg.request_budget)
            self.state.put("RUN#" + self.run_id, self.run)
            self.state.put("LATEST", {"run_id": self.run_id})
            self.hb = threading.Thread(target=self.heartbeat, daemon=True, name="run-heartbeat")
            self.hb.start()
            self.recover()
            if self.client is None:
                self.client = RiotClient(load_key(self.cfg, self.cloud), limits=KEY_LIMITS[self.cfg.key_kind])
            self.check()
            self.preflight_ladder = self.client.apex_league(Platform(self.cfg.platform), "CHALLENGER")
            if not isinstance(self.preflight_ladder, dict):
                raise RiotAPIError("Riot key validation did not return a ladder")
            self.collect()
            self.check()
            self.advance_watermarks()
            self.publish(self.check)
            errors = sum(r["status"] == "error" for r in self.candidates.values())
            if self.run["target"] is not None and not self.target_reached():
                self.patch(status="paused", stage="retry_required" if errors else "exhausted")
            elif errors and self.mode == "full":
                self.patch(status="paused", stage="retry_required")
            else:
                self.patch(status="succeeded", stage="complete")
        except AuthError:
            self.salvage("auth_required", "auth_required")
        except BudgetExhausted:
            # Costs no requests, and only advances histories whose games are
            # all accounted for, so it is safe to run on the way out.
            try:
                self.advance_watermarks()
            except OwnershipError:
                raise
            except Exception as exc:
                log.error("%s", json.dumps({"event": "watermark_failed", "run_id": self.run_id,
                          "type": type(exc).__name__}))
            self.salvage("paused", "budget_exhausted")
        except Paused:
            self.salvage("paused", "interrupted")
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
