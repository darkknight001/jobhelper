#!/usr/bin/env bash
# Deploy JobHelper to the home server: local tests must pass, settings.yaml must match the server's
# (the server copy may hold edits made there), then copy the code, rebuild, and re-run the tests there.
#   scripts/deploy.sh            normal deploy
#   scripts/deploy.sh --force    deploy even though settings.yaml differs (after you've checked the diff)
# data/ (database, profile, knowledge base, resumes) and deploy/.env never leave the server.
set -euo pipefail

HOST="${JOBHELPER_HOST:?set JOBHELPER_HOST to your server ssh name}"
STACK="${JOBHELPER_STACK:-/opt/stacks/jobhelper}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/Scripts/python"; [ -x "$PY" ] || PY="$REPO/.venv/bin/python"
FORCE="${1:-}"

echo "== local tests"
"$PY" -m pytest -q --rootdir "$REPO" "$REPO/tests" | tail -1

echo "== settings.yaml vs server"
if ! ssh "$HOST" "cat $STACK/config/settings.yaml" | diff - "$REPO/config/settings.yaml"; then
  [ "$FORCE" = "--force" ] || { echo "settings.yaml differs from the server (above: < server, > local). Re-run with --force once that's intended."; exit 1; }
fi

echo "== running applications (a deploy restarts the browser; they are re-queued)"
ssh "$HOST" "docker exec -e PYTHONPATH=/app -w /app jobhelper python -c \"from sqlalchemy import select; from jobhelper import db
with db.session() as s: print([a.id for a in s.scalars(select(db.Application).where(db.Application.status == 'applying'))] or 'none')\"" 2>/dev/null | tail -1 || true

echo "== copy"
TAR="$(mktemp -d)/jh.tgz"
(cd "$REPO/.." && tar --force-local -czf "$TAR" \
  --exclude="$(basename "$REPO")/.venv" --exclude="$(basename "$REPO")/data" --exclude="$(basename "$REPO")/deploy/.env" \
  --exclude="$(basename "$REPO")/.pytest_cache" --exclude="$(basename "$REPO")/dist" --exclude='__pycache__' \
  --exclude="$(basename "$REPO")/.claude/launch.json" --exclude="$(basename "$REPO")/.env" \
  "$(basename "$REPO")")
REMOTE_TAR="$(ssh "$HOST" "mktemp /tmp/jh.XXXXXX.tgz")"   # unique and owned by us, not a guessable /tmp name
scp -q "$TAR" "$HOST:$REMOTE_TAR"
rm -f "$TAR"

echo "== rebuild on $HOST"
# Code folders are replaced, not merged, so files deleted here don't linger there.
# data/, config/ and deploy/ (which holds .env) are never removed.
ssh "$HOST" "set -e; cd '$STACK' && rm -rf jobhelper tests docs scripts
cd \"\$(dirname '$STACK')\" && tar -xzf '$REMOTE_TAR' --transform 's,^[^/]*,$(basename "$STACK"),' && rm -f '$REMOTE_TAR'
cd '$STACK' && docker compose up -d --build >/dev/null 2>&1
for i in \$(seq 1 40); do docker logs --since 3m jobhelper 2>&1 | grep -q 'Telegram bot connected' && break; sleep 3; done
docker logs --since 3m jobhelper 2>&1 | grep -q 'Telegram bot connected' && echo 'bot connected' || echo 'WARNING: bot not connected yet'
echo '== server tests'; docker exec jobhelper python -m pytest -q /app/tests 2>&1 | tail -1"
