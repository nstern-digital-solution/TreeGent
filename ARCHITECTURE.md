# ARCHITECTURE — constellation

Design contract derived from SPEC.md + RULINGS.md. Where this document and
RULINGS conflict, RULINGS wins. Divergences require a ruling change first.

## 1. Shape

```
                        ┌───────────────────────────────┐
                        │        CENTRAL HOST           │
                        │                               │
  Humans ──Meteor WS──▶ │  services/web (Meteor)        │
                        │  services/chat      ──┐       │
                        │  services/proxy      ──┤       │
                        │  services/secrets    ──┤ Mongo │
                        │  services/files ─MinIO─┤ Redis │
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

### 2.2 services/proxy — inference gateway (R17)

- OpenAI-compatible: `/v1/chat/completions`, `/v1/embeddings`, streaming.
- Auth: per-agent keys (`sk-agt-…`) issued by control at enrollment.
- Routing: model → ordered provider list (OpenAI/Anthropic/local vLLM/…),
  fallback on error, retries with backoff. Routing table in PG, editable via
  admin UI.
- Metering: every request → `usage_event(agent, model, provider, tokens_in,
  tokens_out, latency_ms, cost_est, status, ts)`. Per-agent budgets/caps
  enforced here (hard stop or alert).
- Never stores prompt content by default — metadata only (privacy default,
  toggle per deployment).

### 2.3 services/secrets (R9, R16)

- Entries: `secret(id, owner_id, name, ciphertext, acl[], notes)` —
  envelope-encrypted at rest with the service master key (server-side, so
  superior access is enforceable; no client-side E2E — that would break R16).
- Read rules: owner ∨ explicit ACL share ∨ org-tree ancestor of owner.
- Every read/write/share is audit-logged (who, what entry, when — never the
  plaintext value).
- API: `write`, `read`, `list`, `share`, `revoke`, `rotate`.

### 2.4 services/files (R8)

- Blobs in MinIO (S3 API); metadata + ACL in PG: `file_object(id, owner,
  path, size, sha256, acl[])` with share-space prefixes
  (`/shared/<channel>/…`, `/home/<actor>/…`).
- Linux mount: WebDAV gateway in front of the API (davfs2/GVFS mount);
  FUSE client later if WebDAV performance annoys us.
- API: `put/get/list/share/delete` + presigned URLs for big transfers.

### 2.5 services/control — fleet brain (R19, R23)

- Desired state: hosts, agents (which host, version, model profile,
  heartbeat config, persona prompt), routing table references.
- Enrollment: bootstrap token → host registers → gets host identity + per-
  agent service tokens (R22).
- Wake relay: `POST /wake/{agent}` routed to the right host's agentd.
- Rollouts: version an artifact (wheel/tar) into the artifact store, then
  flip desired state; agentd pulls and applies (see §4).
- Audit ledger: append-only stream of every tool call agents report + all
  service-side auth events. Feeds the web "action history" (R23).

### 2.6 services/web — Meteor

- Human chat client (DMs, channels, live updates) — humans are actors, so
  this is a view on services/chat.
- Files browser, secrets manager UI (with hierarchy-aware visibility).
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
  3. manages agent processes as systemd units (`constellation-agent@<id>`),
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
| M3 | secrets + files | CRUD/share via API + web UI; hierarchy access proven by test; WebDAV mount works |
| M4 | control + agentd + runtime v1 | agent enrolled on 2nd host, one continuous session, injection block proven, heartbeat fires, tools: chat + exec + scratch |
| M5 | tools v2 | browser + docker; action history visible in web |
| M6 | fleet ops | rollouts with rollback from admin UI; resource limits; backups of `life/` |

Each milestone ends with a demo the operator can see (his eyes, not tool-success).
