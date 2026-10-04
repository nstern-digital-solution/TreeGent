#!/usr/bin/env bash
# Runs all central-host services under one systemd unit (supervision by
# systemd; simple process per service, loopback only).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
VENV="$REPO/.venv/bin"

pids=()
cmds=()
term() { for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; wait; }
trap term TERM INT
FAILURES=0

$VENV/uvicorn services.chat.app.main:app --host 127.0.0.1 --port 8000 &
pids+=($!); cmds+=("chat")
$VENV/uvicorn services.proxy.app.main:app --host 127.0.0.1 --port 8001 &
pids+=($!); cmds+=("proxy")
$VENV/uvicorn services.mail.app.__main__:app --host 127.0.0.1 --port 8002 &
pids+=($!); cmds+=("mail")
$VENV/uvicorn services.secrets.app.__main__:app --host 127.0.0.1 --port 8003 &
pids+=($!); cmds+=("secrets")
$VENV/uvicorn services.files.app.__main__:app --host 127.0.0.1 --port 8004 &
pids+=($!); cmds+=("files")
$VENV/uvicorn services.runtime.app.__main__:app --host 127.0.0.1 --port 8010 &
pids+=($!); cmds+=("runtime")

cd "$REPO/services/web"
METEOR_PORT=3000
# Meteor: db name must be IN the URL (default /meteor otherwise); use the
# same Mongo the services use, from the shared env file.
MU="${TG_WEB_MONGO_URL:-${TG_MONGO_URL:-mongodb://127.0.0.1:27017}}"
BASE="${MU%%\?*}"          # everything before the first ?
QUERY="${MU#*\?}"          # everything after (may equal MU if no ?)
[ "$QUERY" = "$MU" ] && QUERY=""
BASE="${BASE%/}"           # strip trailing slash (Atlas SRV strings have one)
case "$BASE" in
  *"/treegent") : ;;       # db name already present
  *) BASE="$BASE/treegent" ;;
esac
if [ -n "$QUERY" ]; then MU="$BASE?$QUERY"; else MU="$BASE"; fi
if command -v meteor >/dev/null; then
  MONGO_URL="$MU" meteor --production --port "$METEOR_PORT" &
  pids+=($!); cmds+=("web")
else
  echo "meteor not found — web UI not started" >&2
fi

# fail-SOFT: one service dying no longer kills the unit; systemd sees
# the supervisor alive. Real deps (mongo) surface as /health failures.
while :; do
  for i in "${!pids[@]}"; do
    if ! kill -0 "${pids[$i]}" 2>/dev/null; then
      echo "[supervisor] process ${pids[$i]} (${cmds[$i]:-?}) exited" >&2
      unset "pids[$i]"; unset "cmds[$i]"
      FAILURES=$((FAILURES+1))
    fi
  done
  [ ${#pids[@]} -eq 0 ] && { echo "[supervisor] all services exited" >&2; exit 1; }
  sleep 5
done
