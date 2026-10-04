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

# ---------- questions ----------
ask TG_DOMAIN "" "(no domain — plain http on :3000)"
if [ -n "${TG_DOMAIN:-}" ]; then
  SCHEME="https"; PROXY="caddy"
else
  SCHEME="http"; PROXY="none"
fi
# Mongo: Enter = bundled local single-node replica set, or paste ANY
# connection string (Atlas SRV, self-hosted, ...) to bring your own.
ask_secret_or_default TG_MONGO_URL "Mongo: Enter = bundled local, or paste connection string (mongodb:// or mongodb+srv://)" ""
if [ -z "${TG_MONGO_URL:-}" ]; then
  TG_MONGO_MODE=bundled
else
  TG_MONGO_MODE=external
fi

# internal token: generated silently, never asked (implementation detail)
TG_SERVICE_TOKEN="$(head -c32 /dev/urandom | base64 | tr -d '=+/' | head -c 40)"
# NOTE: inference providers (multiple, any keys/URLs) are added at RUNTIME
# in the web UI Models tab — stored encrypted, nothing needed at deploy.
ask TG_EXTRAS "Configure optional extras now — S3 files / Resend mail? (Enter = skip both)" "skip"
if [ "${TG_EXTRAS:-skip}" = "skip" ]; then
  TG_FILES_S3_ENDPOINT=""
else
ask TG_FILES_S3_ENDPOINT "S3 endpoint" "(enter = skip files service)"
[ "$TG_FILES_S3_ENDPOINT" = "" ] || ask TG_FILES_S3_BUCKET "S3 bucket" "treegent"
if [ -n "${TG_FILES_S3_BUCKET:-}" ] && [ "$TG_FILES_S3_BUCKET" != "treegent" ] || [ -n "${TG_FILES_S3_ENDPOINT:-}" ] && [ "${TG_FILES_S3_ENDPOINT:-}" != "" ]; then
  ask_secret TG_FILES_S3_ACCESS_KEY "S3 access key"
  ask_secret TG_FILES_S3_SECRET_KEY "S3 secret key"
fi
ask_secret TG_RESEND_API_KEY "RESEND_API_KEY for outbound mail (enter = dev sink)"
fi

# ---------- install ----------
say "installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq curl git ca-certificates >/dev/null

say "cloning TreeGent"
REPO_DIR="/opt/TreeGent"
if [ -d "$REPO_DIR" ]; then
  say "repo exists — pulling latest"
  git -C "$REPO_DIR" pull --ff-only || true
else
  git clone -q https://github.com/nstern-digital-solution/TreeGent.git "$REPO_DIR"
fi
cd "$REPO_DIR"

say "writing configuration"
TOKEN_FILE="$REPO_DIR/.env"
if [ ! -f "$TOKEN_FILE" ]; then
  cp deploy/env.example "$TOKEN_FILE"
fi
set_kv() { sed -i "s|^$1=.*|$1=$2|" "$TOKEN_FILE"; }
set_kv TG_SERVICE_TOKEN "$TG_SERVICE_TOKEN"
[ -n "${TG_MONGO_URL:-}" ] && set_kv TG_MONGO_URL "$TG_MONGO_URL"
[ -n "${TG_FILES_S3_ENDPOINT:-}" ] && [ "$TG_FILES_S3_ENDPOINT" != "" ] && set_kv TG_FILES_S3_ENDPOINT "$TG_FILES_S3_ENDPOINT"
[ -n "${TG_FILES_S3_BUCKET:-}" ] && set_kv TG_FILES_S3_BUCKET "$TG_FILES_S3_BUCKET"
[ -n "${TG_FILES_S3_ACCESS_KEY:-}" ] && set_kv TG_FILES_S3_ACCESS_KEY "$TG_FILES_S3_ACCESS_KEY"
[ -n "${TG_FILES_S3_SECRET_KEY:-}" ] && set_kv TG_FILES_S3_SECRET_KEY "$TG_FILES_S3_SECRET_KEY"
[ -n "${TG_RESEND_API_KEY:-}" ] && set_kv RESEND_API_KEY "$TG_RESEND_API_KEY"
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
systemctl enable --now treegent
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
