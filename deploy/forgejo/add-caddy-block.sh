#!/usr/bin/env bash
# Append the Forgejo site block to the live Caddyfile (idempotent).
# Finds the Caddyfile via the systemd unit, falls back to /etc/caddy/Caddyfile.
# usage: add-caddy-block.sh [domain]   (default: TG_FORGE_DOMAIN from env file)
set -euo pipefail

ENV_FILE="${TG_ENV_FILE:-/etc/treegent/env}"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
DOMAIN="${1:-${TG_FORGE_DOMAIN:?pass the domain or set TG_FORGE_DOMAIN}}"

CF="$(systemctl cat caddy 2>/dev/null \
      | grep -oP '(?<=(--config |-c ))\S+' | head -1 || true)"
if [ -z "$CF" ] && [ -f /etc/caddy/Caddyfile ]; then CF=/etc/caddy/Caddyfile; fi
if [ -z "$CF" ]; then
    echo "no Caddyfile found — run: systemctl cat caddy" >&2
    exit 1
fi
echo "Caddyfile: $CF"

if grep -q "$DOMAIN" "$CF"; then
    echo "site block for $DOMAIN already present"
else
    cp -a "$CF" "$CF.bak"
    printf '\n# Forgejo (R80)\n%s {\n\tencode zstd gzip\n\treverse_proxy 127.0.0.1:3001\n}\n' \
        "$DOMAIN" >> "$CF"
    echo "site block appended (backup: $CF.bak)"
fi

caddy validate --config "$CF" >/dev/null \
    && echo "caddy config valid"
systemctl reload caddy 2>/dev/null || caddy reload --config "$CF"
echo "done — https://$DOMAIN should answer within a minute (TLS issuance)"
