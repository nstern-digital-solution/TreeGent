#!/usr/bin/env bash
# Runs all central-host services under one systemd unit.
# Supervisor: fail-soft + auto-restart with backoff; per-service logs in
# /var/log/treegent/<name>.log (service stdout no longer vanishes).
# NOTE: deliberately NO `set -e` — a supervisor must not die on a false
# check (that bug shipped once; never again).
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
VENV="$REPO/.venv/bin"

LOGDIR=/var/log/treegent
mkdir -p "$LOGDIR" 2>/dev/null || LOGDIR=/tmp/treegent-logs
mkdir -p "$LOGDIR"

declare -A PID CMD LOG STARTS BACKOFF NEXT_RETRY LAST_START
ORDER=()

now() { date +%s; }

spawn() { # spawn <name> — first call launches; later calls mark dead + schedule retry
  local s="$1"
  LOG[$s]="$LOGDIR/$s.log"
  if [ -z "${NEXT_RETRY[$s]:-}" ]; then
    STARTS[$s]=1; BACKOFF[$s]=5; NEXT_RETRY[$s]=0
    _launch "$s"
    return 0
  fi
  STARTS[$s]=$(( ${STARTS[$s]} + 1 ))
  echo "[supervisor] $s died; restart #${STARTS[$s]} in ${BACKOFF[$s]}s (log: ${LOG[$s]})" >&2
  NEXT_RETRY[$s]=$(( $(now) + ${BACKOFF[$s]} ))
  BACKOFF[$s]=$(( ${BACKOFF[$s]} * 2 ))
  [ "${BACKOFF[$s]}" -gt 60 ] && BACKOFF[$s]=60
}

_launch() { # start the process for service s now
  local s="$1"
  LAST_START[$s]=$(now)
  eval "${CMD[$s]}" >>"${LOG[$s]}" 2>&1 &
  PID[$s]=$!
  echo "[supervisor] $s started pid ${PID[$s]} (log: ${LOG[$s]})" >&2
}

term() {
  for s in "${ORDER[@]}"; do
    [ -n "${PID[$s]:-}" ] && kill "${PID[$s]}" 2>/dev/null
  done
  wait 2>/dev/null
  exit 0
}
trap term TERM INT

ORDER=(chat proxy mail secrets files runtime)
CMD[chat]="$VENV/uvicorn services.chat.app.main:app --host 127.0.0.1 --port 8000"
CMD[proxy]="$VENV/uvicorn services.proxy.app.main:app --host 127.0.0.1 --port 8001"
CMD[mail]="$VENV/uvicorn services.mail.app.__main__:app --host 127.0.0.1 --port 8002"
CMD[secrets]="$VENV/uvicorn services.secrets.app.__main__:app --host 127.0.0.1 --port 8003"
CMD[files]="$VENV/uvicorn services.files.app.__main__:app --host 127.0.0.1 --port 8004"
CMD[runtime]="$VENV/uvicorn services.runtime.app.__main__:app --host 127.0.0.1 --port 8010"
for s in chat proxy mail secrets files runtime; do spawn "$s"; done

# ---- web (Meteor) ----
MU="${TG_WEB_MONGO_URL:-${TG_MONGO_URL:-mongodb://127.0.0.1:27017}}"
BASE="${MU%%\?*}"
QUERY="${MU#*\?}"
[ "$QUERY" = "$MU" ] && QUERY=""
BASE="${BASE%/}"
case "$BASE" in
  *"/treegent") : ;;
  *) BASE="$BASE/treegent" ;;
esac
if [ -n "$QUERY" ]; then MU="$BASE?$QUERY"; else MU="$BASE"; fi

METEOR_BIN="$(command -v meteor || true)"
if [ -z "$METEOR_BIN" ] && [ -x "/home/treegent/.meteor/meteor" ]; then
  METEOR_BIN="/home/treegent/.meteor/meteor"
fi
if [ -n "$METEOR_BIN" ]; then
  ORDER+=(web)
  # cap node heap: the first build on a small box can otherwise pressure-kill neighbors
  CMD[web]="env NODE_OPTIONS=--max-old-space-size=1536 MONGO_URL=$MU $METEOR_BIN --production --port 3000"
  spawn web
else
  echo "[supervisor] meteor not found — web UI not started" >&2
fi

# ---- supervisor loop: detect deaths, retry with backoff, heal ----
while :; do
  T=$(now)
  for s in "${ORDER[@]}"; do
    [ -z "${PID[$s]:-}" ] && continue
    if kill -0 "${PID[$s]}" 2>/dev/null; then
      # healthy for >5 min => this life counts as stable; reset the ladder
      if [ $(( T - ${LAST_START[$s]} )) -gt 300 ]; then
        BACKOFF[$s]=5; STARTS[$s]=1
      fi
      continue
    fi
    # dead: first observation marks it and schedules a retry
    if [ "${NEXT_RETRY[$s]:-0}" -eq 0 ]; then
      spawn "$s"
    elif [ "${NEXT_RETRY[$s]}" -le "$T" ]; then
      _launch "$s"
      NEXT_RETRY[$s]=0
    fi
  done
  sleep 5
done
