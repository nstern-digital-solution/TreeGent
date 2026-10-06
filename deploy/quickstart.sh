#!/usr/bin/env bash
# TreeGent quickstart — ONE command, any fresh Linux box.
#
#   curl -fsSL https://raw.githubusercontent.com/nstern-digital-solution/TreeGent/main/deploy/quickstart.sh | bash
#
# Asks a handful of questions, then installs and starts everything.
# Idempotent: re-run to reconfigure. sudo access required.
set -euo pipefail

say()  { printf '\033[1;36m== %s\033[0m\n' "$*"; }
confirm_secret() { # feedback for hidden input without exposing the value
  if [ -z "$1" ]; then
    echo "  -> left empty (skip / use default)" >&2
  elif [[ "$1" =~ ^(mongodb(\+srv)?|https?)://[^@]*@(.*)$ ]]; then
    echo "  -> OK: ${BASH_REMATCH[1]}://***@${BASH_REMATCH[3]:0:50}" >&2
  else
    echo "  -> OK: received ${#1} characters" >&2
  fi
}
ask_secret_or_default() { # ask_secret_or_default VAR PROMPT DEFAULT (no echo; empty = default)
  local __v
  read -r -s -p "$2 [$3]: " __v </dev/tty || true
  printf '\n' >&2
  confirm_secret "${__v:-$3}"
  printf -v "$1" '%s' "${__v:-$3}"
}
ask() { # ask VAR PROMPT DEFAULT
  local __v
  if [ -n "${BASH_VERSION:-}" ]; then
    read -r -p "$2 [$3]: " __v </dev/tty || true
  fi
  __v="${__v:-$3}"
  printf -v "$1" '%s' "$__v"
}
ask_secret() { # ask_secret VAR PROMPT (no default, no echo)
  local __v
  read -r -s -p "$2: " __v </dev/tty || true
  echo >&2
  confirm_secret "$__v"
  printf -v "$1" '%s' "$__v"
}

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }

say "TreeGent quickstart"
say "multi-agent company — central host installer"
say "press Enter everywhere for defaults — anything skipped now"
say "can be added later by re-running this script"

# ---------- config file (edit freely, save, resume anytime) ----------
CONF_DIR=/etc/treegent
CONF=$CONF_DIR/treegent.conf
ENVF=/opt/TreeGent/.env
mkdir -p "$CONF_DIR"

prefill() { [ -f "$ENVF" ] && grep -m1 "^$1=" "$ENVF" | cut -d= -f2- || true; }

if [ ! -f "$CONF" ]; then
  say "writing config template: $CONF"
  cat > "$CONF" <<TMPL
# TreeGent deployment config
# Edit any order, leave empty what you skip, save + exit to continue.
# Re-run the quickstart anytime: it opens this file again and re-applies.
# Values already detected from a previous install are prefilled below.

# Public domain for the web UI (empty = http://<server-ip>:3000)
TG_DOMAIN="$(prefill TG_DOMAIN)"

# MongoDB — empty = install a bundled local replica set on this box.
# Or paste ANY connection string (mongodb:// or mongodb+srv://, e.g. Atlas).
TG_MONGO_URL="$(prefill TG_MONGO_URL)"

# S3 for the files service — leave ALL FOUR empty to skip (add later).
TG_FILES_S3_ENDPOINT="$(prefill TG_FILES_S3_ENDPOINT)"
TG_FILES_S3_BUCKET="$(prefill TG_FILES_S3_BUCKET)"
TG_FILES_S3_ACCESS_KEY="$(prefill TG_FILES_S3_ACCESS_KEY)"
TG_FILES_S3_SECRET_KEY="$(prefill TG_FILES_S3_SECRET_KEY)"

# Outbound mail via Resend — empty = dev sink (captured, not sent).
TG_RESEND_API_KEY="$(prefill RESEND_API_KEY)"
# mail domain: default = first verified Resend sending domain, else public URL host
TG_MAIL_DOMAIN="$(prefill TG_MAIL_DOMAIN)"
if [ -z "$TG_MAIL_DOMAIN" ] && [ -n "$TG_RESEND_API_KEY" ]; then
  TG_MAIL_DOMAIN="$(curl -fsS --max-time 10 -H "Authorization: Bearer $TG_RESEND_API_KEY" \
    https://api.resend.com/domains | python3 -c 'import json,sys;
ds=[d.get("name") for d in json.load(sys.stdin).get("data",[]) if d.get("status")=="verified"]
print(ds[0] if ds else "")' 2>/dev/null || true)"
fi
if [ -z "$TG_MAIL_DOMAIN" ] && [ -n "${TG_PUBLIC_URL:-}" ]; then
  TG_MAIL_DOMAIN="$(echo "$TG_PUBLIC_URL" | sed 's|^https\?://||; s|/.*||; s|:.*||')"
fi
TMPL
  chmod 600 "$CONF"
fi

validate() { # format checks; sets PROBLEMS (empty = clean)
  PROBLEMS=""
  if [ -n "${TG_MONGO_URL:-}" ] && ! [[ "${TG_MONGO_URL}" =~ ^mongodb(\+srv)?:// ]]; then
    PROBLEMS="$PROBLEMS
- TG_MONGO_URL must start with mongodb:// or mongodb+srv://"
  fi
  if [ -n "${TG_FILES_S3_ENDPOINT:-}" ] && ! [[ "${TG_FILES_S3_ENDPOINT}" =~ ^https?:// ]]; then
    say "note: S3 endpoint has no scheme — https:// will be assumed"
  fi
  local n=0
  for v in TG_FILES_S3_ENDPOINT TG_FILES_S3_BUCKET TG_FILES_S3_ACCESS_KEY TG_FILES_S3_SECRET_KEY; do
    [ -n "${!v:-}" ] && n=$((n+1))
  done
  if [ "$n" -gt 0 ] && [ "$n" -lt 4 ]; then
    PROBLEMS="$PROBLEMS
- S3 is partially configured ($n/4 values) — fill all four or empty all four"
  fi
}

if [ -e /dev/tty ]; then
  while :; do
    say "opening $CONF in your editor — edit, then save & exit to continue"
    "${EDITOR:-$(command -v nano || command -v vi || echo vi)}" "$CONF" </dev/tty >/dev/tty 2>&1 || true
    set -a; . "$CONF"; set +a
    validate
    if [ -z "$PROBLEMS" ]; then break; fi
    say "problems found:"
    echo "$PROBLEMS" >&2
    local_r=""
    read -r -p "press 'e' to edit again, Enter to continue anyway, 'a' to abort: " local_r </dev/tty || true
    [ "$local_r" = "a" ] && exit 1
    [ "$local_r" = "e" ] && continue
    break
  done
else
  say "no terminal — using $CONF as-is (edit it and re-run to change)"
  set -a; . "$CONF"; set +a
fi

if [ -z "${TG_MONGO_URL:-}" ]; then TG_MONGO_MODE=bundled; else TG_MONGO_MODE=external; fi
if [ -n "${TG_DOMAIN:-}" ]; then
  SCHEME="https"; PROXY="caddy"
else
  SCHEME="http"; PROXY="none"
fi
# internal token: kept from previous install if any, else generated silently
TG_SERVICE_TOKEN="$(prefill TG_SERVICE_TOKEN)"
[ -n "$TG_SERVICE_TOKEN" ] || TG_SERVICE_TOKEN="$(head -c32 /dev/urandom | base64 | tr -d '=+/' | head -c 40)"

# ---------- install ----------
say "installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq curl git ca-certificates >/dev/null

say "cloning TreeGent"
REPO_DIR="/opt/TreeGent"
if [ -d "$REPO_DIR" ]; then
  say "repo exists — pulling latest"
  # repo dir is owned by the service user (meteor writes build dirs there);
  # git as root needs an explicit trust or it refuses AND we must NOT hide it
  git config --global --add safe.directory "$REPO_DIR" 2>/dev/null || true
  if ! git -C "$REPO_DIR" pull --ff-only; then
    say "ERROR: git pull failed — fix git access and re-run (NOT deploying stale code)"
    exit 1
  fi
else
  git clone -q https://github.com/nstern-digital-solution/TreeGent.git "$REPO_DIR"
fi
cd "$REPO_DIR"

say "writing configuration"
TOKEN_FILE="$REPO_DIR/.env"
if [ ! -f "$TOKEN_FILE" ]; then
  cp deploy/env.example "$TOKEN_FILE"
fi
set_kv() { grep -q "^$1=" "$TOKEN_FILE" \
  && sed -i "s|^$1=.*|$1=$2|" "$TOKEN_FILE" \
  || echo "$1=$2" >> "$TOKEN_FILE"; }   # append-if-missing: sed alone no-ops on absent lines
set_kv TG_SERVICE_TOKEN "$TG_SERVICE_TOKEN"
[ -n "${TG_MONGO_URL:-}" ] && set_kv TG_MONGO_URL "$TG_MONGO_URL"
# kill the legacy localhost default that shadowed TG_MONGO_URL for web
set_kv TG_WEB_MONGO_URL ""
if [ -n "${TG_FILES_S3_ENDPOINT:-}" ]; then
  set_kv TG_FILES_S3_ENDPOINT "$TG_FILES_S3_ENDPOINT"
  set_kv TG_FILES_S3_BUCKET "${TG_FILES_S3_BUCKET:-treegent}"
  set_kv TG_FILES_S3_ACCESS_KEY "$TG_FILES_S3_ACCESS_KEY"
  set_kv TG_FILES_S3_SECRET_KEY "$TG_FILES_S3_SECRET_KEY"
else
  # S3 disabled in config -> make sure no stale keys survive in .env
  for k in TG_FILES_S3_ENDPOINT TG_FILES_S3_BUCKET TG_FILES_S3_ACCESS_KEY TG_FILES_S3_SECRET_KEY; do
    sed -i "s|^$k=.*|$k=|" "$TOKEN_FILE"
  done
fi
[ -n "${TG_FILES_S3_BUCKET:-}" ] && set_kv TG_FILES_S3_BUCKET "$TG_FILES_S3_BUCKET"
[ -n "${TG_FILES_S3_ACCESS_KEY:-}" ] && set_kv TG_FILES_S3_ACCESS_KEY "$TG_FILES_S3_ACCESS_KEY"
[ -n "${TG_FILES_S3_SECRET_KEY:-}" ] && set_kv TG_FILES_S3_SECRET_KEY "$TG_FILES_S3_SECRET_KEY"
[ -n "${TG_RESEND_API_KEY:-}" ] && set_kv RESEND_API_KEY "$TG_RESEND_API_KEY"
[ -n "${TG_MAIL_DOMAIN:-}" ] && set_kv TG_MAIL_DOMAIN "$TG_MAIL_DOMAIN"
if [ "$PROXY" = "caddy" ]; then
  set_kv TG_PUBLIC_URL "https://$TG_DOMAIN"
fi

say "running installer (uv, services, web, systemd)"
bash deploy/install.sh

say "MongoDB (${TG_MONGO_MODE})"
if [ "$TG_MONGO_MODE" = "bundled" ]; then
  if ! command -v mongod >/dev/null; then
    # install official mongodb 8 on ubuntu/debian
    . /etc/os-release
    curl -fsSL "https://www.mongodb.org/static/pgp/server-8.0.asc" | gpg --dearmor -o /usr/share/keyrings/mongodb.gpg 2>/dev/null || true
    echo "deb [signed-by=/usr/share/keyrings/mongodb.gpg] https://repo.mongodb.org/apt/ubuntu ${UBUNTU_CODENAME}/mongodb-org/8.0 multiverse" \
      > /etc/apt/sources.list.d/mongodb-org-8.0.list 2>/dev/null \
      || echo "deb [signed-by=/usr/share/keyrings/mongodb.gpg] https://repo.mongodb.org/apt/debian ${VERSION_CODENAME}/mongodb-org/8.0 main" \
         > /etc/apt/sources.list.d/mongodb-org-8.0.list
    apt-get update -qq && apt-get install -y -qq mongodb-org >/dev/null
  fi
  # single-node replica set (oplog for Meteor reactivity)
  install -d -m 755 /var/lib/mongodb
  [ -f /etc/mongod.conf ] || cat > /etc/mongod.conf <<EOF
storage:
  dbPath: /var/lib/mongodb
net:
  bindIp: 127.0.0.1
  port: 27017
replication:
  replSetName: rs0
EOF
  systemctl enable --now mongod >/dev/null 2>&1 || true
  sleep 2
  mongosh --quiet --eval 'try { rs.status().ok } catch (e) { rs.initiate({_id: "rs0", members: [{_id: 0, host: "127.0.0.1:27017"}]}).ok }' >/dev/null 2>&1 || \
    echo "NOTE: run 'mongosh --eval \"rs.initiate()\"' once Mongo is up"
fi

say "TLS"
if [ "$PROXY" = "caddy" ]; then
  apt-get install -y -qq caddy >/dev/null 2>&1 || true
  sed "s|treegent.example.com|$TG_DOMAIN|" deploy/Caddyfile.example > /etc/caddy/Caddyfile
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
fi

say "starting TreeGent"
systemctl enable treegent >/dev/null 2>&1 || true
# restart (not just enable --now): re-runs MUST activate freshly pulled code;
# enable --now alone is a no-op on an already-running unit
systemctl restart treegent
sleep 5

say "status"
systemctl --no-pager -l status treegent | head -12 || true

say "opening firewall"
if command -v ufw >/dev/null; then
  ufw allow 3000/tcp >/dev/null 2>&1 || true   # plain-http fallback path
  [ "$PROXY" = "caddy" ] && ufw allow 80,443/tcp >/dev/null 2>&1 || true
fi

IP="$(curl -fsS -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
echo
printf '\033[1;32m'
cat <<DONE

  TreeGent is starting up.

  Web UI:   ${SCHEME}://${TG_DOMAIN:-$IP:3000}
  First run: open the URL and create the first admin account.
  Config:   /opt/TreeGent/.env  (edit + 'systemctl restart treegent')
  Logs:     journalctl -u treegent -f

  Secrets master key was generated at /etc/treegent/env
  -> BACK THIS UP or secret values are unrecoverable.

DONE
printf '\033[0m'
