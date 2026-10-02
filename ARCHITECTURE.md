# ARCHITECTURE — TreeGent

Design contract derived from SPEC.md + RULINGS.md. Where this document and
RULINGS conflict, RULINGS wins. Divergences require a ruling change first.

## 1. Shape

```
                        ┌───────────────────────────────┐
                        │        CENTRAL HOST           │
                        │                               │
  Humans ──Meteor WS──▶ │  services/web (Meteor)        │
                        │  services/chat      ──┐       │
                        │  services/mail      ──┤       │
                        │  services/approvals ─┤       │
                        │  services/proxy      ─┤ Mongo │
                        │  services/secrets    ──┤ Redis │
                        │  services/files ─MinIO─┤       │
                        │  services/control    ──┘       │
                        └───────┬───────────────────────┘
                                │ wake calls / desired state (pull)
             ┌──────────────────┼──────────────────┐
             ▼                  ▼                  ▼
      ┌────────────┐     ┌────────────┐     ┌────────────┐
      │ AGENT HOST │     │ AGENT HOST │     │ AGENT HOST │
      │  agentd    │     │  agentd    │     │  agentd    │
      │  agent:a.. │     │  agent:b.. │     │  agent:c.. │
      │  (systemd) │     │  (systemd) │     │  (systemd) │
      │  docker    │     │  docker    │     │  docker    │
      └────────────┘     └────────────┘     └────────────┘
```

Two planes:

- **Data plane** — chat messages, files, secrets, LLM tokens. Agents are
  clients of the central services, exactly like humans (via web) are.
- **Control plane** — `services/control` + `agentd` on every agent host:
  desired state, wakes, rollouts, health. Agents never talk to it about
  content, only about their own process lifecycle.

## 2. Services (central host)

Common: Python 3.12 + FastAPI + MongoDB + Redis. All services authenticate
callers by actor token (R22) and write audit events to the control ledger.

### 2.1 services/chat — actors, org, conversations

Collections (sketch):

- `actor(id, kind: human|agent, display_name, ...)` — one roster for people
  and agents (R12).
- `org_node(actor_id, parent_actor_id)` — the hierarchy tree (R6). Ancestors
  = superiors. Maintained in the Meteor admin UI.
- `conversation(id, kind: dm|channel|group, members[])`
- `message(id, conversation_id, sender_id, body, attachments[], created_at)`
  — append-only.
- `inbox(message_id, recipient_id, delivered_turn, received_at)` — per-agent
  delivery view. `delivered_turn IS NULL` = not yet seen by the agent.

APIs:

- REST for tools: `POST /messages`, `GET /conversations`, `GET /history`,
  `GET /inbox?undelivered=1`, `POST /inbox/deliver`.
- WebSocket for humans (Meteor) and for anything wanting live updates.

**Wake policy (R3, FIRM):** on message create the chat service computes
recipients → DM ⇒ wake each recipient agent immediately; channel/group ⇒
wake only @mentioned agents; everything else just lands in the inbox and is
picked up by the next heartbeat. Wake = chat service asks `control` to poke
the agent; a failed wake is never fatal — every turn starts by draining the
inbox, and heartbeats are the safety net.

### 2.2 services/mail — company email for agents

- Every registered agent gets a real inbox, `<agent>@<company-domain>`
  (domain + mail server configured at deploy time; service self-manages
  mailboxes via the mail server's API, or delivers to one catch-all and
  routes by recipient address).
- **Inbound**: SMTP receipt → normalized message → lands in the agent's chat
  inbox (sender shown as the external address) → delivered by the standard
  injection rule at next turn. No push.
- **Outbound (gated)**: the `mail.send` tool never sends directly. It
  creates an approval (`action_type: mail.send`, payload = full MIME draft:
  recipients, subject, body, attachments) routed to the requester's direct
  superior. On approve → service sends via SMTP → the sender agent gets the
  outcome injected ("Your email to X was approved and sent at <ts>"). On
  reject → outcome injected with the approver's reason. Expiry (default 72h)
  → outcome injected as expired.
- Humans can have mailboxes too if ruled later; v1 is agent-only (spec).

