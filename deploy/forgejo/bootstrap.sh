#!/usr/bin/env bash
# First-run Forgejo bootstrap: health-wait, admin account, admin API token,
# .env wiring. Idempotent — safe to re-run. Reads TG_FORGE_DOMAIN.
set -euo pipefail
cd "$(dirname "$0")"

DOMAIN="${TG_FORGE_DOMAIN:?set TG_FORGE_DOMAIN (e.g. git.nstern.de)}"
INTERNAL="http://127.0.0.1:3001"          # compose binds here; Caddy fronts public
ENV_FILE="${TG_ENV_FILE:-/opt/TreeGent/.env}"

# 1. wait for the forge to come up (first boot runs db migrations)
echo "waiting for forgejo at $INTERNAL ..."
for i in $(seq 1 60); do
    if curl -sf "$INTERNAL/api/healthz" >/dev/null 2>&1; then break; fi
    sleep 2
done
curl -sf "$INTERNAL/api/healthz" >/dev/null || { echo "forgejo did not become healthy" >&2; exit 1; }

# 2. admin account (random password; the API token is the working credential)
PW="${FORGE_ADMIN_PW:-}"
if [ -z "$PW" ]; then
    PW="$(head -c 32 /dev/urandom | base64 | tr -d '/+' | head -c 28)"
fi
if docker compose exec -T server forgejo admin user list 2>/dev/null | grep -q '^treegent-admin\b'; then
    echo "admin account already exists"
else
    echo "creating admin account 'treegent-admin'"
    docker compose exec -T server forgejo admin user create \
        --admin --username treegent-admin \
        --email "admin@${DOMAIN#git.}" \
        --password "$PW" --must-change-password=false \
        --random-password=false
    echo "admin password (store in the secrets service): $PW"
fi

# 3. mint the admin API token via basic auth (works before any token exists)
echo "minting admin API token"
TOKEN_JSON="$(curl -sf -X POST "$INTERNAL/api/v1/users/treegent-admin/tokens" \
    -u "treegent-admin:${FORGE_ADMIN_PW:-$PW}" \
    -H "Content-Type: application/json" \
    -d '{"name":"treegent-mgmt","scopes":["write:admin","write:user"]}')" \
    || { echo "token mint failed — set FORGE_ADMIN_PW and re-run" >&2; exit 1; }
TOKEN="$(printf '%s' "$TOKEN_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha1"])')"

# 4. wire the chat service env
if grep -q '^TG_CHAT_FORGE_URL=' "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^TG_CHAT_FORGE_URL=.*|TG_CHAT_FORGE_URL=https://$DOMAIN|" "$ENV_FILE"
else
    echo "TG_CHAT_FORGE_URL=https://$DOMAIN" >> "$ENV_FILE"
fi
if grep -q '^TG_CHAT_FORGE_TOKEN=' "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^TG_CHAT_FORGE_TOKEN=.*|TG_CHAT_FORGE_TOKEN=$TOKEN|" "$ENV_FILE"
else
    echo "TG_CHAT_FORGE_TOKEN=$TOKEN" >> "$ENV_FILE"
fi

echo "OK — forge up, connector armed. Restart the chat service (treegent update)."
