#!/usr/bin/env bash
# First-run Forgejo bootstrap: health-wait, admin account, admin API token,
# env wiring. Idempotent — safe to re-run. All config lives in
# /etc/treegent/env like every other TreeGent setting (PR #76 review):
# set TG_FORGE_DOMAIN there; bootstrap sources the file itself so it works
# both standalone AND via ensure-forgejo.sh.
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="${TG_ENV_FILE:-/etc/treegent/env}"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

DOMAIN="${TG_FORGE_DOMAIN:?set TG_FORGE_DOMAIN in $ENV_FILE (e.g. git.nstern.de)}"
INTERNAL="http://127.0.0.1:3001"          # compose binds here; Caddy fronts public

# 1. wait for the forge to come up (first boot runs db migrations)
echo "waiting for forgejo at $INTERNAL ..."
for i in $(seq 1 60); do
    if curl -sf "$INTERNAL/api/healthz" >/dev/null 2>&1; then break; fi
    sleep 2
done
curl -sf "$INTERNAL/api/healthz" >/dev/null 2>&1 \
    || { echo "forgejo did not become healthy" >&2; exit 1; }

# 2. admin account (random password unless FORGE_ADMIN_PW is set; the API
#    token is the working credential). The forgejo binary refuses to run as
#    root, so exec as the container's 'git' user.
PW="${FORGE_ADMIN_PW:-$(head -c 32 /dev/urandom | base64 | tr -d '/+' | head -c 28)}"
if docker compose exec -u git -T server forgejo admin user list 2>/dev/null \
        | grep -q '^treegent-admin'; then
    echo "admin account already exists"
else
    echo "creating admin account 'treegent-admin'"
    docker compose exec -u git -T server forgejo admin user create \
        --admin --username treegent-admin \
        --email "admin@${DOMAIN#git.}" \
        --password "$PW" --must-change-password=false
    echo "admin password (store in the secrets service): $PW"
fi

# 3. mint the admin API token (skip when the connector is already armed;
#    re-minting would orphan the previous token)
if [ -n "${TG_CHAT_FORGE_TOKEN:-}" ]; then
    echo "connector token present in env — skipping token mint"
else
    echo "minting admin API token"
    TOKEN_JSON="$(curl -sf -X POST \
        "$INTERNAL/api/v1/users/treegent-admin/tokens" \
        -u "treegent-admin:$PW" -H "Content-Type: application/json" \
        -d '{"name":"treegent-mgmt","scopes":["write:admin","write:user"]}')" \
        || { echo "token mint failed — set FORGE_ADMIN_PW to the admin password and re-run" >&2; exit 1; }
    TOKEN="$(printf '%s' "$TOKEN_JSON" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha1"])')"
fi

# 4. wire the chat service env (single env file, PR #76 review)
if [ -n "${TOKEN:-}" ]; then
    if grep -q '^TG_CHAT_FORGE_TOKEN=' "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^TG_CHAT_FORGE_TOKEN=.*|TG_CHAT_FORGE_TOKEN=$TOKEN|" "$ENV_FILE"
    else
        echo "TG_CHAT_FORGE_TOKEN=$TOKEN" >> "$ENV_FILE"
    fi
fi
if grep -q '^TG_CHAT_FORGE_URL=' "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^TG_CHAT_FORGE_URL=.*|TG_CHAT_FORGE_URL=https://$DOMAIN|" "$ENV_FILE"
else
    echo "TG_CHAT_FORGE_URL=https://$DOMAIN" >> "$ENV_FILE"
fi

echo "OK — forge up, connector armed."
