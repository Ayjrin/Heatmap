#!/usr/bin/env python3
"""Loopback development server with the same fixed-mode API as AWS."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dotenv import dotenv_values
import yaml
from proleague.pipeline_state import LocalState

MODES = {"smoke": 50, "small": 250, "full": None}
TERMINAL = {"succeeded", "failed", "paused", "auth_required"}


def preflight():
    key = os.environ.get("RIOT_API_KEY") or dotenv_values(ROOT / ".env").get("RIOT_API_KEY")
    if not key or not key.strip():
        return "auth_required"
    config = yaml.safe_load((ROOT / "config.yaml").read_text()) or {}
    platform = config.get("platform", "na1")
    from proleague.config import PLATFORM_TO_REGION
    if platform not in PLATFORM_TO_REGION:
        return "failed"
    request = urllib.request.Request(
        f"https://{platform}.api.riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5",
        headers={"X-Riot-Token": key.strip(), "User-Agent": "ProLeagueHeatmap/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return "ok" if response.status == 200 else "failed"
    except urllib.error.HTTPError as exc:
        return "auth_required" if exc.code in (401, 403) else "failed"
    except (OSError, TimeoutError):
        return "failed"


class Controller:
    def __init__(self, root=ROOT / "data" / "state", check=preflight, spawn=subprocess.Popen):
        self.state = LocalState(root)
        self.check, self.spawn = check, spawn
        self.mutex = threading.RLock()
        self.process = None
        self.pending = None

    def snapshot(self, run=None):
        if run is None:
            pointer = self.state.get("ACTIVE") or self.state.get("LATEST")
            run = self.state.get("RUN#" + pointer["run_id"]) if pointer else None
        fields = ("run_id", "mode", "status", "stage", "new_games", "target", "started_at",
                  "updated_at", "published_version")
        public = {key: run[key] for key in fields if key in run} if run else None
        errors = {"auth_required": "Add or refresh RIOT_API_KEY in .env, then start a new collection.",
                  "failed": "Collection stopped. Completed games were saved; check the local run log.",
                  "paused": "Collection paused. Completed games were saved."}
        if public and public.get("status") in errors:
            public["error"] = errors[public["status"]]
        published = self.state.get("PUBLISHED")
        dataset = {key: published[key] for key in ("dataset_id", "count") if key in published} if published else None
        return {"run": public, "dataset": dataset}

    def recover(self):
        if self.process is not None:
            if self.process.poll() is None:
                return
            run = self.state.get("RUN#" + self.pending)
            if run and run.get("status") not in TERMINAL:
                self.state.update("RUN#" + self.pending, status="failed", stage="worker_exited", updated_at=int(time.time()))
            self.process, self.pending = None, None
        active = self.state.get("ACTIVE")
        if not active:
            return
        # Only the OS lock can prove an old local worker has exited.
        with (self.state.root / "worker.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            current = self.state.get("ACTIVE")
            if current and current["run_id"] == active["run_id"]:
                run = self.state.get("RUN#" + current["run_id"])
                if run and run.get("status") not in TERMINAL:
                    self.state.update("RUN#" + current["run_id"], status="failed", stage="worker_exited", updated_at=int(time.time()))
                self.state.release(current["run_id"])

    def status(self):
        with self.mutex:
            self.recover()
            return self.snapshot()

    def start(self, body):
        if (not isinstance(body, dict) or set(body) != {"mode", "requestId"}
                or not isinstance(body.get("mode"), str) or body["mode"] not in MODES):
            raise ValueError("Use a predefined mode and a UUID requestId.")
        try:
            run_id = str(uuid.UUID(body["requestId"]))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("requestId must be a UUID.") from None
        with self.mutex:
            self.recover()
            existing = self.state.get("REQUEST#" + run_id)
            if existing:
                return self.snapshot(self.state.get("RUN#" + existing["run_id"]))
            if self.pending or self.state.get("ACTIVE"):
                result = self.snapshot()
                self.state.put("REQUEST#" + run_id, {"run_id": result["run"]["run_id"]})
                return result
            now = int(time.time())
            run = {"run_id": run_id, "mode": body["mode"], "status": "starting", "stage": "preflight",
                   "target": MODES[body["mode"]], "new_games": 0, "started_at": now, "updated_at": now}
            self.state.put("RUN#" + run_id, run)
            self.state.put("REQUEST#" + run_id, {"run_id": run_id})
            self.state.put("LATEST", {"run_id": run_id})
            checked = self.check()
            if checked != "ok":
                self.state.update("RUN#" + run_id, status=checked, updated_at=int(time.time()))
                return self.snapshot()
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
            logs = ROOT / "data" / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            try:
                with (logs / f"{run_id}.log").open("ab") as log:
                    self.process = self.spawn([sys.executable, "-m", "proleague.pipeline", "--mode", body["mode"],
                                               "--run-id", run_id, "--local"], cwd=ROOT, env=env,
                                              stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                self.pending = run_id
            except OSError:
                self.state.update("RUN#" + run_id, status="failed", stage="launch_failed", updated_at=int(time.time()))
            return self.snapshot()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

    def reply(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def list_directory(self, path):
        self.send_error(404)
        return None

    def do_GET(self):
        if urllib.parse.urlsplit(self.path).path == "/api/status":
            self.reply(200, self.server.controller.status())
        elif self.path.startswith("/api/"):
            self.reply(404, {"error": "Unknown endpoint."})
        else:
            super().do_GET()

    def do_POST(self):
        if self.path != "/api/runs":
            self.reply(404, {"error": "Unknown endpoint."})
            return
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            self.reply(403, {"error": "Use the collection controls on this local site."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                raise ValueError("Request body must be between 1 and 1024 bytes.")
            result = self.server.controller.start(json.loads(self.rfile.read(length)))
            self.reply(202 if result.get("run", {}).get("status") in ("starting", "running") else 200, result)
        except (ValueError, UnicodeError):
            self.reply(400, {"error": "Use a predefined mode and a UUID requestId.", "retryable": False})
        except Exception as exc:
            print(json.dumps({"event": "local_api_error", "type": type(exc).__name__}), flush=True)
            self.reply(503, {"error": "Collection is temporarily unavailable.", "retryable": True})

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.controller = Controller()
    print(f"Heatmap and collection API: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
