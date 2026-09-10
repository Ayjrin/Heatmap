"""Riot API client. Routing is typed at the call site; the limiter is per route.

Error handling follows RIOT_LOL_API.md §1:
  403  key invalid or expired (dev keys last 24h) -- fatal, do not retry
  404  not found, and ALSO what a wrong routing value returns -- never retried,
       surfaced as None so the caller decides
  429  halt for the full Retry-After; a `service` 429 has no Retry-After
       guarantee, so fall back to exponential backoff
  5xx  Riot-side, retry with backoff
"""
from __future__ import annotations

import hashlib
import logging
import random
import time

import requests

from .limiter import DEV_KEY_LIMITS, LimiterRegistry
from .routing import Platform, Region, base_url

log = logging.getLogger(__name__)

MAX_RETRIES = 5
BACKOFF_BASE = 1.5


class RiotAPIError(RuntimeError):
    pass


class AuthError(RiotAPIError):
    """403/401 -- key expired or not entitled. Fatal; a retry cannot help."""


class RiotClient:
    def __init__(
        self,
        api_key: str,
        limits: tuple[tuple[int, int], ...] = DEV_KEY_LIMITS,
        session: requests.Session | None = None,
        sleep_fn=time.sleep,
    ) -> None:
        if not api_key:
            raise AuthError(
                "No API key. Set RIOT_API_KEY (get one at developer.riotgames.com; "
                "development keys expire every 24 hours)."
            )
        self.api_key = api_key
        self.limiters = LimiterRegistry(limits)
        self.session = session or requests.Session()
        self.session.headers.update({"X-Riot-Token": api_key, "User-Agent": "ProLeagueHeatmap/1.0"})
        self._sleep = sleep_fn
        self.call_count = 0

    @property
    def key_epoch(self) -> str:
        """Stable, non-secret fingerprint of the key in use.

        Used for diagnostics only. Match IDs and participant PUUIDs, never
        display names or a key fingerprint, identify stored data.
        """
        return hashlib.sha256(self.api_key.encode()).hexdigest()[:12]

    def get(self, routing: Platform | Region, path: str, params: dict | None = None,
            method: str | None = None):
        """GET with limiting + retry. Returns parsed JSON, or None on 404."""
        url = f"{base_url(routing)}{path}"
        limiter = self.limiters.get(routing.value)

        for attempt in range(MAX_RETRIES):
            limiter.acquire(method)
            self.call_count += 1
            try:
                resp = self.session.get(url, params=params, timeout=20)
            except requests.RequestException as exc:
                self._backoff(attempt, f"network error: {exc}")
                continue

            limiter.sync_from_headers(resp.headers, method)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 404:
                # Genuinely absent, OR the wrong routing value (§1). Caller decides.
                log.debug("404 %s (check routing: %s)", url, routing.value)
                return None

            if resp.status_code in (401, 403):
                raise AuthError(
                    f"{resp.status_code} on {path}. Dev keys expire every 24h -- "
                    f"regenerate at developer.riotgames.com and re-export RIOT_API_KEY."
                )

            if resp.status_code == 429:
                limit_type = resp.headers.get("X-Rate-Limit-Type", "unknown")
                retry_after = resp.headers.get("Retry-After")
                if retry_after is not None:
                    wait = float(retry_after)
                    log.warning("429 (%s) -- halting %.0fs per Retry-After", limit_type, wait)
                else:
                    # `service` 429s carry no Retry-After guarantee (§1).
                    wait = BACKOFF_BASE ** (attempt + 1) + random.random()
                    log.warning("429 (%s) with no Retry-After -- backing off %.1fs",
                                limit_type, wait)
                limiter.penalize(wait, limit_type)
                continue

            if 500 <= resp.status_code < 600:
                self._backoff(attempt, f"{resp.status_code} from Riot")
                continue

            raise RiotAPIError(f"{resp.status_code} on {url}: {resp.text[:200]}")

        raise RiotAPIError(f"exhausted {MAX_RETRIES} retries on {url}")

    def _backoff(self, attempt: int, why: str) -> None:
        wait = BACKOFF_BASE ** (attempt + 1) + random.random()
        log.warning("%s -- retrying in %.1fs", why, wait)
        self._sleep(wait)

    # -- endpoints --------------------------------------------------------
    def apex_league(self, platform: Platform, tier: str, queue: str = "RANKED_SOLO_5x5"):
        """challenger/grandmaster/master leagues -> LeagueListDTO. PLATFORM routed.

        LeagueItemDTO already carries `puuid`, so there is no summoner-v4 hop.
        """
        slug = {"CHALLENGER": "challengerleagues",
                "GRANDMASTER": "grandmasterleagues",
                "MASTER": "masterleagues"}[tier.upper()]
        return self.get(platform, f"/lol/league/v4/{slug}/by-queue/{queue}",
                        method=f"league-v4.{slug}")

    def league_exp_entries(self, platform: Platform, tier: str, *,
                           queue: str = "RANKED_SOLO_5x5", division: str = "I",
                           page: int = 1):
        """league-exp-v4 -> list[LeagueEntryDTO]. PLATFORM routed.

        Preferred over league-v4's apex endpoints: it is paginated (so the
        caller controls how much ladder to pull), it returns entries directly
        rather than wrapped in a LeagueListDTO, and it covers the apex tiers
        that plain league-v4 entries cannot address. Apex tiers have exactly
        one division, "I".

        Returns [] when the page is past the end of the ladder -- that empty
        page is the pagination terminator.
        """
        return self.get(
            platform,
            f"/lol/league-exp/v4/entries/{queue}/{tier.upper()}/{division}",
            params={"page": page},
            method="league-exp-v4.entries",
        ) or []

    def match_ids_by_puuid(self, region: Region, puuid: str, *, queue: int = 420,
                           type_: str = "ranked", start: int = 0, count: int = 100,
                           start_time: int | None = None, end_time: int | None = None):
        """REGIONAL routed. `count` caps at 100 (§8); page with `start`.

        `start_time` (unix seconds) bounds the window server-side. Passing it
        is strictly better than fetching and discarding: an out-of-patch match
        we never learn about is a match we never spend 2 rate-limited calls on.
        """
        params = {"queue": queue, "type": type_, "start": start, "count": min(count, 100)}
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        return self.get(
            region, f"/lol/match/v5/matches/by-puuid/{puuid}/ids", params=params,
            method="match-v5.ids",
        ) or []

    def match(self, region: Region, match_id: str):
        return self.get(region, f"/lol/match/v5/matches/{match_id}", method="match-v5.match")

    def timeline(self, region: Region, match_id: str):
        return self.get(region, f"/lol/match/v5/matches/{match_id}/timeline", method="match-v5.timeline")
