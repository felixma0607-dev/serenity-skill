#!/bin/bash
# Back up the personal files (holdings, trades, tracker, private notes) to the
# PRIVATE repo github.com/felixma0607-dev/serenity-private.
#
# They live in this working tree but are gitignored by the public repo; a
# second git directory (.git-private) tracks only the paths listed below, so
# nothing moves and the dashboards keep working.
#
# Usage: scripts/backup_private.sh ["commit message"]
set -euo pipefail
cd "$(dirname "$0")/.."

PRIVATE_FILES=(
  portfolio-dashboard.html
  portfolio-dashboard-archive.html
  portfolio-tracker.md
  portfolio-tracker-archive.md
  watchlist.md
  data
  references/minervini-technical.md
  references/martin-luk-strategy.md
  aleabitoreddit_stock_mentions_dashboard.html
  .claude/commands
  .claude/settings.local.json
  SKILL.md
)
REMOTE="https://github.com/felixma0607-dev/serenity-private.git"
pg() { git --git-dir=.git-private --work-tree=. "$@"; }

if [ ! -d .git-private ]; then
  git init -q --bare .git-private
  pg config core.bare false
  pg config status.showUntrackedFiles no
  pg remote add origin "$REMOTE"
fi

for f in "${PRIVATE_FILES[@]}"; do
  [ -e "$f" ] && pg add -f "$f"
done

if pg diff --cached --quiet 2>/dev/null && pg rev-parse -q --verify HEAD >/dev/null; then
  echo "private backup: nothing changed"
  exit 0
fi
pg commit -q -m "${1:-Backup $(date '+%Y-%m-%d %H:%M')}"
pg push -q -u origin HEAD:main
echo "private backup pushed to $REMOTE"
