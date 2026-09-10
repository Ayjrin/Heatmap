#!/usr/bin/env python3
"""Validate RIOT_API_KEY from .env and optionally sync it to SSM SecureString."""
import argparse
import os
from pathlib import Path
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import dotenv_values
import requests

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--parameter", default="/proleague-heatmap/riot-api-key")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    key = os.environ.get("RIOT_API_KEY") or dotenv_values(ROOT / ".env").get("RIOT_API_KEY")
    if not key or not key.strip():
        print("auth_required: RIOT_API_KEY is missing.")
        return 3
    try:
        response = requests.get(
            "https://na1.api.riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5",
            headers={"X-Riot-Token": key.strip()}, timeout=10)
        status = response.status_code
        response.close()
        if status in (401, 403):
            print("auth_required: refresh RIOT_API_KEY in .env.")
            return 3
        if status != 200:
            print(f"Key preflight unavailable: Riot HTTP {status}; no parameter was changed.")
            return 1
        if args.check_only:
            print("Riot key preflight passed.")
            return 0
        ssm = boto3.Session(profile_name=args.profile, region_name=args.region).client("ssm")
        result = ssm.put_parameter(Name=args.parameter, Type="SecureString", Value=key.strip(), Overwrite=True)
        print(f"Updated SSM parameter {args.parameter}, version {result['Version']}. Start collection manually.")
        return 0
    except (requests.RequestException, BotoCoreError, ClientError) as exc:
        print(f"Key sync failed ({type(exc).__name__}); no credential values are logged.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
