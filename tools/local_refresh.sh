#!/bin/bash
# Local refresh (used while the GitHub Actions schedule is switched off): scrape, quality gates, commit and
# push the data. LLM translation is OFF (llm.enabled=false), so new posts stay "Translation pending" until
# they are translated in the IDE (python3 -m pipeline.run enrich-export / enrich-import).
#
# usage: tools/local_refresh.sh [all|xm|auto]      (default: auto)
# Scheduled by ~/Library/LaunchAgents/com.xmmonitor.refresh.*.plist, see tools/launchd/README.md.
set -u
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
unset PYTHONWARNINGS
SCOPE="${1:-auto}"
cd "$(dirname "$0")/.." || exit 1
LOG="$HOME/Library/Logs/xm-monitor-refresh.log"
LOCK="${TMPDIR:-/tmp}/xm-monitor-refresh.lock"
mkdir -p "$(dirname "$LOG")"

notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"XM Monitor\"" >/dev/null 2>&1 || true; }
log() { echo "[$(date -u +%FT%TZ)] $*" >> "$LOG"; }

if ! mkdir "$LOCK" 2>/dev/null; then log "another refresh is running (lock $LOCK); skipped"; exit 0; fi
trap 'rmdir "$LOCK"' EXIT

log "start scope=$SCOPE"
if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then log "not on main; skipped"; exit 0; fi
git pull -q --rebase --autostash origin main >> "$LOG" 2>&1 || { log "git pull failed"; notify "git pull failed - see $LOG"; exit 1; }

python3 -m pipeline.run auto --yes --scope "$SCOPE" >> "$LOG" 2>&1
code=$?
log "pipeline exit code $code"

# Commit only the data paths that exist (a missing pathspec would abort the whole command).
paths=()
for p in docs/data data/runs.jsonl data/last_run.json data/auto_runs.jsonl data/review_queue.json; do
  [ -e "$p" ] && paths+=("$p")
done
git add -- "${paths[@]}" >> "$LOG" 2>&1
if git diff --cached --quiet -- "${paths[@]}"; then
  log "nothing to commit"
else
  git commit -q -m "data: local refresh $(date -u +%FT%H:%MZ) (exit $code)" -- "${paths[@]}" >> "$LOG" 2>&1
  pushed=0
  for i in 1 2 3; do
    if git pull -q --rebase --autostash origin main >> "$LOG" 2>&1 && git push -q origin HEAD:main >> "$LOG" 2>&1; then pushed=1; break; fi
    git rebase --abort >/dev/null 2>&1 || true
    sleep $((i * 5))
  done
  if [ "$pushed" = 1 ]; then log "pushed"; else log "PUSH FAILED"; notify "Data commit could not be pushed - see $LOG"; fi
fi

pending=$(python3 -c "import json;print(json.load(open('data/last_run.json')).get('pending_after') or 0)" 2>/dev/null || echo 0)
industry=$(python3 -c "import json;print(json.load(open('data/last_run.json')).get('pending_industry') or 0)" 2>/dev/null || echo 0)
if [ "$code" = 1 ]; then
  notify "Refresh FAILED (data restored). See $LOG"
elif [ "$code" = 3 ]; then
  notify "Refresh finished with warnings. See $LOG"
elif [ "$((pending - industry))" -gt 0 ]; then
  notify "$((pending - industry)) new post(s) await translation. Ask Copilot: translate pending."
fi
log "done"
exit 0
