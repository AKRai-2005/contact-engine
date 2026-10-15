#!/usr/bin/env bash
# Thin wrapper: run the live checks on a VPS. Configure via env vars (see live_check.py).
#
#   export VERIFY_EMAIL=you@your-domain.com
#   export VERIFY_BAD_EMAIL=nobody-xyz@your-domain.com
#   export HUNTER_API_KEY=...            # optional
#   export PROVIDER_TEST_NAME="Jane Doe" PROVIDER_TEST_DOMAIN=acme.com   # optional
#   bash scripts/live_check.sh
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/live_check.py
