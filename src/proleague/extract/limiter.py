"""Token-bucket rate limiter for the Riot API.

Enforces every (count, seconds) window simultaneously, keyed per routing value
because RIOT_LOL_API.md §1 is explicit that "limits are enforced per routing
value, so na1 and euw1 have separate budgets".

Dev/personal key windows are 20 req/1s AND 100 req/2min. The 2-minute window is
the binding constraint (3,000 req/hour); the 20/s ceiling never engages in
practice, but both are enforced.

RIOT_LOL_API.md §1 documents THREE independent limits, not one: application
(this key overall), method (per endpoint), and service (Riot-side, undocumented
and not header-advertised). Modelling only the application window is how a
crawler that "respects the rate limit" still eats 429s: the method window on a
single hot endpoint -- /matches/{id}/timeline, which we call for every match --
can trip while the app window is half empty. Both advertised limits are
therefore tracked, the method one keyed per endpoint, from the headers Riot
returns. `service` limits cannot be predicted and are handled reactively by
penalize().

The limiter is a sliding-window log rather than a leaky bucket: Riot's own
accounting is a fixed count per window, and the log lets us reconcile directly
against the X-App-Rate-Limit-Count header instead of guessing at a drain rate.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

# (count, window_seconds) for a development / personal key. RIOT_LOL_API.md §1.
DEV_KEY_LIMITS: tuple[tuple[int, int], ...] = ((20, 1), (100, 120))
PROD_KEY_LIMITS: tuple[tuple[int, int], ...] = ((500, 10), (30_000, 600))

# Leave a little headroom so a clock skew against Riot's window boundary does
# not turn into a 429 storm.
SAFETY_MARGIN = 1


@dataclass
class _Window:
    limit: int
    seconds: int
    hits: deque[float] = field(default_factory=deque)

    def prune(self, now: float) -> None:
        cutoff = now - self.seconds
        while self.hits and self.hits[0] <= cutoff:
            self.hits.popleft()

    def wait_time(self, now: float) -> float:
        """Seconds to wait before another request fits inside this window."""
        self.prune(now)
        if len(self.hits) < max(1, self.limit - SAFETY_MARGIN):
            return 0.0
        # Wait until the oldest hit falls out of the window.
        return max(0.0, self.hits[0] + self.seconds - now)


class RateLimiter:
    """Blocking limiter for one routing value.

    `time_fn`/`sleep_fn` are injectable so the tests can drive it with a
    simulated clock and assert the windows are never exceeded.
    """

    def __init__(
        self,
        limits: tuple[tuple[int, int], ...] = DEV_KEY_LIMITS,
        time_fn=time.monotonic,
        sleep_fn=time.sleep,
    ) -> None:
        self._windows = [_Window(limit=c, seconds=s) for c, s in limits]
        self._time = time_fn
        self._sleep = sleep_fn
        self._lock = threading.Lock()
        self._halt_until = 0.0   # set by a 429 Retry-After
        # Method windows are discovered from response headers rather than
        # configured: Riot does not publish them, and they differ per endpoint.
        self._method: dict[str, list[_Window]] = {}
        self.rate_limit_hits: dict[str, int] = {}

    def acquire(self, method: str | None = None) -> None:
        """Block until a request may be sent, then record it.

        `method` is an endpoint key (e.g. "match-v5.timeline"). Its window is
        only enforced once Riot has told us what it is; until the first
        response for that endpoint arrives there is nothing to enforce, which
        is safe because the app window is stricter early in a run.
        """
        while True:
            with self._lock:
                now = self._time()
                windows = list(self._windows)
                if method and method in self._method:
                    windows += self._method[method]
                waits = [w.wait_time(now) for w in windows]
                halt = max(0.0, self._halt_until - now)
                wait = max(waits + [halt])
                if wait <= 0:
                    for w in windows:
                        w.hits.append(now)
                    return
            # Sleep outside the lock so other threads can still observe state.
            self._sleep(wait)

    def penalize(self, retry_after_seconds: float, limit_type: str = "unknown") -> None:
        """Halt all requests for the full Retry-After, per RIOT_LOL_API.md §1.

        `limit_type` is the X-Rate-Limit-Type header (application/method/
        service). It is counted rather than just logged so a run report can
        state WHICH limit was hit -- "zero 429s" is an acceptance criterion,
        and "we got 429s but only `service` ones" is a materially different
        result from "our own accounting was wrong".
        """
        with self._lock:
            self._halt_until = max(self._halt_until, self._time() + retry_after_seconds)
            self.rate_limit_hits[limit_type] = self.rate_limit_hits.get(limit_type, 0) + 1

    def sync_from_headers(self, headers, method: str | None = None) -> None:
        """Reconcile local state against Riot's own accounting.

        `X-App-Rate-Limit` is "20:1,100:120" and `X-App-Rate-Limit-Count` is
        "1:1,7:120". If Riot thinks we have used more of a window than our log
        does, we top the log up so we throttle on their number, not ours.

        The same pair of headers exists scoped to the endpoint
        (`X-Method-Rate-Limit`), and that is where the method windows come
        from -- they are learned from the first response, not configured.
        """
        with self._lock:
            self._sync(self._windows, headers, "X-App-Rate-Limit")
            if method:
                limits = _parse_pairs(headers.get("X-Method-Rate-Limit"))
                if limits:
                    known = self._method.setdefault(method, [])
                    have = {w.seconds for w in known}
                    for window_seconds, limit in limits.items():
                        if window_seconds not in have:
                            known.append(_Window(limit=limit, seconds=window_seconds))
                    self._sync(known, headers, "X-Method-Rate-Limit")

    def _sync(self, windows: list[_Window], headers, prefix: str) -> None:
        """Caller holds the lock."""
        limits = _parse_pairs(headers.get(prefix))
        counts = _parse_pairs(headers.get(f"{prefix}-Count"))
        if not limits or not counts:
            return
        now = self._time()
        for window_seconds, used in counts.items():
            w = next((w for w in windows if w.seconds == window_seconds), None)
            if w is None:
                continue
            w.prune(now)
            # Riot counts more than we do -> backfill phantom hits so our
            # window is at least as conservative as theirs.
            deficit = used - len(w.hits)
            for _ in range(max(0, deficit)):
                w.hits.appendleft(now)

    def snapshot(self) -> dict:
        """Current usage per window, for logging and the end-of-run report."""
        with self._lock:
            now = self._time()
            for w in self._windows:
                w.prune(now)
            out = {"app": {w.seconds: f"{len(w.hits)}/{w.limit}" for w in self._windows}}
            if self._method:
                out["method"] = {
                    m: {w.seconds: f"{len(w.hits)}/{w.limit}" for w in ws}
                    for m, ws in self._method.items()
                }
            if self.rate_limit_hits:
                out["429s"] = dict(self.rate_limit_hits)
            return out


def _parse_pairs(header: str | None) -> dict[int, int]:
    """'20:1,100:120' -> {1: 20, 120: 100}  (keyed by window seconds)."""
    if not header:
        return {}
    out: dict[int, int] = {}
    for part in header.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        value, window = part.split(":", 1)
        try:
            out[int(window)] = int(value)
        except ValueError:
            continue
    return out


class LimiterRegistry:
    """One limiter per routing value (na1, americas, ...)."""

    def __init__(self, limits: tuple[tuple[int, int], ...] = DEV_KEY_LIMITS) -> None:
        self._limits = limits
        self._by_route: dict[str, RateLimiter] = {}
        self._lock = threading.Lock()

    def get(self, routing_value: str) -> RateLimiter:
        with self._lock:
            if routing_value not in self._by_route:
                self._by_route[routing_value] = RateLimiter(self._limits)
            return self._by_route[routing_value]
