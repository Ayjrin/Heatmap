"""ETL storage: selected match context and validated per-match Parquet facts.

Raw API responses exist only in the worker's memory. A complete.json object
commits the context/fact pair; readers ignore unfinished pairs.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path

import duckdb

from .config import MIN_GAME_DURATION_S, SOLO_QUEUE_ID, SR_MAP_ID
from .transform.kills import COLUMNS, transform_match

SCHEMA_VERSION = 1
CURATED_PREFIX = "curated/v1/"
IDENTITY_COLUMNS = [("match_id", "VARCHAR"), ("frame_index", "BIGINT"),
                    ("event_index", "BIGINT"), ("event_ms", "BIGINT"),
                    ("victim_pid", "BIGINT"), ("killer_pid", "BIGINT")]
FACT_COLUMNS = IDENTITY_COLUMNS + [(name, "BIGINT") for name, _ in COLUMNS
                                   if name not in {"match_sk", "patch_sk", "victim_player", "killer_player"}]


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def sql_path(path):
    return "'" + str(path).replace("'", "''") + "'"


class ValidationError(RuntimeError):
    pass


class LocalObjects:
    def __init__(self, root: Path):
        self.root = Path(root)

    def get(self, key):
        p = self.root / key
        return p.read_bytes() if p.exists() else None

    def put(self, key, value, *, immutable=False, content_type=None, encoding=None):
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=p.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(value)
                out.flush()
                os.fsync(out.fileno())
            if immutable:
                try:
                    os.link(name, p)
                except FileExistsError:
                    if p.read_bytes() != value:
                        raise ValidationError(f"Immutable object conflict: {key}")
                    return False
            else:
                os.replace(name, p)
            return True
        finally:
            Path(name).unlink(missing_ok=True)

    def keys(self, prefix):
        parent = self.root / prefix
        if parent.is_dir():
            return sorted(str(p.relative_to(self.root)) for p in parent.rglob("*") if p.is_file())
        return []


class S3Objects:
    def __init__(self, bucket, client=None):
        if client is None:
            import boto3
            client = boto3.client("s3")
        self.client, self.bucket = client, bucket

    def get(self, key):
        from botocore.exceptions import ClientError
        try:
            with self.client.get_object(Bucket=self.bucket, Key=key)["Body"] as body:
                return body.read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
                return None
            raise

    def put(self, key, value, *, immutable=False, content_type=None, encoding=None):
        from botocore.exceptions import ClientError
        opts = {"Bucket": self.bucket, "Key": key, "Body": value,
                "ContentType": content_type or "application/octet-stream",
                "CacheControl": "public,max-age=31536000,immutable" if immutable else "no-cache"}
        if immutable:
            opts["IfNoneMatch"] = "*"
        if encoding:
            opts["ContentEncoding"] = encoding
        try:
            self.client.put_object(**opts)
            return True
        except ClientError as exc:
            if immutable and exc.response["Error"]["Code"] in ("PreconditionFailed", "412"):
                if self.get(key) != value:
                    raise ValidationError(f"Immutable object conflict: {key}")
                return False
            raise

    def keys(self, prefix):
        return [o["Key"] for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix) for o in page.get("Contents", [])]


def game_duration_seconds(info):
    duration = float(info.get("gameDuration") or 0)
    return duration if info.get("gameEndTimestamp") is not None else duration / 1000


def rejection(match, patches):
    info = match.get("info") or {}
    if info.get("mapId") != SR_MAP_ID:
        return "wrong_map"
    if info.get("queueId") != SOLO_QUEUE_ID:
        return "wrong_queue"
    if info.get("endOfGameResult") not in (None, "GameComplete"):
        return "not_complete"
    if any(p.get("teamEarlySurrendered") for p in info.get("participants", [])):
        return "early_surrender"
    if game_duration_seconds(info) < MIN_GAME_DURATION_S:
        return "too_short"
    if len(info.get("participants") or []) != 10:
        return "bad_participant_count"
    if patches and ".".join(str(info.get("gameVersion", "")).split(".")[:2]) not in patches:
        return "out_of_patch"
    return None


def match_prefix(match_id):
    if not re.fullmatch(r"[A-Z0-9]+_[0-9]+", match_id):
        raise ValidationError("Invalid match ID")
    return f"{CURATED_PREFIX}{match_id}/"


def settled_scope(patches):
    """Identity of the acceptance rules a rejection was decided under.

    A rejection is only reusable while both the patch filter and the schema
    hold; widening `patches` produces a new scope, so the game is re-examined.
    """
    return digest(json_bytes({"patches": list(patches), "schema": SCHEMA_VERSION}))


def settled_status(known, scope):
    """Return why a MATCH record already answers for a game, or None.

    Global dedupe across every run: a game committed under this schema, or
    rejected under this scope, must never cost another Riot request.
    """
    known = known or {}
    if known.get("status") == "complete" and known.get("schema_version") == SCHEMA_VERSION:
        return "cached"
    if known.get("status") == "rejected" and known.get("scope") == scope:
        return "rejected"
    return None


def normalize_context(match, match_id, *, source_kind, run_id):
    """Retain only dimensions, acceptance evidence, and reconciliation totals."""
    match_prefix(match_id)
    if match.get("metadata", {}).get("matchId") != match_id:
        raise ValidationError("Match response identity does not match the requested game")
    info = match.get("info") or {}
    participants = info.get("participants") or []
    if len(participants) != 10 or {p.get("participantId") for p in participants} != set(range(1, 11)):
        raise ValidationError("A ranked match must contain ten unique participant IDs")
    if any(p.get("teamId") not in (100, 200) or not p.get("puuid") for p in participants):
        raise ValidationError("Participant team or PUUID is missing")
    if sum(p["teamId"] == 100 for p in participants) != 5:
        raise ValidationError("A ranked match must contain five participants per team")
    fields = ("participantId", "puuid", "championId", "teamId", "teamPosition",
              "individualPosition", "riotIdGameName", "riotIdTagline", "win", "kills", "deaths")
    selected = {key: info[key] for key in ("gameVersion", "gameCreation", "gameStartTimestamp",
                "gameEndTimestamp", "gameDuration", "queueId", "mapId", "endOfGameResult") if key in info}
    selected["participants"] = [{k: p[k] for k in fields if k in p} for p in participants]
    return {"metadata": {"matchId": match_id}, "info": selected,
            "source_kind": source_kind, "schema_version": SCHEMA_VERSION,
            "collected_at": int(time.time()), "run_id": run_id}


def transform_validated(context, timeline):
    mid = context["metadata"]["matchId"]
    if timeline.get("metadata", {}).get("matchId") != mid:
        raise ValidationError("Timeline response identity does not match its context")
    info = timeline.get("info") or {}
    frames = info.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValidationError("Timeline frames are missing")
    times = [f.get("timestamp") for f in frames]
    if any(not isinstance(t, (int, float)) or not math.isfinite(t) for t in times) or times != sorted(times):
        raise ValidationError("Timeline frame timestamps are invalid or unordered")
    pids = {p["participantId"] for p in context["info"]["participants"]}
    kills = []
    for fi, frame in enumerate(frames):
        for ei, ev in enumerate(frame.get("events") or []):
            if ev.get("type") != "CHAMPION_KILL":
                continue
            if ev.get("victimId") not in pids or (ev.get("killerId") or 0) not in pids | {0}:
                raise ValidationError("Kill refers to an unknown participant")
            position = ev.get("position") or {}
            if any(not isinstance(position.get(k), (int, float)) or not math.isfinite(position[k])
                   or not -32767 <= position[k] <= 32767 for k in ("x", "y")):
                raise ValidationError("Kill has missing or unrepresentable coordinates")
            if not isinstance(ev.get("timestamp"), (int, float)) or not 0 <= ev["timestamp"] < 65_535_000:
                raise ValidationError("Kill timestamp is invalid")
            kills.append((fi, ei))
    rows = transform_match(context, timeline, 0, 0, {})
    ids = {(r["frame_index"], r["event_index"]) for r in rows}
    if len(rows) != len(kills) or ids != set(kills):
        raise ValidationError("Source kills do not reconcile with transformed facts")
    limits = {"u8": (0, 255), "u16": (0, 65535), "u32": (0, 2**32 - 1), "i16": (-32768, 32767)}
    for row in rows:
        for name, kind in COLUMNS:
            lo, hi = limits[kind]
            if not isinstance(row[name], int) or not lo <= row[name] <= hi:
                raise ValidationError(f"Fact column {name} cannot be represented")
    deaths = [p.get("deaths") for p in context["info"]["participants"]]
    if all(isinstance(n, int) for n in deaths) and sum(deaths) != len(rows):
        raise ValidationError("Participant deaths do not reconcile with timeline kills")
    return rows, {"source_kill_events": len(kills), "fact_rows": len(rows), "duplicate_fact_ids": 0}


def encode_parquet(rows):
    with tempfile.TemporaryDirectory() as temp:
        p = Path(temp) / "facts.parquet"
        con = duckdb.connect()
        try:
            con.execute("CREATE TABLE facts (" + ",".join(f"{n} {t}" for n, t in FACT_COLUMNS) + ")")
            if rows:
                con.executemany("INSERT INTO facts VALUES (" + ",".join("?" for _ in FACT_COLUMNS) + ")",
                                [[r[n] for n, _ in FACT_COLUMNS] for r in rows])
            con.execute(f"COPY facts TO {sql_path(p)} (FORMAT PARQUET, COMPRESSION ZSTD)")
            return p.read_bytes()
        finally:
            con.close()


def read_complete(store, match_id):
    raw = store.get(match_prefix(match_id) + "complete.json")
    return json.loads(raw) if raw is not None else None


def ingest_match(client, region, match_id, store, *, patches, run_id, source_kind="riot"):
    """Return (completion, rejection). Finished games incur no Riot requests."""
    prefix = match_prefix(match_id)
    complete = read_complete(store, match_id)
    if complete is not None:
        return complete, None
    context_bytes = store.get(prefix + "context.json")
    if context_bytes is None:
        match = client.match(region, match_id)
        if match is None:
            return None, "match_404"
        why = rejection(match, patches)
        if why:
            return None, why
        context = normalize_context(match, match_id, source_kind=source_kind, run_id=run_id)
        context_bytes = json_bytes(context)
        store.put(prefix + "context.json", context_bytes, immutable=True)
        del match
        # The object wins if a previous interrupted worker already persisted it.
        context_bytes = store.get(prefix + "context.json")
    context = json.loads(context_bytes)
    if context.get("source_kind") != source_kind or context.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError("Stored match context has incompatible provenance or schema")
    # An orphaned facts object contains validated output. Its commit metadata is
    # written first as a small recovery record, avoiding another timeline call.
    pending_bytes = store.get(prefix + "validated.json")
    fact_bytes = store.get(prefix + "facts.parquet") if pending_bytes else None
    if pending_bytes and fact_bytes:
        pending = json.loads(pending_bytes)
        if pending["facts_sha256"] != digest(fact_bytes) or pending["context_sha256"] != digest(context_bytes):
            raise ValidationError("An interrupted match has inconsistent stored objects")
        store.put(prefix + "complete.json", pending_bytes, immutable=True)
        return pending, None
    timeline = client.timeline(region, match_id)
    if timeline is None:
        return None, "timeline_404"
    rows, validation = transform_validated(context, timeline)
    del timeline
    fact_bytes = encode_parquet(rows)
    complete = {"match_id": match_id, "source_kind": source_kind, "schema_version": SCHEMA_VERSION,
                "run_id": run_id, "row_count": len(rows), "collected_at": int(time.time()),
                "context_sha256": digest(context_bytes), "facts_sha256": digest(fact_bytes),
                "validation": validation}
    # Recovery descriptor -> facts -> commit. Only complete.json is discoverable
    # by release builds. Conditional writes prevent replacing canonical data.
    existing_validation = store.get(prefix + "validated.json")
    if existing_validation is not None:
        old = json.loads(existing_validation)
        if old["facts_sha256"] != digest(fact_bytes) or old["context_sha256"] != digest(context_bytes):
            raise ValidationError("Retry generated different facts for the same schema version")
        encoded, complete = existing_validation, old
    else:
        encoded = json_bytes(complete)
        store.put(prefix + "validated.json", encoded, immutable=True)
    store.put(prefix + "facts.parquet", fact_bytes, immutable=True)
    if digest(store.get(prefix + "facts.parquet")) != complete["facts_sha256"]:
        raise ValidationError("Stored facts failed their integrity check")
    store.put(prefix + "complete.json", encoded, immutable=True)
    return complete, None
