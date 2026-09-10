#!/bin/sh
# Compatibility wrapper for the Terraform and Docker deployment helper.
set -eu
cd "$(dirname "$0")"
exec "${PYTHON:-python3}" scripts/aws_deploy.py "$@"
