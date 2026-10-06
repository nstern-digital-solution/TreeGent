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

# propagate shared env to children (systemd EnvironmentFile vars are NOT inherited
# by grandchildren unless exported — this bug baked 127.0.0.1 URLs into agent hosts
# at provision time because the runtime read TG_PUBLIC_URL with a silent fallback)
export TG_MONGO_URL TG_PUBLIC_URL TG_SERVICE_TOKEN TG_MAIL_DOMAIN 2>/dev/null || true

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
# rehearsal mode shifts ports explicitly (TG_REHEARSAL=1) to avoid
# colliding with a dev stack on the same box. NEVER build ports by string
# concat — 18+8010 = 188010 > 65535 (shipped bug, caught by rehearsal).
P_CHAT=8000; P_PROXY=8001; P_MAIL=8002; P_SEC=8003; P_FILES=8004; P_RT=8010; P_WEB=3000
if [ "${TG_REHEARSAL:-0}" = "1" ]; then
  P_CHAT=18000; P_PROXY=18001; P_MAIL=18002; P_SEC=18003; P_FILES=18004; P_RT=18010; P_WEB=13000
fi
CMD[chat]="$VENV/uvicorn services.chat.app.main:app --host 127.0.0.1 --port $P_CHAT"
CMD[proxy]="$VENV/uvicorn services.proxy.app.main:app --host 127.0.0.1 --port $P_PROXY"
CMD[mail]="$VENV/uvicorn services.mail.app.__main__:app --host 127.0.0.1 --port $P_MAIL"
CMD[secrets]="$VENV/uvicorn services.secrets.app.__main__:app --host 127.0.0.1 --port $P_SEC"
CMD[files]="$VENV/uvicorn services.files.app.__main__:app --host 127.0.0.1 --port $P_FILES"
CMD[runtime]="$VENV/uvicorn services.runtime.app.__main__:app --host 127.0.0.1 --port $P_RT"
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

# web: PRODUCTION bundle with plain node (built by install.sh).
# Fallback: meteor dev server only if the bundle is missing.
WEB_BUNDLE="$REPO/web-bundle/bundle"
# Node version: prefer the one the bundle itself pins (star.json engines).
# Meteor bundles are NOT portable across node majors (v24 crashes a v22 bundle).
NODE_BIN=""
if [ -f "$WEB_BUNDLE/star.json" ]; then
  WANT_NODE="$(python3 -c "import json;print(json.load(open('$WEB_BUNDLE/star.json'))['nodeVersion'])" 2>/dev/null || true)"
fi
for CAND in "$HOME"/.meteor/packages/meteor-tool/*/mt-os.linux.x86_64/dev_bundle/bin/node \
           /home/treegent/.meteor/packages/meteor-tool/*/mt-os.linux.x86_64/dev_bundle/bin/node; do
  [ -x "$CAND" ] || continue
  CV="$("$CAND" --version 2>/dev/null || echo v0)"
  if [ -n "${WANT_NODE:-}" ] && [ "$CV" = "$WANT_NODE" ]; then NODE_BIN="$CAND"; break; fi
  [ -z "$NODE_BIN" ] && NODE_BIN="$CAND"
done
[ -z "$NODE_BIN" ] && NODE_BIN="$(command -v node || true)"
ROOT_URL="${TG_PUBLIC_URL:-http://localhost:3000}"
if [ -d "$WEB_BUNDLE" ] && [ -n "$NODE_BIN" ]; then
  ORDER+=(web)
  CMD[web]="cd $WEB_BUNDLE && env MONGO_URL=$MU ROOT_URL=$ROOT_URL PORT=$P_WEB TG_SERVICE_TOKEN=${TG_SERVICE_TOKEN:-dev-service-token} NODE_OPTIONS=--max-old-space-size=1024 $NODE_BIN main.js"
  spawn web
else
  METEOR_BIN="$(command -v meteor || true)"
  if [ -z "$METEOR_BIN" ] && [ -x "/home/treegent/.meteor/meteor" ]; then
    METEOR_BIN="/home/treegent/.meteor/meteor"
  fi
  if [ -n "$METEOR_BIN" ]; then
    ORDER+=(web)
    echo "[supervisor] WARN: production bundle missing — using meteor dev server" >&2
    CMD[web]="cd $REPO/services/web && env NODE_OPTIONS=--max-old-space-size=1536 MONGO_URL=$MU ROOT_URL=$ROOT_URL $METEOR_BIN --production --port 3000"
    spawn web
  else
    echo "[supervisor] no web runtime — web UI not started" >&2
  fi
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
