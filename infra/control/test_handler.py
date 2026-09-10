"""Offline contract tests: no AWS credentials, Riot traffic, or synthetic site data."""
import copy
import importlib.util
import pathlib
import time
import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location("heatmap_control", pathlib.Path(__file__).with_name("handler.py"))
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)


def test_riot_preflight_identifies_the_application(monkeypatch):
    monkeypatch.setenv("SSM_PARAMETER", "/test/key")
    ssm = Mock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": "isolated-test-key"}}
    def response(request, timeout):
        assert request.get_header("User-agent") == "ProLeagueHeatmap/1.0"
        return nullcontext(SimpleNamespace(status=200))
    monkeypatch.setattr(control.urllib.request, "urlopen", response)
    assert control.preflight(ssm) == "ok"


class MemoryStore:
    def __init__(self):
        self.rows = {}

    def get(self, key):
        return copy.deepcopy(self.rows.get(key))

    def reserve(self, run, request_id):
        if "ACTIVE" in self.rows or "REQUEST#" + request_id in self.rows:
            return False
        self.rows["RUN#" + run["run_id"]] = copy.deepcopy(run)
        self.rows["ACTIVE"] = {"run_id": run["run_id"]}
        self.rows["LATEST"] = {"run_id": run["run_id"]}
        self.rows["REQUEST#" + request_id] = {"run_id": run["run_id"], "mode": run["mode"]}
        return True

    def bind_request(self, request_id, run_id, mode):
        if self.rows.get("ACTIVE", {}).get("run_id") != run_id or "REQUEST#" + request_id in self.rows:
            return False
        self.rows["REQUEST#" + request_id] = {"run_id": run_id, "mode": mode}
        return True

    def patch_starting(self, run_id, *, expected_stage=None, **values):
        run = self.rows["RUN#" + run_id]
        if run["status"] != "starting" or (expected_stage is not None and run["stage"] != expected_stage):
            return False
        run.update(copy.deepcopy(values))
        return True

    def attach_task(self, run_id, task_arn):
        if self.rows.get("ACTIVE", {}).get("run_id") == run_id:
            self.rows["ACTIVE"]["task_arn"] = task_arn

    def release(self, run_id):
        if self.rows.get("ACTIVE", {}).get("run_id") == run_id:
            self.rows.pop("ACTIVE")

    def stopped(self, run_id):
        run = self.rows["RUN#" + run_id]
        if run["status"] not in control.TERMINAL:
            run.update(status="failed", stage="task_stopped")
        self.release(run_id)


@pytest.fixture
def app(monkeypatch):
    for key in ("ECS_CLUSTER", "TASK_DEFINITION", "SUBNET_ID", "SECURITY_GROUP_ID", "SSM_PARAMETER"):
        monkeypatch.setenv(key, "test-" + key.lower())
    monkeypatch.setattr(control, "preflight", lambda ssm: "ok")
    ecs = Mock()
    ecs.run_task.return_value = {"tasks": [{"taskArn": "arn:task:one"}], "failures": []}
    ecs.describe_tasks.return_value = {"tasks": [{"lastStatus": "RUNNING"}]}
    return control.Controller(MemoryStore(), ecs, Mock())


def start(app, mode="smoke", request_id=None):
    return app.start({"mode": mode, "requestId": request_id or str(uuid.uuid4())})


@pytest.mark.parametrize("mode,target", [("smoke", 50), ("small", 250), ("full", None)])
def test_fixed_modes_and_no_private_fields(app, mode, target):
    result = start(app, mode)
    assert result["run"]["target"] == target
    assert "launch_params" not in result["run"]
    assert "task_arn" not in result["run"]
    args = app.ecs.run_task.call_args.kwargs
    assert args["clientToken"] == result["run"]["run_id"]
    assert args["overrides"]["containerOverrides"][0]["command"][-3:] == [mode, "--run-id", args["clientToken"]]


@pytest.mark.parametrize("result", ["auth_required", "temporary"])
def test_failed_preflight_does_not_launch_and_releases_lock(app, monkeypatch, result):
    monkeypatch.setattr(control, "preflight", lambda ssm: result)
    run = start(app)["run"]
    assert run["status"] == ("auth_required" if result == "auth_required" else "failed")
    app.ecs.run_task.assert_not_called()
    assert app.store.get("ACTIVE") is None


def test_same_request_recovers_exact_launch_after_timeout(app):
    app.ecs.run_task.side_effect = [TimeoutError("do not expose secret"), {"tasks": [{"taskArn": "arn:one"}]}]
    request_id = str(uuid.uuid4())
    first = start(app, request_id=request_id)
    assert first["run"]["status"] == "starting"
    assert app.store.get("ACTIVE")["run_id"] == request_id
    second = start(app, request_id=request_id)
    assert second["run"]["run_id"] == request_id
    assert app.ecs.run_task.call_args_list[0] == app.ecs.run_task.call_args_list[1]


