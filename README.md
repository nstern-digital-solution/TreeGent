# constellation

Autonomous multi-agent company: agents and humans as colleagues on a shared
chat platform, with hierarchy, secrets, files, and metered inference — built
from scratch, inspired by OpenClaw and Hermes Agent.

Working name (renameable in one ruling).

## Status

Phase 0 — spec captured, first rulings pending.

## Documents

- **SPEC.md** — requirements (verbatim) + interpretation
- **RULINGS.md** — decision log (FIRM / PROPOSED / OPEN)
- **ARCHITECTURE.md** — service map + lifecycles
- **LOGBOOK.md** — dated build journal

## Planned layout

```
services/chat      DMs, channels, actors, org, delivery
services/proxy     OpenAI-compatible router + usage stats
services/secrets   envelope store, sharing, hierarchy access
services/files     shared storage + ACLs, Linux-mountable
services/control   fleet brain: desired state, wakes, rollouts, audit
services/web       Meteor: chat client, admin, files, secrets, history
agent/             agent runtime + agentd supervisor
deploy/            central compose + agent-host install
```
