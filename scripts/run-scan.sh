#!/bin/bash
# One scan cycle: sweep the date window, alert if cheap, refresh the dashboard.
# Invoked by launchd every 10 hours (see com.kevinramdath.flightwatch.plist).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON="$PROJECT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "No venv at $PYTHON -- run: python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

"$PYTHON" -m flight_watch.main scan --export docs

# Publishing the refreshed dashboard is opt-in: set FW_AUTO_PUBLISH=1 in the
# plist's environment once you are happy for every scan to push to GitHub.
if [[ "${FW_AUTO_PUBLISH:-0}" == "1" ]] && ! git diff --quiet -- docs/data.json; then
  git add docs/data.json
  git commit -qm "chore: refresh dashboard data ($(date -u +%Y-%m-%dT%H:%MZ))"
  git push -q origin HEAD
  echo "Dashboard published."
fi
