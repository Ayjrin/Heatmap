"""Routing is typed at the call site, never threaded through as one region string.

RIOT_LOL_API.md §2: there are two routing schemes and mixing them up is the most
common source of spurious 404s -- and a 404 from the wrong routing value looks
exactly like "no such match" (§1 error table). Making the two schemes distinct
types means a wrong routing value is a construction error, not a silent 404.
"""
from __future__ import annotations

from enum import Enum


class Platform(str, Enum):
    """Per-shard routing: summoner-v4, league-v4, league-exp-v4, spectator-v5..."""
    BR1 = "br1"; LA1 = "la1"; LA2 = "la2"; NA1 = "na1"
    JP1 = "jp1"; KR = "kr"
    EUN1 = "eun1"; EUW1 = "euw1"; ME1 = "me1"; RU = "ru"; TR1 = "tr1"
    OC1 = "oc1"; SG2 = "sg2"; TW2 = "tw2"; VN2 = "vn2"

    @property
    def region(self) -> "Region":
        return Region(_PLATFORM_TO_REGION[self.value])


class Region(str, Enum):
    """Per-cluster routing: match-v5, account-v1, lol-rso-match-v1..."""
    AMERICAS = "americas"
    ASIA = "asia"
    EUROPE = "europe"
    SEA = "sea"


_PLATFORM_TO_REGION = {
    "br1": "americas", "la1": "americas", "la2": "americas", "na1": "americas",
    "jp1": "asia", "kr": "asia",
    "eun1": "europe", "euw1": "europe", "me1": "europe", "ru": "europe", "tr1": "europe",
    "oc1": "sea", "sg2": "sea", "tw2": "sea", "vn2": "sea",
}


def base_url(routing: Platform | Region) -> str:
    return f"https://{routing.value}.api.riotgames.com"


def platform_of_match_id(match_id: str) -> Platform:
    """'NA1_4567890123' -> Platform.NA1.

    §2: a match ID is prefixed with the platform it was played on, but you fetch
    it from that platform's *cluster*.
    """
    prefix = match_id.split("_", 1)[0].lower()
    return Platform(prefix)


def region_of_match_id(match_id: str) -> Region:
    return platform_of_match_id(match_id).region