### 2.3 services/approvals — the gate for gated actions

- `approval` objects as defined in SPEC; append-only state history
  (`pending → approved|rejected|expired`, no edits).
- **Gated-action registry**: per action-type, scope global / per-org-node /
  per-agent — editable in admin UI. Lookup order: agent → org node → global
  default. v1: `mail.send` gated everywhere (spec-mandated); all other
  action types ungated until ruled.
- **Approver routing**: default = requester's direct superior (from the org
  tree, maintained in chat service; ancestry = stored path, single lookup).
  Multiple superiors (matrix orgs) → first configured approver, or "any of"
  — needs a small ruling when we get there; v1 assumes tree.
- **Delivery to approver**: approval requests are delivered like messages —
  web UI badge + list for humans; for agent approvers, an injected
  "Approval requested: …" block plus the `approval.decide` tool. Agent
  approvers decide in their own single session like everything else.
- **Audit**: every create/approve/reject/expire + the gated action's
  execution outcome lands in the control audit ledger (R23).

### 2.4 services/proxy — inference gateway (R17, R31–R34)

Job-queue based, not streaming-first. Agents submit generation jobs and get
woken (proxy → control → agentd → agent, reason `generation-done`) when the
result is ready. Rationale: with many agents, hosted rate limits and slow
endpoints make synchronous serving unreliable; a scheduler degrades
gracefully (queues, defers to rate-window resets, fails over).

**Request shape** — the agent runtime owns content (transcript window,
persona, scratch — assembled locally via the shared `treegent-runtime` lib
into OpenAI-format messages with stable-prefix discipline for caching); the
proxy owns the wire (provider formats, tool-schema translation, retries,
metering). The proxy never sees the raw transcript.

**Model selection (R31)** — class-based dynamic routing. Admin defines task
classes (cheap / standard / reasoning / vision / …) each mapping to an
ordered provider preference; a live model catalog (auto-refreshed from
provider APIs + admin-editable) resolves class → concrete model that is
currently listed, healthy and within rate budget at dispatch time. No
per-agent model assignment (churn), no agent free choice (agents can't
self-assess). Heartbeats map to a cheap class by default; work turns to a
stronger class — the biggest spend lever.

**Priority + serialization (R32)** — priority = org-hierarchy weight ×
inverse token usage over the trailing 48h (senior AND frugal agents
dispatch first). Max ONE in-flight generation per agent: later triggers
(including heartbeats) queue behind the active one or merge into the next
turn — an active request is never cancelled and never runs parallel to a
second one for the same agent (pre-heartbeat and post-heartbeat results
must not interleave into one sequence).

**Providers (R33)** — any OpenAI-compatible endpoint is a provider config
entry (base_url + key + rate budget); self-hosted vLLM is just another
entry, no special casing.

**Modalities** — wire format is OpenAI content-parts (text/image) from day
one; M2 implements text in/out + image input; embeddings endpoint included
(vision checks against catalog capabilities; audio/video generation deferred
until a tool needs it).

**Caching** — stable prefixes (persona+system first, tools stable, newest
last); provider-native cache insertion (Anthropic cache_control,
OpenAI prompt_cache_key = agent id); same-session provider affinity while
healthy (switching providers discards warm prefix cache).

**History (R34)** — full content stored: prompts, completions, tool calls,
per job — feeds the web history viewer ("chat and action history", spec).

**Metering** — `usage_event(agent, class, model, provider, tokens_in,
tokens_out, latency_ms, cost_est, queue_wait_ms, status, ts)`; per-agent
budgets/caps enforced here. Priority's usage factor reads the same ledger.

Collections: `agents_keys`, `task_classes`, `providers`, `model_catalog`,
`jobs`, `job_results`, `usage_events`, `history`.

REST sketch:

```
POST /v1/jobs            submit generation job (agent key)
GET  /v1/jobs/{id}       poll job status/result (fallback; wake is primary)
POST /v1/embeddings      synchronous (small, fast, non-queued)
GET  /v1/models          catalog for admins/runtime
admin: classes/providers/catalog/budgets (via web admin UI)
```

