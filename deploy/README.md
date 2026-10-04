# TreeGent deployment bundle (first production deployment)

One central host runs the shared services + web UI + agent runtime.
Agent hosts (optional, later) run agentd with exec enabled (R48: never
on the central host).

## Layout

```
deploy/
  README.md            this file
  install.sh           one-shot installer for the central host
  treegent.service     systemd unit template (central host, all services)
  agentd.service       systemd unit template (agent hosts, exec ON)
  env.example          every env var, documented, no secrets
  Caddyfile.example    reverse proxy + automatic TLS (optional)
```

## Prerequisites (user-provided, per rulings R21/R41)

- Linux host (Ubuntu 24.04+ or Debian 12+), 2 GB RAM minimum
- **MongoDB** — user-provided server; the installer does NOT install it
  (AIO bundle territory). Point `TG_MONGO_URL` at yours. For a quick
  self-host on the same box, any mongod with a single-node replica set
  works (oplog needed for Meteor reactivity).
- **S3-compatible storage** — user-provided (AWS / Hetzner / R2 / your
  MinIO). Set `TG_FILES_S3_*`.
- Provider API key for inference (e.g. an OpenRouter key) — set
  `TG_PROXY_KEY_<PROVIDER>`.

## Quickstart (the way anyone deploys)

One command on a fresh Linux box (root/sudo), answer the questions:

```bash
curl -fsSL https://raw.githubusercontent.com/nstern-digital-solution/TreeGent/main/deploy/quickstart.sh | bash
```

It asks for: domain (optional — TLS via Caddy if given), Mongo
(bundled single-node by default, or your external URL), service token
(generates one if you just press enter), inference provider key
(optional now, add later in the web UI), S3 details (optional), Resend
mail key (optional — dev sink until set). Then it installs everything
and starts it. Open the printed URL, create the first admin account,
build your org in the web UI.

## Manual install (alternative)

```bash
git clone https://github.com/nstern-digital-solution/TreeGent.git
cd TreeGent
cp deploy/env.example .env        # edit: fill in real values
sudo bash deploy/install.sh       # installs uv, builds, installs units
sudo systemctl enable --now treegent
```

`install.sh` is idempotent — re-run after `git pull` to update.

## What runs on the central host

| unit | what | port (default) |
|---|---|---|
| treegent.service | mongod-not-included; chat, proxy, mail, secrets, files, runtime, web | 3000 (web), internal 8000-8010 on loopback |

All internal services bind `127.0.0.1` only. The web UI is the only thing
meant to be exposed; put Caddy/nginx in front for TLS (example included).

The web UI needs the central-tier service token; it lives in
`/etc/treegent/env` (root-readable, loaded by the units).

## Agent hosts (later, per R48)

```bash
git clone ... && cd TreeGent
cp deploy/env.example .env        # TG_RUNTIME_EXEC_ENABLED=true, agent key
sudo bash deploy/install.sh --agent-host
sudo systemctl enable --now agentd
```

Agent hosts hold ONLY the agent key + service URLs — never the
central-tier service token.

## Bootstrap (first run)

1. Open the web UI, create the root human account.
2. Create org: actors, hierarchy, agent actors (this provisions agent
   keys — copy each key to its agent host's env).
3. In Models tab: add provider row + set ranks/designations.
4. Mail: set `RESEND_API_KEY` when real outbound email is wanted (dev
   sink until then).

## Updating

```bash
cd /opt/TreeGent && sudo systemctl stop treegent
sudo -u treegent git pull
sudo bash deploy/install.sh && sudo systemctl start treegent
```

## Backups

- Mongo: `mongodump` on a timer (not included — operator choice).
- S3: your provider's tooling.
- Secrets master key file `/etc/treegent/secrets.key`: **back it up or
  lose all secret values** — it is never stored anywhere else.
