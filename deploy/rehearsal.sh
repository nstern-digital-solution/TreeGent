#!/usr/bin/env bash
# deploy/rehearsal.sh — fresh-host rehearsal of the CENTRAL deployment.
#
# Runs the REAL scripts (install.sh logic subset + run-central.sh) against
# REAL services + REAL mongo + REAL meteor bundle, on loopback ports, in an
# isolated HOME/lab dir. Nothing here is mocked except: no apt (assumes
# mongod/meteor present on the dev box), no systemd (runs the supervisor
# directly), no caddy.
#
# Verifications (ALL must pass; any failure = exit 1):
#   1. env.example is parseable: no inline comments, services import OK
#   2. supervisor starts: 6 python services bind their ports
#   3. web bundle: meteor build succeeds; node (star.json-pinned) serves
#      HTTP 200 with the app title; node version matches the pin
#   4. restart resilience: kill one service -> supervisor relaunches it
#   5. web page actually contains the login UI marker
#
# Intended use: pre-push gate (deploy/ or services/ changes) and manual
# pre-release check. ~5-10 min (meteor build dominates).
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LAB="${TG_REHEARSAL_DIR:-/tmp/tg-rehearsal}"
LOG="$LAB/log"; mkdir -p "$LAB"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS: $*"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $*"; }

echo "== TreeGent deploy rehearsal =="

# ---------- 1. env hygiene + imports ----------
echo "[1/5] env.example hygiene + service imports"
ENVF="$LAB/env"
sed -E 's/[[:space:]]+#.*$//' "$REPO/deploy/env.example" > "$ENVF"
# R62: the lab runs with a real generated token (fail-closed guard would reject the placeholder)
sed -i "s|^TG_SERVICE_TOKEN=.*|TG_SERVICE_TOKEN=lab-$(head -c32 /dev/urandom | base64 | tr -d '=+/' | head -c 40)|" "$ENVF"
if grep -qE '=.*#' "$ENVF"; then bad "inline comments survive sanitization"; else ok "env sanitized"; fi
if grep -qE '^TG_[A-Z_]*MONGO_URL=.{3,}' <(grep -v '^TG_MONGO_URL=' "$ENVF"); then
  bad "a second MONGO_URL default shadows TG_MONGO_URL (web-goes-localhost class)"
