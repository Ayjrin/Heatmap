#!/usr/bin/env python3
"""Explicit deployment steps; never reads or uploads the Riot secret.

Run bootstrap, init, apply (initial ECR), image, apply --image-tag TAG, site.
Terraform retains its normal plan/approval prompt unless --auto-approve is used.
"""
from __future__ import annotations

import argparse
import datetime
import json
import mimetypes
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
SITE_EXTENSIONS = {".html", ".css", ".js", ".mjs", ".json", ".png", ".svg", ".webp", ".ico", ".txt"}


def site_files():
    """Yield approved static assets only; publication of live data belongs to ETL."""
    for path in sorted((ROOT / "web").rglob("*")):
        relative = path.relative_to(ROOT / "web")
        if (not path.is_file() or path.is_symlink() or "data" in relative.parts or
                any(part.startswith(".") for part in relative.parts) or path.suffix not in SITE_EXTENSIONS):
            continue
        mime = "text/javascript" if path.suffix in (".js", ".mjs") else mimetypes.guess_type(path.name)[0]
        yield path, relative.as_posix(), mime or "application/octet-stream"


def command(args, *, capture=False, data=None):
    result = subprocess.run(args, cwd=ROOT, check=True, text=True, input=data,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=["bootstrap", "init", "plan", "apply", "image", "site", "status"])
    parser.add_argument("--profile", default="default")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--image-tag")
    parser.add_argument("--auto-approve", action="store_true")
    args = parser.parse_args()
    tf = ["terraform", f"-chdir={INFRA}"]
    aws = ["aws", "--profile", args.profile, "--region", args.region]
    variables = [f"-var=aws_profile={args.profile}", f"-var=region={args.region}"]
    if args.step == "bootstrap":
        bootstrap = ["terraform", f"-chdir={INFRA / 'bootstrap'}"]
        command(bootstrap + ["init"])
        command(bootstrap + ["apply"] + variables + (["-auto-approve"] if args.auto_approve else []))
        bucket = command(bootstrap + ["output", "-raw", "state_bucket"], capture=True)
        # JSON quoting is HCL-compatible for these scalar strings.
        values = {"bucket": bucket, "key": "application/terraform.tfstate", "region": args.region,
                  "profile": args.profile, "encrypt": True, "use_lockfile": True}
        (INFRA / "backend.hcl").write_text("\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items()) + "\n")
        print("Created infra/backend.hcl. Run the init step next.")
        return
    if args.step == "init":
        command(tf + ["init", "-backend-config=backend.hcl"])
        return
    if args.step in ("plan", "apply"):
        (INFRA / ".build").mkdir(exist_ok=True)
        extra = [f"-var=image_tag={args.image_tag}"] if args.image_tag else []
        if args.step == "apply" and args.auto_approve:
            extra.append("-auto-approve")
        command(tf + [args.step] + variables + extra)
        if args.step == "apply" and args.image_tag:
            (INFRA / "image.auto.tfvars.json").write_text(json.dumps({"image_tag": args.image_tag}) + "\n")
        return
    outputs = json.loads(command(tf + ["output", "-json"], capture=True))
    output = lambda key: outputs[key]["value"]
    if args.step == "image":
        tag = args.image_tag or datetime.datetime.now(datetime.timezone.utc).strftime("build-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        repository = output("ecr_repository")
        password = command(aws + ["ecr", "get-login-password"], capture=True)
        command(["docker", "login", "--username", "AWS", "--password-stdin", repository.split("/")[0]], data=password)
        platform = "linux/arm64" if output("task_architecture") == "ARM64" else "linux/amd64"
        command(["docker", "buildx", "build", "--platform", platform, "--provenance=false", "--push", "-t", f"{repository}:{tag}", "."])
        print(f"Image pushed. Apply with --image-tag {tag}")
    elif args.step == "site":
        # Explicit extension allowlist; never uploads local web/data or .env.
        import boto3
        s3 = boto3.Session(profile_name=args.profile, region_name=args.region).client("s3")
        for path, key, mime in site_files():
            s3.upload_file(str(path), output("site_bucket"), key, ExtraArgs={
                "ContentType": mime,
                "CacheControl": "no-cache, max-age=0, must-revalidate"})
        command(aws + ["cloudfront", "create-invalidation", "--distribution-id", output("distribution_id"), "--paths", "/*"])
        print(output("site_url"))
    else:
        import urllib.request
        with urllib.request.urlopen(output("site_url") + "/api/status", timeout=35) as response:
            print(json.dumps(json.load(response), indent=2))


if __name__ == "__main__":
    main()
