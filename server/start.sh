#!/bin/bash
# Manual start — use this first to test before setting up auto-start via launchd.
# If `claude` isn't on PATH for this shell, set PORTFOLIO_CLAUDE_BIN to its full path, e.g.:
#   export PORTFOLIO_CLAUDE_BIN="$(which claude)"
cd "$(dirname "$0")/.." || exit 1
exec python3 server/portfolio_server.py
