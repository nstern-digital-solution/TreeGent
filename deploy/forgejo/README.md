# Forgejo for TreeGent (R80)

Self-hosted git forge at a configurable public domain (`TG_FORGE_DOMAIN`,
e.g. `git.nstern.de`), Docker on the central server, fronted by Caddy.
Every actor (human or agent) created in TreeGent automatically gets a
Forgejo account via the admin API. Forge events (PRs, review requests)
deliberately do NOT become chat messages — accounts only (ruling 2026-10-15).

## Layout

- `docker-compose.yml` — Forgejo bound to `127.0.0.1:3001`, registration
  disabled, git over HTTPS (tokens); Caddy provides TLS + the public domain.
- `bootstrap.sh` — first-run: wait for health, create the admin account,
  mint the admin API token, write `TG_CHAT_FORGE_URL` + `TG_CHAT_FORGE_TOKEN`
  into the central `.env` (via `treegent env set`), idempotent.
- `migrate-from-github.sh` — import the GitHub repo (issues, PRs, labels,
  milestones, releases) into Forgejo. READ-ONLY against GitHub: migration
  pulls; nothing on GitHub is modified or deleted (ruling 2026-10-15).

## Install (central server, once)

```sh
cd /opt/TreeGent/deploy/forgejo
TG_FORGE_DOMAIN=git.nstern.de docker compose up -d
./bootstrap.sh                       # admin + token + .env wiring
./migrate-from-github.sh nstern-digital-solution/TreeGent
```

Caddy: `deploy/forgejo/add-caddy-block.sh` finds the live Caddyfile,
appends the `git.<domain>` site (idempotent), validates and reloads.

## Verify

- `curl -s https://$TG_FORGE_DOMAIN/api/v1/version -H "Authorization: token $TG_CHAT_FORGE_TOKEN"`
- Create a test actor in the dashboard → its forge account appears at
  `https://$TG_FORGE_DOMAIN/<username>` within seconds.
- `GET /internal/forge/sync` (service token) retries pending provisions.

## Config

| env | meaning | default |
|---|---|---|
| `TG_FORGE_DOMAIN` | public domain (compose + Caddy) | `git.example.com` |
| `TG_CHAT_FORGE_URL` | base URL the chat service calls; **empty = connector off** | "" |
| `TG_CHAT_FORGE_TOKEN` | admin-scoped API token | "" |

## Notes

- The connector is **total**: forge down never blocks or fails actor
  creation; the actor doc records `forge.status=pending` and
  `GET /internal/forge/sync` (or the next manual sweep) retries.
- Forgejo usernames mirror TreeGent usernames (lowercased, `[a-z0-9-]`).
- No PR/review notifications are generated (ruling 2026-10-15).
- Migration does not touch GitHub; when the cutover is ruled later,
  archive/lock the GitHub repo as a separate explicit step.
