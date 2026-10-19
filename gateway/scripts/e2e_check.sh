#!/usr/bin/env bash
# Thin wrapper around the Groki Bot gateway's end-to-end self-check.
# Usage:./scripts/e2e_check.sh [--with-voice] [--skip-music] [--base-url URL]
set -euo pipefail

cd "$(dirname "$0")/.."
exec uv run python -m stackchan_mcp.e2e_check "$@"