### 2.5 services/secrets (R9, R16)

- Entries: `secret(id, owner_id, name, ciphertext, acl[], notes)` —
  envelope-encrypted at rest with the service master key (server-side, so
  superior access is enforceable; no client-side E2E — that would break R16).
- Read rules: owner ∨ explicit ACL share ∨ org-tree ancestor of owner.
- Every read/write/share is audit-logged (who, what entry, when — never the
  plaintext value).
- API: `write`, `read`, `list`, `share`, `revoke`, `rotate`.

### 2.6 services/files (R8)

- Blobs in MinIO (S3 API); metadata + ACL in PG: `file_object(id, owner,
  path, size, sha256, acl[])` with share-space prefixes
  (`/shared/<channel>/…`, `/home/<actor>/…`).
- Linux mount: WebDAV gateway in front of the API (davfs2/GVFS mount);
  FUSE client later if WebDAV performance annoys us.
- API: `put/get/list/share/delete` + presigned URLs for big transfers.

### 2.7 services/control — fleet brain (R19, R23)

- Desired state: hosts, agents (which host, version, model profile,
  heartbeat config, persona prompt), routing table references.
- Enrollment: bootstrap token → host registers → gets host identity + per-
  agent service tokens (R22).
- Wake relay: `POST /wake/{agent}` routed to the right host's agentd.
- Rollouts: version an artifact (wheel/tar) into the artifact store, then
  flip desired state; agentd pulls and applies (see §4).
- Audit ledger: append-only stream of every tool call agents report + all
  service-side auth events. Feeds the web "action history" (R23).

### 2.8 services/web — Meteor

- Human chat client (DMs, channels, live updates) — humans are actors, so
  this is a view on services/chat.
- Files browser, secrets manager UI (with hierarchy-aware visibility).
- Approvals: pending-approval inbox for humans, gated-action registry
  editor, approval history (who decided what, when).
- Admin: org tree editor (R6), agent CRUD + host assignment + heartbeat
  config + persona prompt, proxy routing table + budgets, usage dashboards.
- History viewer: chat log timeline fused with action history per agent.

## 3. Agent runtime — "a person" (Python, R2)

One process per agent, supervised by agentd, on a dedicated host (R5).

### 3.1 The life

- **One session, forever (R15).** Transcript = append-only JSONL on the
  agent host + periodic flush/backup to services/files. Heartbeats, messages,
  admin turns — all appended to the same thread.
- **Context management** happens inside the runtime: when the live window
  overflows, older turns are folded into an additive summary block
  (summarizer call through the proxy). The log itself is never rewritten
  (R13). Summary blocks are marked, reversible, and stored alongside.
- **No subagents, ever.** Delegation = `chat.send` to a colleague. Long work
  = tool calls in the agent's own turn (bounded exec steps), reported on
  completion via a self-wake.

### 3.2 Turn pipeline

```
trigger (wake | heartbeat | admin)
  └─▶ drain inbox (GET /chat/inbox?undelivered=1)
        render injection block, VERBATIM shape (spec):
          "You have a new message from <X> received at <ts>: <body>"
          (one line per message; DM/channel noted)
        mark delivered (delivered_turn = this turn id)
  └─▶ append injection as the turn's opening user block
  └─▶ model call(s) via services/proxy (agent key)
        tools loop:
          chat.send / chat.read / chat.list
          mail.send (gated: creates approval, never sends directly)
          approval.list / approval.decide (for agent approvers)
          secrets.* / files.*
          exec (subprocess in ~/work, timeout, output caps)
          docker (CLI; sibling containers for dev environments)
          browser (headless Chromium via CDP in the agent home)
          scratch.write (agent's own notes, injected next turn)
  └─▶ append assistant blocks + tool results to transcript
  └─▶ report tool calls to control audit ledger
```

Rules: one turn at a time per agent; triggers arriving mid-turn queue; a
queued wake collapses into the next turn's inbox drain (no double wakes).
Heartbeat due while busy ⇒ deferred, not dropped.

### 3.3 Heartbeat

