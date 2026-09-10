"""Durable run records and exclusive worker ownership, locally or in DynamoDB."""
from __future__ import annotations

import fcntl
from functools import wraps
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote


class OwnershipError(RuntimeError):
    pass


class LocalState:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.RLock()
        self._lease = None

    def _path(self, pk):
        return self.root / (quote(pk, safe="") + ".json")

    def get(self, pk):
        p = self._path(pk)
        return json.loads(p.read_text()) if p.exists() else None

    def put(self, pk, item):
        with self._mutex:
            p = self._path(pk)
            tmp = p.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(dict(item, pk=pk), separators=(",", ":")))
            os.replace(tmp, p)

    def update(self, pk, **fields):
        with self._mutex:
            self.put(pk, dict(self.get(pk) or {}, **fields))

    def put_many(self, records):
        for pk, item in records:
            self.put(pk, item)

    def delete_many(self, pks):
        with self._mutex:
            for pk in pks:
                self._path(pk).unlink(missing_ok=True)

    def scan(self, prefix):
        return [item for p in self.root.glob("*.json")
                if (item := json.loads(p.read_text())).get("pk", "").startswith(prefix)]

    def claim(self, run_id):
        self._lease = (self.root / "worker.lock").open("a+")
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lease.close()
            self._lease = None
            raise OwnershipError("Another collection is running") from exc
        # Holding flock proves any previous local ACTIVE owner has exited.
        self.put("ACTIVE", {"run_id": run_id, "heartbeat_at": int(time.time())})

    def heartbeat(self, run_id):
        with self._mutex:
            if (self.get("ACTIVE") or {}).get("run_id") != run_id:
                raise OwnershipError("Collection ownership was lost")
            self.update("ACTIVE", heartbeat_at=int(time.time()))

    def release(self, run_id):
        with self._mutex:
            if (self.get("ACTIVE") or {}).get("run_id") == run_id:
                self._path("ACTIVE").unlink(missing_ok=True)
            if self._lease is not None:
                fcntl.flock(self._lease, fcntl.LOCK_UN)
                self._lease.close()
                self._lease = None


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)
    return call


class DynamoState:
    def __init__(self, table_name, resource=None):
        if resource is None:
            import boto3
            resource = boto3.resource("dynamodb")
        self.table = resource.Table(table_name)
        self._mutex = threading.RLock()

    @_serialized
    def get(self, pk):
        return self.table.get_item(Key={"pk": pk}, ConsistentRead=True).get("Item")

    @_serialized
    def put(self, pk, item):
        self.table.put_item(Item=dict(item, pk=pk))

    @_serialized
    def update(self, pk, **fields):
        self.table.update_item(
            Key={"pk": pk},
            UpdateExpression="SET " + ", ".join(f"#n{i}=:v{i}" for i in range(len(fields))),
            ExpressionAttributeNames={f"#n{i}": k for i, k in enumerate(fields)},
            ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(fields.values())},
        )

    @_serialized
    def put_many(self, records):
        with self.table.batch_writer() as batch:
            for pk, item in records:
                batch.put_item(Item=dict(item, pk=pk))

    @_serialized
    def delete_many(self, pks):
        with self.table.batch_writer() as batch:
            for pk in pks:
                batch.delete_item(Key={"pk": pk})

    @_serialized
    def scan(self, prefix):
        kwargs = {"FilterExpression": "begins_with(pk, :prefix)",
                  "ExpressionAttributeValues": {":prefix": prefix}, "ConsistentRead": True}
        out = []
        while True:
            page = self.table.scan(**kwargs)
            out.extend(page.get("Items", []))
            if not page.get("LastEvaluatedKey"):
                return out
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    @_serialized
    def claim(self, run_id):
        from botocore.exceptions import ClientError
        try:
            self.table.update_item(
                Key={"pk": "ACTIVE"},
                UpdateExpression="SET run_id=:run, heartbeat_at=:now",
                ConditionExpression="attribute_not_exists(pk) OR run_id=:run",
                ExpressionAttributeValues={":run": run_id, ":now": int(time.time())},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise OwnershipError("Another collection owns ACTIVE") from exc
            raise

    @_serialized
    def heartbeat(self, run_id):
        from botocore.exceptions import ClientError
        try:
            self.table.update_item(
                Key={"pk": "ACTIVE"}, UpdateExpression="SET heartbeat_at=:now",
                ConditionExpression="run_id=:run",
                ExpressionAttributeValues={":run": run_id, ":now": int(time.time())},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise OwnershipError("Collection ownership was lost") from exc
            raise

    @_serialized
    def release(self, run_id):
        from botocore.exceptions import ClientError
        try:
            self.table.delete_item(Key={"pk": "ACTIVE"}, ConditionExpression="run_id=:run",
                                   ExpressionAttributeValues={":run": run_id})
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
