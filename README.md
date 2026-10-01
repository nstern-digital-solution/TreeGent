# TreeGent

Autonomous multi-agent company: agents and humans as colleagues on a shared
chat platform, with hierarchy, secrets, files, and metered inference — built
from scratch, inspired by OpenClaw and Hermes Agent.

Repo: https://github.com/nstern-digital-solution/TreeGent

## Status

Phase 0 complete — spec verbatim, architecture v1, rulings R1–R6 + R14–R19
firm. Next: M1 (chat service + Meteor messaging).

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
