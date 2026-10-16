#!/usr/bin/env bash
# Forgejo lifecycle, called by treegent update (stage 4b) and install.
# Single source of truth for config: /etc/treegent/env (TG_FORGE_DOMAIN,
# TG_CHAT_FORGE_URL, TG_CHAT_FORGE_TOKEN) — the same file every other
# TreeGent setting lives in (Felix, PR #76 review: no extra commands,
# no separate config). Idempotent; safe on hosts without docker.
set -uo pipefail

ENV_FILE="${TG_ENV_FILE:-/etc/treegent/env}"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
COMPOSE_DIR="$(cd "$(dirname "$0")/.." && pwd)/forgejo"   # deploy/forgejo

# disabled explicitly? TG_FORGE_DOMAIN empty/absent = forge not wanted here
if [ -z "${TG_FORGE_DOMAIN:-}" ]; then
  echo "forgejo: TG_FORGE_DOMAIN not set in $ENV_FILE — skipping (set it to enable)"
  exit 0
fi

command -v docker >/dev/null 2>&1 || {
  echo "forgejo: docker not found — install docker, then re-run: treegent update" >&2
  exit 1
}
docker compose version >/dev/null 2>&1 || {
  echo "forgejo: docker compose plugin missing — install docker-compose-plugin" >&2
  exit 1
}

cd "$COMPOSE_DIR"
mkdir -p forgejo-data
chown -R 1000:1000 forgejo-data   # container runs as UID 1000; root-owned dir breaks the install wizard's db write

# compose reads TG_FORGE_DOMAIN from the environment we just sourced
export TG_FORGE_DOMAIN

echo "forgejo: bringing up container (127.0.0.1:3001)"
docker compose up -d

# wait for health (first boot runs db migrations)
for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:3001/api/healthz >/dev/null 2>&1; then
    echo "forgejo: healthy"
    break
  fi
  sleep 2
done
curl -sf http://127.0.0.1:3001/api/healthz >/dev/null 2>&1 || {
  echo "forgejo: not healthy after 120s — check: docker logs treegent-forgejo" >&2
  exit 1
}

# bootstrap admin + token + env wiring ONLY when the connector isn't armed yet
if [ -z "${TG_CHAT_FORGE_TOKEN:-}" ]; then
  echo "forgejo: running bootstrap (admin + token)"
  bash "$COMPOSE_DIR/bootstrap.sh" || exit 1
else
  echo "forgejo: connector already armed"
fi
echo "forgejo: done — https://$TG_FORGE_DOMAIN (Caddy block: deploy/Caddyfile.example)"
