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
| R31b | Model selection (replaces criteria/price era) | FIRM | classes are designations: **agent** (agent-loop models) + **task** (short auxiliary tasks). System-side selection pipeline: designation filter → modality filter (image/video/file/audio in request excludes non-supporting models) → availability filter (rate-limited/unreachable models+providers cooldown-blocked) → **highest rank**. Rank + designations + exclusions are the only admin levers; NO pricing anywhere (no prices in catalog, no cost metering, no budgets) |
| R35 | Shared mailboxes | FIRM | group/team mailboxes (e.g. sales@) exist alongside personal agent mailboxes; members can read AND send from the shared address (send-as). Inbound to a shared mailbox reaches all member agents |
| R36 | Human mailboxes | FIRM | NO human mailboxes in TreeGent — mail is for agents (personal + shared). Humans interact via web UI: approvals queue, and viewing agent/shared mailboxes as superiors/admins |
| R37 | Approval timeouts | FIRM | none — an unresponded approval stays PENDING forever (no auto-escalate, no auto-reject). Agents get a tool to list pending approvals (requested by them + awaiting their action) |
| R38 | Mail transport | FIRM | Resend as the first real adapter pair: outbound `resend-send` (REST API), inbound `resend-inbound` (poll now, webhook-ready). Rationale: free tier (100/day ≈ 5-10 agents' outbound), both directions from one vendor/key, no custom servers or DNS infrastructure beyond domain verification — any deployment can set it up easily. Adapters stay data rows (like providers); local capture sink for dev until RESEND_API_KEY exists |
| R39 | Send approval modes | FIRM | mail.send takes mode: **foreground** or **background**. Foreground: the agent's loop suspends after requesting; the decision (approved OR rejected + reason) arrives as a wake event and resumes the loop. Background: loop continues immediately, request sits in pending approvals (agent checks via approval tool, R37). Foreground never blocks a generation slot while waiting (R32-compatible: turn ends, wake resumes) |
| R40 | Permission system | FIRM | ONE rule set for humans and agents (only login differs: web session vs agent key). Rules are DATA rows (`permissions` collection) AND code enforcement in every service — deny by default. Vocabulary = relationships only: own / member / superior / root (root = org-root human, infrastructure settings only; no agent ever gets root). Own-scope default: listings/searches return only own + shared resources (superior's search does NOT return subordinates' items). Superior reach-down = explicitly opening one NAMED subordinate resource (allowed by 'superior' in read rules), never bulk, never in listings. Rule edits restricted to root. NO audit trail (operator rejected: more data to review makes the problem worse) |
| R41 | File storage backend | FIRM | users provide their own S3-compatible host, exactly like they provide MongoDB — the files service is an S3 CLIENT; endpoint/bucket/credentials are deployment config (instance data), never bundled or hardcoded. MinIO/Mongo/SMTP bundling = future AIO deployment bundle, out of scope for initial release |
| R42 | Agent tools v1 | FIRM | core (chat.send, mail.send + check, approvals.list/decide, secrets.list/read/write) + code execution (subprocess in agent workspace, timeouts) + web fetch (page→text; full browser automation later) + web search (configurable backend, default off) + memory (search/write in own workspace). FILES NOT via API tools — agents use the OS-mounted share (workspace file tools, path-guarded) |
| R43 | Secrets in history | FIRM | full literal R34: proxy history stores complete transcripts INCLUDING secret values — history is permission-guarded (R40), no masking |
| R44 | Heartbeat | FIRM | default 60 minutes, per-agent configurable |
| R45 | Agent identity | FIRM | DONE 2026-10-03: ALL services (chat, mail, secrets, files, proxy) derive agent identity from X-Agent-Key server-side; claimed ids/params are ignored. Shared service token remains ONLY as the central tier (web server acting for logged-in humans) and never reaches agent hosts. Proven: spoofed caller_id and X-Actor-Id both ignored; forged/absent keys 401 |
| R46 | exec sandbox | FIRM | agent commands run as a SEPARATE linux user (cannot read/overwrite agentd/runtime code); workspace folder limits stay; foreground timeout 10 min; background execution NO timeout; deliberately NO containers — agents run their own dockers for dev work |
| R47 | agent history viewer | FIRM | web tab with per-agent turn + tool-call history, built NOW (not deferred) |
| R48 | Agent host boundary | FIRM | deployment hosts running agent code are reserved machines — never a shared services host; dev/testing of exec is the only exception and exec is DISABLED there (TG_RUNTIME_EXEC_ENABLED=false). Real agents run on their own hosts via agentd (R19) with exec enabled, own workspaces, own users |

## R49 — agent identity (2026-10-04)
Every agent gets a random human name + personality traits at creation
(operator: "every agent should get a random 'human' name and personality
traits assigned at creation. These can then also reside in his system
prompt and be used for his email address").
- identity assigned ONCE in chat actors.create (agents only), stored on
  the actor doc (persona.persona_name / traits / style)
- display_name = persona name (admins still see username)
- runtime system prompt leads with the persona line
- personal mailbox = firstname@domain (idempotent ensure at mail boot)
- humans unaffected

## R50 — soul & memory as markdown (2026-10-04)
Operator: "How do we handle memories and soul like as a markdown files?"
+ "Yes build it now and take inspiration from the hermes agent, openclaw,
  nanoclaw and nanobot system prompts".
- SOUL.md (voice, injected every turn, generated from R49 persona once,
  then agent/operator-owned; edits apply next turn — live re-read)
- MEMORY.md (durable notes, tail-injected ≤8k chars; memory.write appends
  timestamped lines; unbounded on disk)
- notes/*.md (working memory, never injected, searched on demand)
- budgets: SOUL 4k / MEMORY tail 8k / note 4k chars — injected copy only,
  disk never truncated; oversize shows a trim marker
- system message regenerated from current code + soul files on every
  session load (stale prompts from disk never trusted)
- memory tools now file-backed (Mongo agent_memory legacy, unused)

## R51 — client account creation forbidden (2026-10-04)
Operator caught live: "you have client side account creation enabled in
meteor which is a big security issue we cant have people create accounts!"
- Accounts.config({ forbidClientAccountCreation: true })
- verified: client-side createUser over raw DDP → 403 "Signups forbidden",
  no user written; server-side paths (bootstrap, admin-gated tg.createActor)
  unaffected

## R53 — agent hosts via web UI (2026-10-04)
Operator design: enter server IP/domain (+port +ssh user) in the web UI; UI
shows a one-liner to authorize the central box's PUBLIC key on the agent
box; central then provisions the machine remotely over SSH.
- Hosts tab (admin): add/list/provision; central ed25519 keypair generated
  on first use, private key NEVER leaves the central box / enters Mongo
- provision = tgexec user (R46), repo, venv, systemd treegent-agent.service,
  runtime-only env (no service token — agent keys only, R45)
- actor.host_id: host-scoped runtimes claim ONLY their agents; central
  runtime runs unassigned ones — no double-running
- Caddy: /chat /proxy /mail /secrets /files TLS routes for agent hosts
- verified: central runtime skipped a host-assigned agent; host-scoped
  runtime claimed exactly that one (Jonas Jovic) and nothing else

## R53b — per-host SSH keys (2026-10-04)
Operator: "You are not creating a ssh key per agent host like I asked you
are you?" — correct; v1 used ONE central keypair for all hosts.
- ONE ed25519 keypair PER host, generated at host creation
  (~/.treegent/agenthosts/<host_id>_ed25519, 0600, dir 0700)
- private halves NEVER in Mongo/UI — central box filesystem only
- Hosts tab shows PER-HOST one-liners (each authorizes only that key)
- remove host = stop+disable service on the box (best-effort),
  strip its authorized_keys line, DELETE the central keypair —
  revocation is per host; a leaked key exposes exactly one machine
- verified: 2 hosts → 2 distinct keys; DELETE removed only that keypair
- **R54** — Host fleet observability + maintenance: `check` (SSH probe: service state, git commit, uptime, load → status active|stopped|unreachable + last_seen), `update` (git pull to central's commit + service restart), auto-health loop every 60s, central vs host version comparison in UI.
