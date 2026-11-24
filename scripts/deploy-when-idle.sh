#!/bin/bash
# Run a deploy that the queue check held, once the server's queue is empty.
#
# A push to main runs CI and then Deploy; Deploy stops (and shows as failed) while the
# download queue holds jobs, and the old version keeps running. This waits for that Deploy,
# then asks the server every five minutes whether the queue is empty, and runs the failed
# Deploy again when it is. It never forces a deploy past the check.
#
#   scripts/deploy-when-idle.sh <ssh host> [commit, default: origin/main]
#
# Needs `gh` (logged in) and ssh access to the server; the service lives in ~/kiri there.

set -u
HOST="${1:?usage: scripts/deploy-when-idle.sh <ssh host> [commit]}"
GH="$(command -v gh || echo "/c/Program Files/GitHub CLI/gh.exe")"
cd "$(dirname "$0")/.." || exit 1
SHA="$(git rev-parse "${2:-origin/main}")" || exit 1
HOURS=12

run_of() { "$GH" run list -L 10 --workflow "$1" --json databaseId,headSha -q ".[]|select(.headSha==\"$SHA\")|.databaseId" | head -1; }
conclusion() { "$GH" run view "$1" --json conclusion -q .conclusion; }
idle() {
  ssh -o ConnectTimeout=20 "$HOST" 'cd ~/kiri && docker compose run --rm --no-deps -T kiri /app/.venv/bin/python -m backend.check_state --require-idle < /dev/null' > /dev/null 2>&1
}

ci="$(run_of CI)"; [ -n "$ci" ] || { echo "no CI run for ${SHA:0:7}"; exit 1; }
"$GH" run watch "$ci" --exit-status > /dev/null 2>&1
[ "$(conclusion "$ci")" = success ] || { echo "CI: $(conclusion "$ci")"; exit 1; }

deploy=""
for _ in $(seq 1 20); do deploy="$(run_of Deploy)"; [ -n "$deploy" ] && break; sleep 15; done
[ -n "$deploy" ] || { echo "no Deploy run for ${SHA:0:7}"; exit 1; }
"$GH" run watch "$deploy" --exit-status > /dev/null 2>&1

if [ "$(conclusion "$deploy")" != success ]; then
  waited=0
  until idle; do
    waited=$((waited + 1))
    [ "$waited" -le $((HOURS * 12)) ] || { echo "queue still busy after $HOURS hours, deploy not run"; exit 1; }
    sleep 300
  done
  [ "$(git ls-remote origin refs/heads/main | cut -f1)" = "$SHA" ] || { echo "main has moved, deploy not run"; exit 1; }
  # A run that somebody started again meanwhile cannot be rerun: then only watch it.
  "$GH" run rerun "$deploy" --failed > /dev/null 2>&1
  sleep 20
  "$GH" run watch "$deploy" --exit-status > /dev/null 2>&1
fi
echo "Deploy of ${SHA:0:7}: $(conclusion "$deploy")"
ssh "$HOST" 'docker ps --format "{{.Names}} {{.Status}}" | grep "^kiri "'
