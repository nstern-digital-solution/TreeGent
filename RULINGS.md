# RULINGS — constellation

Every decision has exactly one row here. the operator's word updates a row; nothing
else does. Status legend:

- **FIRM** — ruled by the operator (or verbatim in SPEC.md). Contract.
- **PROPOSED** — working default. Overridable anytime, in effect until then.
- **OPEN** — awaiting the operator's call. Listed in the next clarification round.

| # | Decision | Status | Value |
|---|----------|--------|-------|
| R1 | Project name | PROPOSED | `constellation` (placeholder, rename = 1 command) |
| R2 | Agent runtime language | OPEN | asked: TS / Python / mixed |
| R3 | Wake policy for inbound messages | OPEN | asked: wake on DM / heartbeat-only / hybrid |
| R4 | First milestone shape | OPEN | asked: walking skeleton / vertical depth / platform first |
| R5 | Deployment topology | OPEN | asked: single host compose / small fleet / k8s |
| R6 | Org/hierarchy source of truth | OPEN | asked: DB+admin UI / org file in repo / hybrid |
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
