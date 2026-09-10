"""Public, fixed-mode launcher. Credentials and AWS exceptions never leave Lambda.

RunTask's token and entire request are durable before the first attempt. GET
reconciles an uncertain launch using that exact request while the ACTIVE lock
continues to exclude other runs. ECS STOPPED events release abandoned ownership.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

MODES = {"smoke": 50, "small": 250, "full": None}
TERMINAL = {"auth_required", "failed", "succeeded", "paused"}
# discovered_games is the only sign of life during a full run's discovery
# stage, which reads every ladder player's history before fetching one game.
# Without it the page shows "0 new games" for half an hour and looks stuck.
PUBLIC_FIELDS = ("run_id", "mode", "status", "stage", "new_games", "target",
                 "started_at", "updated_at", "published_version", "discovered_games")
ERRORS = {
    "auth_required": "The Riot API key needs to be added or refreshed before another manual run.",
    "failed": "Collection stopped. Completed games were saved; check the private run logs.",
    "paused": "Collection paused. Completed games were saved and can be resumed manually.",
}


class Store:
    def __init__(self, name):
        self.name = name
        self.table = boto3.resource("dynamodb").Table(name)

    def get(self, pk):
        return self.table.get_item(Key={"pk": pk}, ConsistentRead=True).get("Item")

    def reserve(self, run, request_id):
        serializer = TypeSerializer()
        records = [
            {"pk": "ACTIVE", "run_id": run["run_id"], "heartbeat_at": run["started_at"]},
            {"pk": "RUN#" + run["run_id"], **run},
            {"pk": "REQUEST#" + request_id, "run_id": run["run_id"], "mode": run["mode"]},
            {"pk": "LATEST", "run_id": run["run_id"]},
        ]
        writes = []
        for record in records:
            put = {"TableName": self.name,
                   "Item": {key: serializer.serialize(value) for key, value in record.items()}}
            if record["pk"] != "LATEST":
                put["ConditionExpression"] = "attribute_not_exists(pk)"
            writes.append({"Put": put})
        try:
            # A fresh low-level client avoids the resource serializer re-encoding values.
            boto3.client("dynamodb").transact_write_items(TransactItems=writes)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                return False
            raise

    def bind_request(self, request_id, run_id, mode):
        """A click made while busy remains attached to that run on every retry."""
        serializer = TypeSerializer()
        try:
            boto3.client("dynamodb").transact_write_items(TransactItems=[
                {"ConditionCheck": {"TableName": self.name, "Key": {"pk": {"S": "ACTIVE"}},
                    "ConditionExpression": "run_id = :run", "ExpressionAttributeValues": {":run": {"S": run_id}}}},
                {"Put": {"TableName": self.name, "ConditionExpression": "attribute_not_exists(pk)",
                    "Item": {key: serializer.serialize(value) for key, value in {
                        "pk": "REQUEST#" + request_id, "run_id": run_id, "mode": mode}.items()}}},
            ])
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                return False
            raise

    def patch_starting(self, run_id, *, expected_stage=None, **values):
        names = {"#status": "status"}
        attrs = {":starting": "starting"}
        updates = []
        for index, (name, value) in enumerate(values.items()):
            names[f"#n{index}"] = name
            attrs[f":v{index}"] = value
            updates.append(f"#n{index} = :v{index}")
        condition = "#status = :starting"
        if expected_stage is not None:
            condition += " AND #stage = :expected_stage"
            names["#stage"] = "stage"
            attrs[":expected_stage"] = expected_stage
        try:
            self.table.update_item(
                Key={"pk": "RUN#" + run_id},
                UpdateExpression="SET " + ", ".join(updates),
                ConditionExpression=condition,
                ExpressionAttributeNames=names, ExpressionAttributeValues=attrs,
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def attach_task(self, run_id, task_arn):
        try:
            self.table.update_item(
                Key={"pk": "ACTIVE"},
                UpdateExpression="SET task_arn = :task, heartbeat_at = :now",
                ConditionExpression="run_id = :run",
                ExpressionAttributeValues={":task": task_arn, ":now": int(time.time()), ":run": run_id},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def release(self, run_id):
        try:
            self.table.delete_item(Key={"pk": "ACTIVE"}, ConditionExpression="run_id = :run",
                                   ExpressionAttributeValues={":run": run_id})
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def stopped(self, run_id):
        try:
            self.table.update_item(
                Key={"pk": "RUN#" + run_id},
                UpdateExpression="SET #status = :failed, stage = :stage, updated_at = :now",
                ConditionExpression="#status IN (:starting, :running)",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":failed": "failed", ":stage": "task_stopped",
                    ":now": int(time.time()), ":starting": "starting", ":running": "running"},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        self.release(run_id)


def preflight(ssm):
    """Do not log the key, HTTP request, response body, or AWS error messages."""
    try:
        key = ssm.get_parameter(Name=os.environ["SSM_PARAMETER"], WithDecryption=True)["Parameter"]["Value"].strip()
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ParameterNotFound":
            return "auth_required"
        return "temporary"
    if not key:
        return "auth_required"
    request = urllib.request.Request(
        "https://na1.api.riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5",
        headers={"X-Riot-Token": key, "User-Agent": "ProLeagueHeatmap/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return "ok" if response.status == 200 else "temporary"
    except urllib.error.HTTPError as exc:
        return "auth_required" if exc.code in (401, 403) else "temporary"
    except (OSError, TimeoutError):
        return "temporary"


def launch_parameters(run_id, mode):
    return {
        "cluster": os.environ["ECS_CLUSTER"], "taskDefinition": os.environ["TASK_DEFINITION"],
        "launchType": "FARGATE", "platformVersion": "1.4.0", "count": 1,
        "clientToken": run_id, "startedBy": run_id,
        "networkConfiguration": {"awsvpcConfiguration": {
            "subnets": [os.environ["SUBNET_ID"]],
            "securityGroups": [os.environ["SECURITY_GROUP_ID"]], "assignPublicIp": "ENABLED"}},
        "overrides": {"containerOverrides": [{"name": "collector", "command": [
            "python", "-m", "proleague.pipeline", "--mode", mode, "--run-id", run_id]}]},
    }


def plain(value):
    # Boto3 restores DynamoDB numbers as Decimal; ECS expects actual int values.
    return json.loads(json.dumps(value, default=lambda obj: int(obj) if isinstance(obj, Decimal) else str(obj)))


class Controller:
    def __init__(self, store, ecs, ssm):
        self.store, self.ecs, self.ssm = store, ecs, ssm

    def snapshot(self, run=None):
        if run is None:
            pointer = self.store.get("ACTIVE") or self.store.get("LATEST")
            run = self.store.get("RUN#" + pointer["run_id"]) if pointer else None
        public = {key: run.get(key) for key in PUBLIC_FIELDS if key in run} if run else None
        if public and public.get("status") in ERRORS:
            public["error"] = ERRORS[public["status"]]
        published = self.store.get("PUBLISHED")
        dataset = {key: published[key] for key in ("dataset_id", "count") if key in published} if published else None
        return {"run": public, "dataset": dataset}

    def advance(self, run):
        if run["status"] != "starting":
            return run
        run_id = run["run_id"]
        active = self.store.get("ACTIVE")
        if not active or active["run_id"] != run_id:
            return run
        if active.get("task_arn") or run.get("task_arn"):
            # ECS has acknowledged this task. Replaying RunTask is unnecessary
            # and could outlive ECS's idempotency retention for a stopped task.
            return run
        if run["stage"] == "preflight":
            result = preflight(self.ssm)
            if result != "ok":
                status = "auth_required" if result == "auth_required" else "failed"
                if self.store.patch_starting(run_id, expected_stage="preflight", status=status,
                                             stage="preflight", updated_at=int(time.time())):
                    self.store.release(run_id)
                return self.store.get("RUN#" + run_id)
            self.store.patch_starting(run_id, expected_stage="preflight", stage="launch_pending",
                                      updated_at=int(time.time()))
            run = self.store.get("RUN#" + run_id)
            if run["status"] != "starting":
                return run
        # Never reuse an ECS token near/after its post-stop expiry. Reconcile
        # long-abandoned launches by startedBy instead of issuing a fresh task.
        if int(time.time()) - int(run["started_at"]) > 2700:
            tasks = self.ecs.list_tasks(cluster=run["launch_params"]["cluster"], startedBy=run_id).get("taskArns", [])
            if tasks:
                self.store.attach_task(run_id, tasks[0])
                self.store.patch_starting(run_id, stage="collecting", status="running", task_arn=tasks[0], updated_at=int(time.time()))
            elif self.store.patch_starting(run_id, stage="launch_expired", status="paused", updated_at=int(time.time())):
                self.store.release(run_id)
            return self.store.get("RUN#" + run_id)
        try:
            response = self.ecs.run_task(**plain(run["launch_params"]))
        except Exception:
            # A timeout/5xx can occur after ECS accepted the request. Keep lock.
            return self.store.get("RUN#" + run_id)
        tasks = response.get("tasks", [])
        if tasks:
            task_arn = tasks[0]["taskArn"]
            self.store.attach_task(run_id, task_arn)
            self.store.patch_starting(run_id, task_arn=task_arn, stage="provisioning", updated_at=int(time.time()))
        elif response.get("failures"):
            if self.store.patch_starting(run_id, status="failed", stage="launch_failed", updated_at=int(time.time())):
                self.store.release(run_id)
        return self.store.get("RUN#" + run_id)

    def start(self, body):
        if (not isinstance(body, dict) or set(body) != {"mode", "requestId"} or
                not isinstance(body.get("mode"), str) or body["mode"] not in MODES):
            raise ValueError("Use a predefined collection mode and a UUID requestId.")
        try:
            request_id = str(uuid.UUID(body["requestId"]))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("requestId must be a UUID.") from None
        for _ in range(3):
            existing = self.store.get("REQUEST#" + request_id)
            if existing:
                if existing.get("mode", body["mode"]) != body["mode"]:
                    raise ValueError("requestId was already used for another mode.")
                return self.snapshot(self.advance(self.store.get("RUN#" + existing["run_id"])))
            active = self.store.get("ACTIVE")
            if active:
                if self.store.bind_request(request_id, active["run_id"], body["mode"]):
                    return self.snapshot(self.advance(self.store.get("RUN#" + active["run_id"])))
                continue
            now = int(time.time())
            run = {"run_id": request_id, "mode": body["mode"], "status": "starting", "stage": "preflight",
                   "new_games": 0, "target": MODES[body["mode"]], "started_at": now, "updated_at": now,
                   "launch_params": launch_parameters(request_id, body["mode"])}
            if self.store.reserve(run, request_id):
                return self.snapshot(self.advance(run))
        raise RuntimeError("Collection reservation changed; retry this request.")

    def status(self):
        active = self.store.get("ACTIVE")
        if active:
            run = self.store.get("RUN#" + active["run_id"])
            if run["status"] in TERMINAL:
                self.store.release(run["run_id"])
            elif active.get("task_arn"):
                response = self.ecs.describe_tasks(cluster=os.environ["ECS_CLUSTER"], tasks=[active["task_arn"]])
                tasks = response.get("tasks", [])
                if tasks and tasks[0].get("lastStatus") == "STOPPED":
                    self.store.stopped(run["run_id"])
            elif run["status"] == "starting":
                self.advance(run)
        return self.snapshot()

    def stopped_event(self, event):
        detail = event.get("detail", {})
        active = self.store.get("ACTIVE")
        if active and (active.get("task_arn") == detail.get("taskArn") if active.get("task_arn")
                       else active["run_id"] == detail.get("startedBy")):
            self.store.stopped(active["run_id"])
        return {"ok": True}


def handler(event, context):
    clients = Config(connect_timeout=3, read_timeout=6, retries={"max_attempts": 1})
    controller = Controller(Store(os.environ["STATE_TABLE"]), boto3.client("ecs", config=clients),
                            boto3.client("ssm", config=clients))
    if event.get("source") == "aws.ecs" and event.get("detail", {}).get("lastStatus") == "STOPPED":
        return controller.stopped_event(event)
    try:
        method = event.get("requestContext", {}).get("http", {}).get("method")
        path = event.get("rawPath", "")
        if method == "GET" and path == "/api/status":
            result = controller.status()
        elif method == "POST" and path == "/api/runs":
            raw = event.get("body") or "{}"
            if event.get("isBase64Encoded"):
                raw = base64.b64decode(raw, validate=True).decode()
            if len(raw) > 1024:
                raise ValueError("Request body is too large.")
            result = controller.start(json.loads(raw))
        else:
            return reply(404, {"error": "Unknown endpoint.", "retryable": False})
        code = 202 if result.get("run") and result["run"]["status"] in ("starting", "running") else 200
        return reply(code, result)
    except (ValueError, UnicodeError) as exc:
        return reply(400, {"error": str(exc) if not isinstance(exc, json.JSONDecodeError) else "Invalid JSON.", "retryable": False})
    except Exception as exc:
        print(json.dumps({"event": "control_error", "type": type(exc).__name__}))
        return reply(503, {"error": "Collection status is temporarily unavailable. Retry the same request.", "retryable": True})


def reply(code, body):
    return {"statusCode": code, "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps(plain(body))}
