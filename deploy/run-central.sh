#!/usr/bin/env bash
# Runs all central-host services under one systemd unit (supervision by
# systemd; simple process per service, loopback only).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
VENV="$REPO/.venv/bin"

pids=()
term() { for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; wait; }
trap term TERM INT

$VENV/uvicorn services.chat.app.main:app --host 127.0.0.1 --port 8000 &
pids+=($!)
$VENV/uvicorn services.proxy.app.main:app --host 127.0.0.1 --port 8001 &
pids+=($!)
$VENV/uvicorn services.mail.app.__main__:app --host 127.0.0.1 --port 8002 &
pids+=($!)
$VENV/uvicorn services.secrets.app.__main__:app --host 127.0.0.1 --port 8003 &
pids+=($!)
$VENV/uvicorn services.files.app.__main__:app --host 127.0.0.1 --port 8004 &
pids+=($!)
$VENV/uvicorn services.runtime.app.__main__:app --host 127.0.0.1 --port 8010 &
pids+=($!)

cd "$REPO/services/web"
METEOR_PORT=3000
if command -v meteor >/dev/null; then
  meteor --production --port "$METEOR_PORT" &
  pids+=($!)
else
  echo "meteor not found — web UI not started" >&2
fi

wait -n
term
