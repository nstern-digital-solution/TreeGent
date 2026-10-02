# RULINGS — TreeGent

Every decision has exactly one row here. The operator's word updates a row; nothing
else does. Status legend:

- **FIRM** — ruled by the operator (or verbatim in SPEC.md). Contract.
- **PROPOSED** — working default. Overridable anytime, in effect until then.
- **OPEN** — awaiting The operator's call. Listed in the next clarification round.

| # | Decision | Status | Value |
|---|----------|--------|-------|
| R1 | Project name | FIRM | **TreeGent** — ruled 2026-10-01 by the operator creating github.com/nstern-digital-solution/TreeGent |
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
| R24 | Agent email inboxes | FIRM (spec addendum) | every registered agent gets an inbox on the company mail domain; inbound mail delivered via the standard injection rule; outbound mail ALWAYS requires approval |
| R25 | Approval system | FIRM (spec addendum) | gated actions need superior approval before execution; the approver may be human or agent; gated-action types configurable |
| R26 | v1 gated-action set | PROPOSED | only `mail.send` gated at launch (spec-mandated); further action types (e.g. payments, external posts, secret shares) added by ruling via admin UI |
| R27 | M1 demo host | FIRM | this dev machine — compose stack locally, browser demo; move to real central host later |
| R28 | Web login (M1) | FIRM | username + password, admin-provisioned users, Meteor accounts-password; SSO layer later |
| R29 | Web frontend framework | FIRM | **React** (Meteor 3 default pairing, react-meteor-data/useTracker). Blaze attempt reverted same day — the operator: React is the Meteor default now |
| R30 | License | FIRM | **AGPL-3.0** — network copyleft; competitors can't run closed forks of the public code; dual-licensing stays open as an option |
| R31 | Model selection | FIRM | class-based dynamic routing: admin defines task classes (cheap/standard/reasoning/vision/…) → ordered provider preferences; live catalog resolves class → concrete healthy model at dispatch. NO per-agent model assignment (providers/models churn — unmanageable), NO agent free choice (agents can't self-assess task difficulty or frontier-model quality) |
| R32 | Queue priority + serialization | FIRM | 2-factor priority: org-hierarchy weight × inverse usage over trailing 48h (senior AND frugal agents dispatch first). Per-agent serialization: max ONE in-flight generation per agent; later triggers (incl. heartbeats) queue behind or merge into the next turn — never cancel, never parallelize an agent's active request (requests before/after a heartbeat can't interleave into one sequence) |
| R33 | Local inference | FIRM | out of scope as a special case — any OpenAI-compatible endpoint (incl. self-hosted vLLM) is just a provider config entry with a base_url; the job queue already handles slow endpoints gracefully |
| R34 | Proxy history logging | FIRM | full content, not just metadata: prompts, completions, tool calls are stored and feed the human-facing history viewer (spec: "chat and action history") |
