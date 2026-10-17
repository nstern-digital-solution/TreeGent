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
INTERNAL="${TG_FORGE_URL_INTERNAL:-http://127.0.0.1:3001}"  # compose binds here; Caddy fronts public

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
# Admin password flow (R82, 2026-10-17): the official token-mint route
# (POST /users/{u}/tokens) requires BASIC auth — an admin password is
# the only credential that can mint agent tokens via the API (spec +
# routers/api/v1/api.go, Forgejo v12.0.4). Therefore the password must
# be KNOWN, not random: if FORGE_ADMIN_PW is set we create with it (or
# reset an existing account to it); unset means random-on-create and
# the mint falls back to the container CLI (TG_FORGE_COMPOSE_DIR).
if [ -n "${FORGE_ADMIN_PW:-}" ] && \
        docker compose exec -u git -T server forgejo admin user list 2>/dev/null \
        | awk '{print $2}' | grep -qx 'treegent-admin'; then
    echo "admin exists — resetting password to FORGE_ADMIN_PW (mint route)"
    docker compose exec -u git -T server forgejo admin user change-password \
        --username treegent-admin --password "$FORGE_ADMIN_PW" \
        --must-change-password=false
elif [ -n "${FORGE_ADMIN_PW:-}" ]; then
    echo "creating admin account 'treegent-admin' (known password)"
    docker compose exec -u git -T server forgejo admin user create \
        --admin --username treegent-admin \
        --email "admin@${DOMAIN#git.}" \
        --password "$FORGE_ADMIN_PW" --must-change-password=false
else
    PW="$(head -c 32 /dev/urandom | base64 | tr -d '/+' | head -c 28)"
    if docker compose exec -u git -T server forgejo admin user list 2>/dev/null \
            | awk '{print $2}' | grep -qx 'treegent-admin'; then
        echo "admin account already exists"
    else
        echo "creating admin account 'treegent-admin'"
        docker compose exec -u git -T server forgejo admin user create \
            --admin --username treegent-admin \
            --email "admin@${DOMAIN#git.}" \
            --password "$PW" --must-change-password=false
        echo "admin password (store in the secrets service): $PW"
    fi
fi

# 3. connector token via the container CLI: needs NO admin password and NO
#    web UI. Hands-off by design: `treegent update` arms the connector
#    without any manual step.
if [ -n "${TG_CHAT_FORGE_TOKEN:-}" ]; then
    echo "connector token present in env — skipping token mint"
else
    echo "generating connector token (container CLI)"
    TNAME="treegent-mgmt-$(date +%Y%m%d%H%M%S)"
    OUT="$(docker compose exec -u git -T server forgejo admin user \
        generate-access-token -u treegent-admin -t "$TNAME" \
        --scopes write:admin,write:user 2>&1)" \
        || { echo "token generation failed: $OUT" >&2; exit 1; }
    TOKEN="$(printf '%s' "$OUT" | grep -oE '[0-9a-f]{40}' | tail -1)"
    if [ -z "$TOKEN" ]; then
        echo "no token found in CLI output: $OUT" >&2
        exit 1
    fi
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
# R82: mint path + agent repo access (see deploy/env.example). The admin
# password enables the official basic-auth mint route; COMPOSE_DIR the
# CLI fallback; AGENT_REPOS the collaborator grants.
for VAR in TG_FORGE_ADMIN_PW TG_FORGE_COMPOSE_DIR TG_FORGE_AGENT_REPOS; do
    VAL="${!VAR:-}"
    [ -n "$VAL" ] || continue
    if grep -q "^$VAR=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^$VAR=.*|$VAR=$VAL|" "$ENV_FILE"
    else
        echo "$VAR=$VAL" >> "$ENV_FILE"
    fi
done

echo "OK — forge up, connector armed."