- Config from control: interval, active hours, optional cheap model.
- Timer enqueues a trigger; turn starts with an injected
  `Heartbeat: <interval> elapsed since your last turn.` block + scratch.
- Scratch (small persistent checklist/notes) rides the heartbeat prompt;
  agent updates it via `scratch.write`. Inspired by OpenClaw's monitor
  scratch, adapted: ours lives in the agent's own home, not the gateway.

### 3.4 Persona & memory

- System prompt = persona card (name, role, org position, who their
  superiors/colleagues are, communication norms) generated from control
  state — editable in admin UI.
- Long-term memory = files in the agent's home (`memory.md`, journals),
  readable/writable via tools; injected as pointers, not bulk.

## 4. Agent hosts & fleet ops (R18–R20)

- **agentd** (Python, systemd unit): the only thing installed manually on an
  agent host, once. It:
  1. enrolls with control (bootstrap token),
  2. pulls desired state on interval + on wake,
  3. manages agent processes as systemd units (`TreeGent-agent@<id>`),
  4. exposes a localhost wake endpoint (called via control),
  5. applies rollouts: download artifact → stage → flip symlink → restart →
     report; automatic rollback if the new version fails its startup probe,
  6. reports host health (disk, mem, load) + per-agent process health.
- **Agents are systemd services, not containers** (R18 primary): native
  Docker access for dev environments (agents launch sibling containers);
  resource limits via cgroup directives (CPUQuota, MemoryMax) per unit;
  per-agent home with quota. If a host must be shared more aggressively we
  fall back to containerized agents with the Docker socket mounted — same
  sibling-container semantics, slightly weaker isolation.
- Per-agent `$HOME`: `~/life/` (transcript, scratch), `~/work/` (exec cwd,
  git repos), `~/mem/` (journals). Everything in `life/` and `mem/` is backed
  up to services/files on flush.

## 5. Deployment (R5: central + fleet)

- Monorepo:
  ```
  services/{chat,proxy,secrets,files,control}/   Python (uv workspace)
  services/web/                                  Meteor (pnpm)
  agent/                                         runtime lib + agentd
  deploy/compose/                                central host stack
  deploy/agent-host/                             agentd install script
  ops/                                           migrations, seeding, docs
  ```
- Central host: Docker Compose, one `deploy` command updates services
  (build → migrate → rolling restart via health checks).
- Agent hosts: `curl -sL central/bootstrap | bash` style one-liner installs
  agentd; everything after that is remote-managed (R19).
- Config/secrets of the deployment itself (provider keys, master keys) live
  in an operator-owned env/age setup — not in the repo.

## 6. Security model (v1 scope)

- Private network or Tailscale-style overlay; services not exposed publicly
  except web (behind TLS + human auth).
- Actor tokens per agent; services authorize per call and audit (R22, R23).
- Secrets server-side encrypted; hierarchy read rule enforced + audited (R16).
- Agent containers/exec run as non-root user on the host; docker group
  membership is deliberate (R18) — the trust boundary is the host, one agent
  (or one team) per host for anything sensitive.
- Prompt-content privacy default in the proxy (§2.2).

## 7. Milestones (R4: platform first)

| # | Deliverable | Done means |
|---|---|---|
| M1 | chat + web messaging | 2 humans DM + channel via Meteor; org tree editable; inbox tables + wake stubs |
| M2 | proxy | agents' keys work against 1 real provider via OpenAI-compatible API; usage dashboard |
| M3 | approvals + mail | approval objects + gated-action registry in admin UI; agent mailboxes; inbound mail lands as inbox injection; outbound `mail.send` gated → approve/reject round-trip proven by test |
| M4 | secrets + files | CRUD/share via API + web UI; hierarchy access proven by test; WebDAV mount works |
| M5 | control + agentd + runtime v1 | agent enrolled on 2nd host, one continuous session, injection block proven, heartbeat fires, tools: chat + exec + scratch + approval.decide + mail |
| M6 | tools v2 | browser + docker; action history visible in web |
| M7 | fleet ops | rollouts with rollback from admin UI; resource limits; backups of `life/` |

Each milestone ends with a demo the operator can see (his eyes, not tool-success).
