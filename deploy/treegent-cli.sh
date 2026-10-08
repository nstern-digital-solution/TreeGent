#!/usr/bin/env bash
# treegent CLI — operator helper on the shared (central) server.
#   treegent status   — service, version, ports, recent errors
#   treegent update   — git pull + uv sync + restart (one command, no assistant)
# Install: deploy/quickstart.sh symlinks this to /usr/local/bin/treegent.
set -uo pipefail

REPO="/opt/TreeGent"
VENV="$REPO/.venv"
ENV_FILE="/etc/treegent/env"

c_green=$'\033[32m'; c_red=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s%s%s\n' "$c_green" "$*" "$c_off"; }
err()  { printf '%s%s%s\n' "$c_red" "$*" "$c_off" >&2; }
dim()  { printf '%s%s%s\n' "$c_dim" "$*" "$c_off"; }

need_repo() {
  if [ ! -d "$REPO" ]; then
    err "no checkout at $REPO — is this the shared server?"
    exit 1
  fi
}

cmd_status() {
  need_repo
  say "TreeGent — $(hostname)"

  # version: local HEAD + remote tip
  local head ahead
  head=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null) || head="?"
  ahead=$(git -C "$REPO" rev-list --count "$head..origin/main" 2>/dev/null) || ahead="?"
  say "  version:   $head  (remote ahead by: $ahead commit(s))"

  # service
  if systemctl is-active --quiet treegent; then
    ok "  service:   treegent active (since $(systemctl show -p ActiveEnterTimestamp --value treegent))"
  else
    err "  service:   treegent $(systemctl is-active treegent 2>/dev/null)"
  fi

  # ports (chat 8000 proxy 8001 mail 8002 secrets 8003 files 8004)
  say "  ports:"
  for p in 8000 8001 8002 8003 8004; do
    if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$p/health" \
        || curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$p/docs"; then
      ok "    :$p  listening"
    else
      err "    :$p  no answer"
    fi
  done

  # mongo
  if systemctl is-active --quiet mongod 2>/dev/null; then
    ok "  mongo:     up (systemd)"
  elif pgrep -x mongod >/dev/null 2>&1; then
    ok "  mongo:     up (manual)"
  else
    err "  mongo:     down"
  fi

  # recent errors from the supervisor logs
  say "  recent errors (last 30 min):"
  local hits
  hits=$(grep -h -E "ERROR|CRASHED|error" /var/log/treegent/*.log 2>/dev/null \
         | awk -v cutoff="$(date -d '30 min ago' '+%Y-%m-%dT%H:%M' 2>/dev/null)" \
             '$0 >= cutoff' | tail -5)
  # log lines carry no timestamps in all services — fall back to plain tail
  if [ -z "$hits" ]; then
    hits=$(grep -h -E "ERROR|CRASHED" /var/log/treegent/*.log 2>/dev/null | tail -3)
  fi
  if [ -n "$hits" ]; then
    err "$hits" | sed 's/^/    /'
  else
    ok "    none"
  fi
  exit 0
}

# locate tools the way installers placed them (non-login shells lack them)
find_tools() {
  UV_BIN="$(command -v uv || true)"
  [ -z "$UV_BIN" ] && [ -x /root/.local/bin/uv ] && UV_BIN=/root/.local/bin/uv
  [ -z "$UV_BIN" ] && [ -x /home/treegent/.local/bin/uv ] && UV_BIN=/home/treegent/.local/bin/uv
  METEOR_BIN="$(command -v meteor || true)"
  [ -z "$METEOR_BIN" ] && [ -x /home/treegent/.meteor/meteor ] && METEOR_BIN=/home/treegent/.meteor/meteor
}

# Build the production web bundle into $REPO/web-bundle.
# install.sh chowns the repo to the service user and runs meteor as THAT user
# (meteor writes .meteor/local under $HOME and refuses under a different
# owner — running as root prints 'sudo chown -Rh <user> .meteor/local' and
# exits 1, exactly the first-ever `treegent update` failure on prod).
# Also installs the bundle's server-side node_modules, which
# `meteor build --directory` does NOT do — without it node main.js dies on
# bare 'require'. Log kept at /var/log/treegent/web-bundle.log.
build_web_bundle() {
  WEBLOG="/var/log/treegent/web-bundle.log"
  mkdir -p /var/log/treegent 2>/dev/null || WEBLOG="/tmp/treegent-web-bundle.log"
  SYSU="treegent"
  id -u "$SYSU" >/dev/null 2>&1 || SYSU=root   # never installed? build anyway
  : > "$WEBLOG"
  echo "[web-bundle $(date -Is)] build start (as $SYSU)" >> "$WEBLOG"

  # uv sync (step 2) ran as root and may have left root-owned files in the
  # treegent-owned tree; hand the whole repo back to the service user first.
  if [ "$SYSU" != root ]; then
    chown -R "$SYSU:$SYSU" "$REPO" >> "$WEBLOG" 2>&1
  fi

  rm -rf "$REPO/web-bundle"
  if [ "$SYSU" = root ]; then
    (cd "$REPO/services/web" \
       && "$METEOR_BIN" npm install --silent \
       && "$METEOR_BIN" build --directory "$REPO/web-bundle") >> "$WEBLOG" 2>&1
  else
    (cd "$REPO/services/web" \
       && sudo -u "$SYSU" "$METEOR_BIN" npm install --silent \
       && sudo -u "$SYSU" "$METEOR_BIN" build --directory "$REPO/web-bundle") >> "$WEBLOG" 2>&1
  fi || { echo "[web-bundle] meteor build FAILED" >> "$WEBLOG"; return 1; }

  # server-side deps for the built bundle (meteor build --directory skips them)
  if [ -d "$REPO/web-bundle/bundle/programs/server" ]; then
    if [ "$SYSU" = root ]; then
      (cd "$REPO/web-bundle/bundle/programs/server" \
         && "$METEOR_BIN" npm install --production) >> "$WEBLOG" 2>&1
    else
      (cd "$REPO/web-bundle/bundle/programs/server" \
         && sudo -u "$SYSU" "$METEOR_BIN" npm install --production) >> "$WEBLOG" 2>&1
    fi || { echo "[web-bundle] server npm install FAILED" >> "$WEBLOG"; return 1; }
  else
    echo "[web-bundle] bundle/programs/server missing after build" >> "$WEBLOG"
    return 1
  fi
  echo "[web-bundle $(date -Is)] done" >> "$WEBLOG"
  return 0
}

cmd_update() {
  need_repo
  find_tools
  say "== TreeGent update =="

  say "1/5 git pull"
  # egg-info is build metadata uv sync regenerates in place; when a checkout
  # predates R68's untracking it is still tracked and its regenerated state
  # blocks the pull (the prod egg-info incident). Untracked at HEAD = lossless.
  rm -rf "$REPO/packages/treegent-common/treegent_common.egg-info"
  if ! git -C "$REPO" pull --ff-only origin main 2>&1 | sed 's/^/    /'; then
    err "git pull failed — fix manually: git -C $REPO status"
    exit 1
  fi

  say "2/5 deps (uv sync)"
  if [ -n "$UV_BIN" ]; then
    (cd "$REPO" && "$UV_BIN" sync 2>&1 | tail -2 | sed 's/^/    /') \
      || { err "uv sync failed — see above; refusing to continue"; exit 1; }
  else
    err "uv not found (looked in PATH, /root/.local/bin, /home/treegent/.local/bin)"
    err "deps NOT updated — install uv (curl -LsSf https://astral.sh/uv/install.sh | sh)"
    exit 1
  fi

  say "3/5 web bundle"
  if [ -n "$METEOR_BIN" ]; then
    if build_web_bundle; then
      ok "    web bundle rebuilt (+ server deps)"
    else
      err "web bundle build failed — web UI is now STALE; refusing to continue"
      err "    full log: ${WEBLOG:-?} — last lines:"
      tail -15 "${WEBLOG:-/dev/null}" 2>/dev/null | sed 's/^/      /' >&2
      exit 1
    fi
  else
    err "meteor not found (looked in PATH, /home/treegent/.meteor)"
    err "web bundle NOT rebuilt — web UI would be stale; refusing to continue"
    exit 1
  fi

  say "4/5 restart treegent"
  if systemctl restart treegent; then
    ok "    restarted"
  else
    err "    restart failed — see: journalctl -u treegent -n 50"
    exit 1
  fi

  say "5/5 health"
  local i up=false
  for i in $(seq 1 30); do
    if curl -s -o /dev/null --max-time 2 http://127.0.0.1:8000/health; then up=true; break; fi
    sleep 1
  done
  if [ "$up" = true ]; then
    ok "    chat :8000 healthy"
  else
    err "    chat did not come up in 30s — see: journalctl -u treegent -n 50"
    exit 1
  fi

  local head
  head=$(git -C "$REPO" rev-parse --short HEAD)
  ok "update done — now on $head"
  dim "agent hosts: Hosts tab -> update (per host) to pick up runtime changes"
  exit 0
}

case "${1:-}" in
  status) cmd_status ;;
  update) cmd_update ;;
  *)
    say "usage: treegent {status|update}"
    say "  status  service health, version, ports, recent errors"
    say "  update  git pull + deps + web bundle + restart + health check"
    exit 1
    ;;
esac
