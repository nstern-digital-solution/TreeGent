# RULINGS — constellation

Every decision has exactly one row here. the operator's word updates a row; nothing
else does. Status legend:

- **FIRM** — ruled by the operator (or verbatim in SPEC.md). Contract.
- **PROPOSED** — working default. Overridable anytime, in effect until then.
- **OPEN** — awaiting the operator's call. Listed in the next clarification round.

| # | Decision | Status | Value |
|---|----------|--------|-------|
| R1 | Project name | PROPOSED | `constellation` (placeholder, rename = 1 command) |
| R2 | Agent runtime language | FIRM | Python |
| R3 | Wake policy for inbound messages | FIRM | wake immediately on DMs and @mentions; channel chatter waits for heartbeat |
| R4 | First milestone shape | FIRM | platform first — chat, proxy, secrets, files solid; agents last |
| R5 | Deployment topology | FIRM | 1 central host for shared services + separate agent hosts |
| R6 | Org/hierarchy source of truth | FIRM | database + admin UI in the Meteor app |
| R7 | Chat client protocol | PROPOSED | WebSocket JSON (typed events), REST for tool-side writes |
| R8 | File storage backend | PROPOSED | S3-compatible store (MinIO) + WebDAV gateway for Linux mounts |
| R9 | Secret envelope format | PROPOSED | age/X25519 envelopes; ACLs enforced server-side |
| R10 | Model abstraction in agent runtime | PROPOSED | thin OpenAI-compatible client pointed at `services/proxy` |
| R11 | Agent process isolation | PROPOSED | one container per agent; browser engine as sidecar container |
| R12 | Humans in chat backend | PROPOSED | full actors (same API), web login, no tools |
| R13 | Chatlog retention | PROPOSED | append-only, kept forever; UI paginates; compaction is additive summary, never deletion of the log |
| R14 | Injection format | FIRM (spec) | at next turn's start: `You have a new message from X received at <ts>` |
| R15 | Subagents | FIRM (spec) | none — one continuous session per agent, delegation = messaging a colleague |
| R16 | Superior secret access | FIRM (spec) | superiors can always access subordinates' secrets |
| R17 | Inference path | FIRM (spec) | all agent LLM traffic via central OpenAI-compatible proxy with per-agent usage stats |
| R18 | Agent execution model | PROPOSED | agents run as systemd services on their host (not containerized) with native Docker access for dev work; fallback: containerized agents with Docker socket mounted (sibling containers) |
| R19 | Fleet installs/updates | FIRM (requirement) | no manual per-agent installs/updates — central management. PROPOSED mechanism: pull-based `agentd` supervisor per host + desired-state service, rollout from admin UI |
| R20 | Per-agent resources | PROPOSED | systemd cgroup limits per agent (CPUQuota, MemoryMax), per-agent home dir with disk quota; dev containers are ephemeral and sized by host capacity |
| R21 | Shared-services stack | PROPOSED | Python/FastAPI services + MongoDB + Redis; MinIO for file blobs; Meteor only for web. Mongo over PG: Meteor reactivity is Mongo-native; org hierarchy via stored ancestors path; one DB to operate. Swap per-service later is contained (no cross-service joins) |
| R22 | Agent ↔ service auth | PROPOSED | host enrollment token at bootstrap → per-agent service tokens; agents identified at every service |
| R23 | Action history | PROPOSED | runtime reports every tool call to the control service's audit ledger; web UI renders chat + action timeline per agent |
