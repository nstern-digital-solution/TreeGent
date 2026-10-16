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

## Install (central server)

Everything lives in `/etc/treegent/env` like every other TreeGent setting
(`TG_FORGE_DOMAIN` is the only value you set by hand):

```sh
echo 'TG_FORGE_DOMAIN=git.nstern.de' >> /etc/treegent/env
treegent update
```

Stage 4b of update runs `ensure-forgejo.sh`: container up (data dir
chown'd to the container user), health-wait, admin + token bootstrap
(first time only), env wiring — all idempotent.

**Separation of identities (ruling 2026-10-16):** provisioning never runs
as the personal admin. `bootstrap.sh` creates a dedicated service account
`treegent-connector` (admin — Forgejo has no narrower create-users scope —
but never used for UI logins, independently revocable) and mints the
connector token from THAT account. Rotate by deleting the account and
re-running bootstrap.

**First run only — the install wizard:** Forgejo shows a one-time setup
page at `https://<domain>`. Accept the defaults (sqlite3), set Server
domain and Base URL to the real domain, keep **Disable self-registration**
checked, and enter the admin credentials (`treegent-admin` + a password
of your choice). The wizard must complete before the API works.

Caddy: `deploy/forgejo/add-caddy-block.sh` finds the live Caddyfile,
appends the `git.<domain>` site (idempotent), validates and reloads.

GitHub history import (optional, one-shot, read-only on the GitHub side):
`./migrate-from-github.sh nstern-digital-solution/TreeGent`.

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