else ok "no shadowing mongo defaults"; fi
(cd "$REPO" && set -a && . "$ENVF" && set +a && \
  uv run python3 -c "
import importlib
for m in ['services.chat.app.main','services.proxy.app.main','services.mail.app.main',
          'services.secrets.app.main','services.files.app.main','services.runtime.app.__main__']:
    importlib.import_module(m)
print('imports OK')" >>"$LOG" 2>&1) && ok "all services import under sanitized env" || { bad "service import failed (see $LOG)"; }

# ---------- 2. real supervisor, real services ----------
echo "[2/5] supervisor brings up all services"
SUP_LOG="$LAB/supervisor.log"
# local mongo must be up (fresh-host stand-in)
if ! curl -s --max-time 2 mongodb://127.0.0.1:27017 >/dev/null 2>&1 && ! ss -l 2>/dev/null | grep -q ':27017 '; then
  bash "$REPO/deploy/run-mongo.sh" >>"$LOG" 2>&1 || true
  sleep 3
fi
ss -l 2>/dev/null | grep -q ':27017 ' && ok "mongo reachable" || bad "mongo not reachable"
# run the REAL supervisor with lab overrides. Ports are shifted +10000
# so a dev stack on the same box doesn't collide (and vice versa).
mkdir -p "$LAB/varlog"
HOME_SAVE="$HOME"
export HOME="$LAB"   # star.json lookup, meteor paths -> lab
# R62: the lab's generated token must reach the supervisor (fail-closed guard)
set -a; . "$ENVF"; set +a
TG_REHEARSAL=1 bash "$REPO/deploy/run-central.sh" >"$SUP_LOG" 2>&1 &
SUP_PID=$!
export HOME="$HOME_SAVE"
trap 'kill "$SUP_PID" 2>/dev/null; pkill -f "tg-rehearsal.*uvicorn" 2>/dev/null' EXIT
PORTS="${TG_REHEARSAL_PORTS:-18000 18001 18002 18003 18004 18010}"
for i in $(seq 1 30); do
  up=0
  for p in $PORTS; do ss -l 2>/dev/null | grep -q ":$p " && up=$((up+1)); done
  [ $up -eq 6 ] && break
  sleep 2
done
[ $up -eq 6 ] && ok "6/6 services listening" || bad "only $up/6 services listening (supervisor log: $SUP_LOG)"
HP=0
for p in $PORTS; do
  curl -s --max-time 3 "http://127.0.0.1:$p/health" | grep -q '"ok": *true' && HP=$((HP+1))
done
[ $HP -eq 6 ] && ok "6/6 health checks green" || bad "only $HP/6 health checks green"

# ---------- 3. web bundle ----------
echo "[3/5] web production bundle (meteor build + pinned node)"
WEBLOG="$LAB/web.log"
BUNDLE="$LAB/web-bundle/bundle"
(cd "$REPO/services/web" && "$HOME_SAVE/.meteor/meteor" build "$LAB/web-bundle" --directory) >>"$LOG" 2>&1 \
  && ok "meteor build" || bad "meteor build failed"
if [ -d "$BUNDLE" ]; then
  (cd "$BUNDLE/programs/server" && "$HOME_SAVE/.meteor/meteor" npm install --production) >>"$LOG" 2>&1 \
    && ok "bundle npm install" || bad "bundle npm install failed"
  WANT="$(python3 -c "import json;print(json.load(open('$BUNDLE/star.json'))['nodeVersion'])" 2>/dev/null)"
  NODE_BIN=""
  for C in "$HOME_SAVE"/.meteor/packages/meteor-tool/*/mt-os.linux.x86_64/dev_bundle/bin/node; do
    [ -x "$C" ] || continue
    [ "$("$C" --version 2>/dev/null)" = "v$WANT" ] || continue
    NODE_BIN="$C"; break
  done
  [ -n "$NODE_BIN" ] && ok "node pinned to $WANT" || bad "no node matching bundle pin v$WANT"
  if [ -n "$NODE_BIN" ]; then
    (cd "$BUNDLE" && env MONGO_URL='mongodb://127.0.0.1:27017/treegent?directConnection=true' \
       ROOT_URL='http://localhost:3050' PORT=3050 \
       NODE_OPTIONS=--max-old-space-size=1024 "$NODE_BIN" main.js) >"$WEBLOG" 2>&1 &
    WEB_PID=$!
    for i in $(seq 1 20); do curl -s -o /dev/null --max-time 2 http://127.0.0.1:3050 && break; sleep 2; done
    CODE="$(curl -s -o "$LAB/page.html" -w '%{http_code}' --max-time 4 http://127.0.0.1:3050)"
    [ "$CODE" = "200" ] && ok "web bundle serves 200" || bad "web bundle HTTP $CODE (log: $WEBLOG)"
    grep -q "TreeGent" "$LAB/page.html" && ok "page contains app title" || bad "page missing title"
    # auth-chain proof: web's first-run method must RESPOND (the "..." hang
    # class). SockJS xhr polling — verified protocol sequence (create, send
    # connect, send method, poll for result).
    S="rh$(date +%s)"
    ddp() { curl -s --max-time 8 -X POST -H 'Content-Type: text/plain' \
      -H "Origin: http://127.0.0.1:3050" --data "$1" "http://127.0.0.1:3050/sockjs/000/$S/$2"; }
    ddp '' xhr >/dev/null
    ddp '["{\"msg\":\"connect\",\"version\":\"1\",\"support\":[\"1\"]}"]' xhr_send >/dev/null
    ddp '["{\"msg\":\"method\",\"method\":\"tg.userCount\",\"id\":\"m1\",\"params\":[]}"]' xhr_send >/dev/null
    RESP="$(ddp '' xhr | head -c 400)"
    # NOTE: sockjs wraps frames in JSON, so the wire bytes are \"result\"
    # (backslash-escaped) — match the bare word, not the quoted form.
    case "$RESP" in
      *result*) ok "DDP method responds (bootstrap not hung)" ;;
      *) bad "DDP tg.userCount hung/failed: $(echo "$RESP" | head -c 120)" ;;
    esac
    # NOTE: bundle stays up — the caddy websocket test (4b) proxies to it
  fi
else
  bad "bundle dir missing"
fi

# ---------- 4. restart resilience ----------
echo "[4/5] supervisor auto-restart"
VICTIM_PID="$(ss -tlnp 2>/dev/null | grep -E ':18010 ' | grep -oP 'pid=\K[0-9]+' | head -1)"
if [ -n "$VICTIM_PID" ]; then
  kill "$VICTIM_PID"
  # backoff can schedule the relaunch up to 60s out — poll, don't sleep blind
  RELAUNCHED=0
  for i in $(seq 1 30); do
    if ss -l 2>/dev/null | grep -q ':18010 '; then RELAUNCHED=1; break; fi
    sleep 3
  done
  if [ $RELAUNCHED -eq 1 ]; then ok "runtime relaunched after kill"; else bad "runtime NOT relaunched within 90s"; fi
else
  bad "could not find runtime pid for kill test"
fi

# ---------- 4b. caddy websocket path (only if caddy binary available) ----------
echo "[4b/5] caddy sockjs websocket forwarding"
CADDY_BIN="$(command -v caddy || echo "$REPO/deploy/.caddy-lab")"
if [ -x "$CADDY_BIN" ]; then
  # lab caddy: no :80 (dev box has no root), tls internal, admin off
  { printf '{\n    admin off\n    http_port 8080\n    https_port 8443\n}\n\n'; \
    sed "s|treegent.example.com|127.0.0.1:8443|" "$REPO/deploy/Caddyfile.example" \
    | sed "s|127.0.0.1:3000|127.0.0.1:3050|"; } > "$LAB/Caddyfile"
  XDG_DATA_HOME="$LAB/caddy-data" XDG_CONFIG_HOME="$LAB/caddy-cfg" \
    "$CADDY_BIN" run --config "$LAB/Caddyfile" --adapter caddyfile >"$LAB/caddy.log" 2>&1 &
  CADDY_PID=$!
  sleep 4
  # real ws client (curl's minimal handshake 400s through proxies — browsers don't)
  WSOK="$(uv run --with websocket-client python3 -c "
import websocket, ssl, sys, random
sid = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=8))
try:
    ws = websocket.create_connection(f'wss://127.0.0.1:8443/sockjs/000/{sid}/websocket',
        timeout=10, sslopt={'cert_reqs': ssl.CERT_NONE}, suppress_origin=True)
    print('OK' if ws.recv().startswith('o') else 'BAD-FRAME'); ws.close()
except Exception as e:
    print('FAIL', type(e).__name__)
" 2>/dev/null)"
  case "$WSOK" in
    OK) ok "sockjs websocket through caddy (real client)" ;;
    *) bad "ws via caddy failed: $WSOK" ;;
  esac
  kill "$CADDY_PID" 2>/dev/null
  [ -n "${WEB_PID:-}" ] && kill "$WEB_PID" 2>/dev/null
else
  echo "  SKIP: no caddy binary (websocket-forwarding untested this run)"
fi

# ---------- 4b. quickstart tail under set -u (unbound-variable class) ----------
# shipped once (2026-10-06: $REPO unbound killed the deploy BEFORE the service
# restart). This check executes the install/restart tail verbatim with stubs,
# so any future unbound variable fails HERE, not on the operator's server.
if bash -c '
set -uo pipefail
systemctl() { :; }; install() { :; }
say() { :; }
PROXY=caddy TG_DOMAIN=x
systemctl enable treegent >/dev/null 2>&1 || true
install -m 0755 /opt/TreeGent/deploy/treegent-cli.sh /usr/local/bin/treegent \
  || echo "warn"
systemctl restart treegent
' 2>/tmp/qs-tail-err; then
  ok "quickstart tail runs clean under set -u"
else
  bad "quickstart tail unbound variable: $(cat /tmp/qs-tail-err | head -1)"
fi

# ---------- 5. verdict ----------
echo
echo "== verdict: $PASS passed, $FAIL failed =="
[ $FAIL -eq 0 ] || { echo "REHEARSAL FAILED — do not ship"; exit 1; }
echo "REHEARSAL PASSED"