def test_second_click_reuses_existing_worker_and_mode(app):
    first = start(app)
    second = start(app, "full")
    assert second["run"]["run_id"] == first["run"]["run_id"]
    assert second["run"]["mode"] == "smoke"
    tokens = {call.kwargs["clientToken"] for call in app.ecs.run_task.call_args_list}
    assert tokens == {first["run"]["run_id"]}
    assert app.ecs.run_task.call_count == 1


def test_busy_click_retry_after_completion_never_starts_another_run(app):
    first = start(app)["run"]["run_id"]
    request_id = str(uuid.uuid4())
    assert start(app, "small", request_id)["run"]["run_id"] == first
    app.store.rows["RUN#" + first]["status"] = "succeeded"
    app.store.release(first)
    assert start(app, "small", request_id)["run"]["run_id"] == first
    assert app.store.get("ACTIVE") is None
    assert app.ecs.run_task.call_count == 1


def test_request_id_cannot_change_mode(app):
    request_id = str(uuid.uuid4())
    start(app, "smoke", request_id)
    with pytest.raises(ValueError, match="another mode"):
        start(app, "full", request_id)


def test_busy_request_retries_if_owner_completes_during_binding(app):
    first = start(app)["run"]["run_id"]
    bind = app.store.bind_request

    def complete_then_bind(*args):
        app.store.rows["RUN#" + first]["status"] = "succeeded"
        app.store.release(first)
        return bind(*args)

    app.store.bind_request = complete_then_bind
    second = start(app, "small")["run"]
    assert second["run_id"] != first
    assert second["mode"] == "small"


def test_http_success_with_ecs_failure_is_terminal(app):
    app.ecs.run_task.return_value = {"tasks": [], "failures": [{"reason": "RESOURCE:MEMORY"}]}
    assert start(app)["run"]["status"] == "failed"
    assert app.store.get("ACTIVE") is None


def test_stopped_task_reconciled_by_status_if_event_is_delayed(app):
    run_id = start(app)["run"]["run_id"]
    app.ecs.describe_tasks.return_value = {"tasks": [{"lastStatus": "STOPPED"}]}
    result = app.status()
    assert result["run"]["status"] == "failed"
    assert app.store.get("ACTIVE") is None
    assert app.store.get("RUN#" + run_id)["stage"] == "task_stopped"


def test_delayed_stopped_event_cannot_clear_new_owner(app):
    old = start(app)["run"]["run_id"]
    app.store.release(old)
    app.ecs.run_task.return_value = {"tasks": [{"taskArn": "arn:task:two"}]}
    new = start(app)["run"]["run_id"]
    app.stopped_event({"detail": {"taskArn": "arn:task:one", "startedBy": old}})
    assert app.store.get("ACTIVE")["run_id"] == new


def test_old_task_event_cannot_clear_resumed_run_with_different_task(app):
    run_id = start(app)["run"]["run_id"]
    app.store.attach_task(run_id, "arn:task:resumed")
    app.stopped_event({"detail": {"taskArn": "arn:task:one", "startedBy": run_id}})
    assert app.store.get("ACTIVE")["task_arn"] == "arn:task:resumed"


def test_stopped_event_preserves_worker_success(app):
    run_id = start(app)["run"]["run_id"]
    app.store.rows["RUN#" + run_id].update(status="succeeded", published_version="live-release")
    app.stopped_event({"detail": {"taskArn": "arn:task:one", "startedBy": run_id}})
    assert app.status()["run"]["status"] == "succeeded"
    assert app.store.get("ACTIVE") is None


def test_old_uncertain_launch_does_not_reuse_expired_token(app):
    app.ecs.run_task.side_effect = TimeoutError()
    run_id = start(app)["run"]["run_id"]
    app.store.rows["RUN#" + run_id]["started_at"] = int(time.time()) - 3000
    app.ecs.list_tasks.return_value = {"taskArns": []}
    result = app.status()
    assert result["run"]["status"] == "paused"
    assert app.ecs.run_task.call_count == 1
    assert app.store.get("ACTIVE") is None


def test_acknowledged_task_is_not_relaunched_after_token_expiry(app):
    run_id = start(app)["run"]["run_id"]
    app.store.rows["RUN#" + run_id]["started_at"] = int(time.time()) - 100000
    assert start(app, request_id=run_id)["run"]["run_id"] == run_id
    assert app.ecs.run_task.call_count == 1


def test_public_errors_never_include_worker_details(app):
    run_id = start(app)["run"]["run_id"]
    app.store.rows["RUN#" + run_id].update(status="failed", error="secret-key / private stacktrace")
    assert "secret" not in app.snapshot()["run"]["error"]


@pytest.mark.parametrize("body", [{"mode": "smoke", "requestId": "bad"},
    {"mode": "shell", "requestId": str(uuid.uuid4())},
    {"mode": [], "requestId": str(uuid.uuid4())},
    {"mode": {}, "requestId": str(uuid.uuid4())},
    {"mode": "smoke", "requestId": str(uuid.uuid4()), "target": 9999}, None, []])
def test_reject_arbitrary_commands_and_limits(app, body):
    with pytest.raises(ValueError):
        app.start(body)
    app.ecs.run_task.assert_not_called()
