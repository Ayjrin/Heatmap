"""Sliding-window limiter: header reconciliation must not stall the window."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from proleague.extract.limiter import RateLimiter


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def test_phantom_hits_from_headers_keep_the_window_time_ordered():
    clock = Clock()
    limiter = RateLimiter(limits=((100, 120),), time_fn=clock.now, sleep_fn=clock.sleep)
    for _ in range(98):
        limiter.acquire()
        clock.t += 1.0
    # Riot has seen one request more than we logged (a probe from another
    # process, a retry it counted and we did not). We take its word for it.
    limiter.sync_from_headers({"X-App-Rate-Limit": "100:120", "X-App-Rate-Limit-Count": "99:120"})
    limiter.acquire()
    # The next slot opens when the OLDEST real hit (t=0) leaves the window at
    # t=120, not 120 s after the phantom was recorded (t=218).
    assert clock.t < 125, f"the window stalled until t={clock.t}"
    # Afterwards the limiter paces at the window rate rather than in bursts
    # separated by full-window stalls.
    before = clock.t
    for _ in range(50):
        limiter.acquire()
    assert clock.t - before < 60
