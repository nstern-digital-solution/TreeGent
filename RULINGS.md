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
| R42 | Agent tools v1 | FIRM | core (chat.send, mail.send + check, approvals.list/decide, secrets.list/read/write) + code execution (subprocess in agent workspace, timeouts) + web fetch (page→text; full browser automation later) + web search (configurable backend, default off) + memory (search/write in own workspace). FILES: amended by R77 — files API tools exist (files.ls/pull/push over the agent-key route); the OS-mounted share intent is DEFERRED |
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
- **R55** — Assigning/moving agents between hosts: chat `PATCH /internal/agents/{id}/host` (host_id or null=central; 404 on unknown host, 401 without service token), web `tg.setAgentHost` (admin-only, normalizes ''->null), per-agent host dropdown in the Admin tab. Central runtime claims only `host_id=None`; a host runtime claims only matching `host_id`.
- **R56** — Agent hosts get NO database credentials. Hosted runtimes are pure HTTPS clients of the central services: per-host key (X-Host-Id/X-Host-Key, hash stored in agent_hosts, secret only in the host's runtime.env), chat `/internal/host/*` serves exactly that host's agents+keys+pending work (404 for anyone else's), transcripts live on the host (workspace sessions/*.jsonl, atomic rewrite), turn summaries POST back. Mongo URL never leaves the central box. Central runtime keeps direct Mongo (shared box, exec off).
- **R57** — Observability: (a) per-job inference history — model, tokens in/out, status, error, reason on every job row + `/v1/history` endpoint; (b) session explorer — every turn's injections, replies, and tool calls+args+results recorded as transcript lines (agent_transcripts; hosted hosts push over their authenticated channel, central writes directly), Sessions tab (admin) renders jobs table + full transcript.
- **R57e** — Session store hygiene: failure-storm bloat (duplicate unread-backlog re-appends + `[generation failed]` retry stubs, Rosa: 802 KB/1,726 lines → 259k tok-in per request) healed on every session load — consecutive-duplicate user lines collapsed, failure stubs dropped, 200-message tail cap. Applies to hosted (file) and central (Mongo) modes. Chat view: timestamps/args demoted to ⓘ tooltips; bodies >600 chars collapsed behind show-all.
- **R58** — Async messaging contract RESTORED (operator re-ruled 2026-10-06): injections are NOTIFICATIONS ONLY — "You have N new message(s) from X — use chat.check". Message bodies NEVER enter context automatically; the agent pulls them via the new `chat.check` tool (filters: sender, unread, since, limit; marks results seen). Hosted agents route it over the host tier (X-Host-Key), central via service token. The M-era ": <body>" extension is reverted — it caused the context bloat and re-answering artifacts.
- **R58b** — Split message lifecycle (2026-10-06): `delivered_at` = notification acked by the runtime turn; `seen_at` = body pulled via chat.check. The unread filter keys on `seen_at` ONLY — a turn's ack must never mark a message read before the agent saw its body. Fixes "notification says 1 new, chat.check returns nothing" (single shared flag, two owners).
- **R59** — mail.read tool (2026-10-06, from Rosa's gap report): agents can now READ mail. mail.read (no args) = own mailbox overview incl. the agent's own address; optional mailbox=<shared box they belong to>/limit/unread_only. Mail service GETs /mailboxes + /mailboxes/{id}/messages are now key-authenticated (R45: key-derived identity, caller_id param never trusted) with hard own-scope enforcement — personal boxes readable only by owner, shared only by members. mail.check remains approvals-only (renamed semantics documented in tool description).
- **R60** — Inbound mail BUILT (2026-10-06, closing the R38 gap): POST /mail/resend/inbound webhook authenticated by shared URL secret (?secret=<TG_RESEND_WEBHOOK_SECRET>; 403 without/bad). Resend posts inbound events; mail lands in the matching mailbox and wakes recipient agents (R35). The old dev simulate hook /internal/inbound is now service-token gated (was unauthenticated). Personal mailboxes are auto-created at mail-service startup from persona_name@TG_MAIL_DOMAIN — Rosa's box appears on restart with the domain set.
- **R60b** — Orphaned-mailbox claim (2026-10-06): a personal box whose canonical _id exists but whose owner actor no longer exists (agent row recreated with a new id — same persona -> same address -> same _id) is CLAIMED for the current agent at mail-service startup. Existence-by-_id alone silently hid such boxes from the owner's mail.read (own-scope filter: owner != my id -> empty list). A box owned by a LIVE different actor is never stolen (name-collision WARN instead).
- **R60c** — Permission rules are DATA (R40) but nothing ever seeded them: fresh deploys denied EVERYTHING with no error (can() deny-by-default, no permissions rows -> mailboxes.list False -> empty list -> "no mailbox yet"). mail service now seeds RULE_SEEDS idempotently at startup ($setOnInsert — root edits preserved). Dev boxes had rows from M3 manual testing, which is why dev replicas passed while prod failed.
- **R61** — Secrets "create for" (2026-10-06): the actor dropdown in Secrets was view-only (reach-down listing); the create form never sent an owner, so every new secret was attributed to the CALLER regardless of selection. Now: creating while viewing a subordinate's scope creates it OWNED by them, shared back to the caller; the form header says who it's for. Server-enforced: only a superior (org ancestor of the target) may set owner — peers/bosses get 403. Absent owner field = self-owned (unchanged).
- **R62** — security hardening (2026-10-07, external audit findings 01–06): (a) chat REST org mutations (actor create/reparent/delete) now require a root-human/admin principal — agent keys and non-admin humans get 403, first-actor bootstrap preserved; (b) all `tg.hosts.*` DDP methods require logged-in admin before any runtime fetch; (c) `mail.api` no longer forwards client-supplied actor IDs as identity — superior reach-down is read-only GET within own subtree, approvals always act as the real caller; (d) mail/approvals/turns/usage publications are resource-scoped (own/subtree/admin) and reactively re-scoped; (e) mail `/adapters` endpoints require the central service token; (f) fail-closed credentials: services refuse to boot with empty/dev/placeholder TG_SERVICE_TOKEN unless TG_DEV=1, install.sh rejects placeholder .env, run-central.sh no longer silently defaults the token, runtime daemon never accepts an empty credential pair.
- **R63** — secrets scope hygiene + real Resend inbound (2026-10-07): (a) create-for no longer implicitly shares back to the creating superior — the secret lands ONLY in the subordinate's scope; sharing back requires an explicit shared_with entry; (b) Secrets "my scope" shows own rows only (reach-down metadata no longer pollutes the personal list); (c) Resend webhook rewritten for the REAL email.received payload ({type, data:{to:[...], from, subject, email_id}} — metadata only; the old parser expected a nonexistent data.email.to string and silently dropped every real inbound); body fetched via GET /emails/{id}/received with RESEND_API_KEY (best-effort; metadata-only delivery when unreachable).
- **R63e** — turn limits + hosted heartbeats (2026-10-08): (a) hitting max_turn_steps (24) used to end the turn SILENTLY — no reply, no transcript note; now the cap appends a [turn limit reached] notice to transcript+context and grants exactly ONE closing generation (tool calls ignored in it) so the agent always ends with a status reply (completed/remaining/resume point); (b) hosted agents' heartbeat trigger called _cdb() (central Mongo) which hosted runtimes don't have (R56) — EVERY hosted heartbeat turn crashed with AssertionError since R58; now tier-aware: hosted uses settings.heartbeat_s; TG_RUNTIME_MAX_TURN_STEPS env override added.
- **R64** — turn limits replaced by failure-pattern breakers (2026-10-08, Felix's ruling): a step cap cuts healthy long jobs mid-work — the agent's LIFE is the chatlog, not a step count; protection belongs against FAILURE PATTERNS, not effort. New defaults: max_turn_steps=0 (UNLIMITED steps; >0 = explicit hard cap, TG_RUNTIME_MAX_TURN_STEPS); breaker 1: 24 consecutive FAILED tool calls (ERROR: results) closes the turn (TG_RUNTIME_MAX_CONSEC_FAILURES); breaker 2: the same tool call (name+canonical args) 8x unchanged closes the turn (TG_RUNTIME_MAX_IDENTICAL_STEPS). Every breaker close ends with the R63e closing generation — a status reply (completed/remaining/resume), never silence. Proven: 40-gen varied healthy job runs free; 24-fail streak stops at 25; identical loop stops at 9; INTERMITTENT errors (never 24 consecutive) keep working.
- **R65** — webhook misconfiguration must be loud (SUPERSEDED by R67 — webhook deleted; pull-through reads) (2026-10-08): TG_RESEND_WEBHOOK_SECRET unset previously made POST /mail/resend/inbound return HTTP 200 {"delivered": false} — Resend's dashboard showed "delivered" while we stored NOTHING (prod state found by probe: secret not set, 0 messages anywhere). Now: 503 + journal line "[mail] DROPPED inbound webhook: TG_RESEND_WEBHOOK_SECRET is not set" so Resend retries and the operator sees it. Operator steps to enable: (1) generate secret, (2) add TG_RESEND_WEBHOOK_SECRET=... to /opt/TreeGent/.env, (3) systemctl restart treegent, (4) Resend dashboard webhook URL https://tree.nstern.de/mail/resend/inbound?secret=<same>.
- **R67** — inbound mail is PULL, not push (2026-10-08, Felix's ruling): "why do we even use a webhook, we do not need real-time emails — if an agent calls the tool we pull their emails, thats it." Webhook endpoint DELETED (no public inbound URL, no TG_RESEND_WEBHOOK_SECRET, no Resend dashboard registration — and the fake-mail wake billing surface dies with it). mail.read + Mail tab now pull-through: every read syncs from Resend's receiving API first (GET /emails/receiving + per-mail body fetch), imports new mail deduped by Resend email id, then serves. Pre-existing mail appears on first read (backfill for free). Resend outage degrades to local cache (logged, never 500s). Old mails keep their real received_at. The only mail config is RESEND_API_KEY.
- **R68** — runtime/proxy/deploy hardening (2026-10-08, external review of 7d0ff00): (a) proxy crash recovery — jobs stranded in 'dispatched' are requeued at startup + idle-sweep (15 min stale threshold, attempt count kept) and only RECENT dispatched rows 409 new submissions, so a crash mid-flight no longer locks an agent out forever; (b) _sanitize now extracts the system message BEFORE tail-capping (200+ message sessions used to restore with NO system prompt, and soul-refresh rewrote a conversation message at index 0); (c) the closing generation advertises NO tools and a still-empty reply is synthesized from turn bookkeeping (steps + breaker reason) — the guaranteed status reply is never empty; (d) quickstart config heredoc no longer aborts under set -u (source-time logic escaped; TG_MAIL_DOMAIN defaults to treegent.local); (e) the failed-turn ACK safety net now covers CENTRAL agents too and runs on crashed turns (was hosted-only + skipped exceptions — central agents re-polled the same failed wake every 5s); (f) SSRF deny-list on web.fetch + browser open: loopback/link-local/metadata/RFC1918/unique-local denied after DNS resolution, redirects re-validated per hop, TG_WEB_FETCH_ALLOW_PRIVATE=1 escape for dev/rehearsal; (g) demo agent key auto-seed gated behind TG_DEV=1 (never in production boots); dev scripts take TG_AGENT_KEY/TG_AGENT_ID via env/args instead of hardcoded actor ids.
- **R69** — independent bug-hunt fixes (2026-10-08, third-party review + hunt): (1) proxy meter idempotent (attempt-scoped id, terminal-status-first ordering — crash-after-meter no longer loops PAID retries forever); (2) central-tier acks id-scoped like hosted (message arriving mid-turn no longer blanket-acked = silent loss); (3) chat.check seen_at scoped to recipient (channel fanout no longer marks OTHER agents' inbox read); (4) stray decorator on _root_caller removed (was shadowing update_host with an UNAUTHENTICATED bool oracle — parent's own R68 edit bug); (5) approval decide atomic (find_one_and_update pending-claim — concurrent approves can no longer double-send real email) + send-failure returns to pending (retryable, not bricked); (6) dead mail_pending contract replaced: pull-sync now WAKES the owner on newly-discovered mail (deduped by resend_id); (7) R32 usage-decay priority actually works now (datetime cutoff vs float epoch matched zero rows); (8) deleted actors: keys revoked + wakes dropped at delete; proxy auth re-checks actor existence (no paid generations for the dead); (9) one-in-flight enforced by partial unique index (TOCTOU closed); (10) hosted runtimes no longer attach the daemon token to chat calls; (11) usernames path-safe charset (workspace traversal); (12) host_pubkey validates host_id (path traversal); (13) wake_events compound index + TTLs (wake 7d, messages 90d); (14) hosted wake payloads carry approval_id/detail (rejection reasons reached central only); (15) mark_ok anchored+escaped (sibling provider cooldown revival); (16) Retry-After HTTP-date handled (failover no longer aborts); (17) refresh_catalog off the event loop; (18) system prompt + reply matcher match the REAL notification format; (19) chat.check hosted query encoded; (20) trl ids salted; (21) add_member body minimal. Proofs: meter idempotent x2 attempts, atomic claim single-winner, B-unread preserved, one-in-flight dup rejected + done frees slot.
- **R68w** — web tier hardening (2026-10-08, external review of 7d0ff00): (a) secrets.api/files.api now mirror mail.api's impersonation policy (one shared forwardedActor): writes NEVER impersonate (asActorId on non-GET = 403, mutation always acts as the real caller), reads honor it only as read-only reach-down into own org subtree (or admin), and both forwarders route-whitelist their documented contract (UPLOAD = POST /files); (b) publishScoped fails CLOSED — a rescope error stops the observer and withdraws every published row (an uncertain scope serves nothing), then retries on a timer and on the next scope change; the old shape kept the stale observer + rows live so a revoked subordinate kept receiving data (proven A/B over live DDP); (c) the web server refuses an empty/dev/placeholder TG_SERVICE_TOKEN without TG_DEV=1 (mirrors treegent_common/security.py) and settings.json ships an EMPTY serviceToken (was: the dev token in-repo + silent code fallback); (d) the Mail tab triggers the R67 read-through sync on mount + a refresh button, with publications kept as the render source and errors surfaced. Drive-bys proven while fixing: runtime.api forwarded an undefined actor (ReferenceError every call); files UPLOAD never sent X-Actor-Id (every upload 400'd); tg.bootstrapAdmin/tg.createActor did not await Meteor 3's async Accounts.createUser so isAdmin/display silently never landed; MAIL_URL/PROXY_URL read settings.mailUrl/settings.proxyUrl instead of settings.private.* so configured URLs were ignored; mailMessages type-checks mailboxId (object arg = selector injection widening the publish query across every mailbox).
- **R70** — fractal quadtree memory (2026-10-08, Felix's design): before R70 there was NO compaction — _sanitize tail-capped at 200 messages / 160k chars and the overflow just LEFT the context silently (raw log stayed on disk, nothing resurfaced it; MEMORY.md was agent-initiated notes only). Now: when the chatlog crosses 96k non-system chars OR 160 messages (before _sanitize's caps), the oldest half is summarized by the metered provider stack (proxy class "task", tools off) into a ≤4000-char entry at L0 of workspace memory/qtree/. Every 4 entries on a level roll up into ONE ≤4000-char entry on the level above — unbounded levels, created as needed. Children are NEVER deleted: each L*.md is the complete append-only log of its era (readable via ws.read), meta.json tracks the rolled pointer. The system prompt always carries the NEWEST entry of EVERY level (<episodic-memory> block after <memory> in soul_block) — fine recent + coarse ancient history simultaneously; a 6k budget trims oldest eras first. Provider-down degrades to raw-tail entries, never a failed turn. Proofs: 16-compaction run builds L2 with all children kept; entry cap enforced; context block shows all levels; count+char triggers; E2E through the loop hook (120k→28k chars, 1 L0 entry, system prompt regenerated with the block); rehearsal 15/15.
- **R71** — version-drift red dot on the Hosts tab (2026-10-08, Felix request): the navbar Hosts button shows a pulsing red dot when (a) ANY host's version differs from the central server's (unreachable/unknown hosts do NOT count — no false alarms while a box is provisioning or rebooting), or (b) the CENTRAL server itself is behind origin/main — the shared box going stale is exactly the failure the quickstart egg-info incident exposed; hosts can all match central while the whole fleet is outdated. Backend: `/internal/hosts` now returns each host's `host_version_short` + `central_behind` (git fetch + rev-list count HEAD..origin/main, cached 5 min so the 60s health loop never hammers GitHub; -1 = check failed = no dot). Frontend: `HostsAlertDot` polls `tg.hosts.list` every 60s, renders NOTHING when healthy, fails open (API error → no dot — the Hosts pane is where errors are shown). Proven in-browser both directions: seeded stale host → dot with tooltip; row removed → dot gone. Admin-only by construction.
- **R72** — deploy-path hardening (2026-10-08, first-ever `treegent update` on prod aborted at 3/5): (a) the operator CLI's web-bundle step ran meteor as ROOT while install.sh chowns the repo to the service user — meteor refuses under a different owner ("sudo chown -Rh …" + exit 1); the build now runs as the treegent service user (root fallback only if the user doesn't exist), re-chowns the repo before building (uv sync as root leaves root-owned files), installs the bundle's server-side node_modules (`meteor build --directory` skips them — node main.js dies on bare `require` without them), keeps a full log at /var/log/treegent/web-bundle.log, and on failure prints the last 15 log lines instead of `tail -2` swallowing the error; (b) `treegent update`, quickstart re-run, and agent-host provisioning ALL pre-clean `packages/treegent-common/treegent_common.egg-info` before pulling — on pre-R68 checkouts that build metadata is still tracked, uv sync regenerates it in place, and the regenerated state blocks the pull (the prod egg-info incident; the guard is lossless — egg-info is untracked at HEAD); (c) provisioning's `git pull -q || true` no longer swallows a failed pull: it warns in the provision log that the host kept its old code and the operator should run Update. Proven: real meteor build end-to-end through the new function (build + server npm install + exit 0), fresh .env→runtime.env mapping validated, bash -n + ast.parse clean.
- **R72b** — proxy auth crash + error masking (2026-10-08, found live: Rosa still dead after full fleet update): (a) R69's "proxy re-checks actor existence" queried `db.actors` — a handle that was never added to the proxy's db.py; from the moment central updated to R69, EVERY authenticated POST /v1/jobs died with AttributeError → plain-text 500 → clients parsed it as JSON → `JSONDecodeError: Expecting value: line 1 column 1` → turn dead before any job row existed (hence "no attempted completion, no error" in the usage table — the table only lists dispatched requests). Fixed: `actors` handle added to proxy db.py. Proven over real HTTP (uvicorn subprocess, exact prod shape): valid submit 201 queued, second submit 409 one-in-flight, deleted-actor key 401 (R69's intent intact). Regression suite services/proxy/tests/test_jobs_auth.py (subprocess server; fails if auth 500s again). Fleet-wide AST sweep for referenced-but-undefined db handles across all services: clean. (b) Both runtime transports (Services._call, HostClient._call) parsed r.json() BEFORE checking the status code — any non-JSON error body (plain-text 500 from a crashing hop) surfaced as JSONDecodeError, masking the real status. Now parse defensively: non-JSON bodies become `RuntimeError: service 500: {"raw": "Internal Server Error"}` — the operator sees the hop and the status, never a parse crash. Proven with a mocked plain-text-500 transport; runtime suite 7/7.
- **R73** — compaction recalibrated to modern context windows (2026-10-08, Felix's ruling): R70's triggers (96k chars ≈ 24k tok / 160 msgs) compacted far too early for ~1M-token models. New scale, per Felix's 200k–400k-token band: trigger 1.2M chars (~300k tok) or 400 messages; the summarize-oldest-half logic (unchanged) removes ~150k tokens per compaction. The safety ceilings above the trigger moved with it — _sanitize is now 500 messages / 2M chars (~500k tok; it only catches a compaction failure). Proofs: suite re-pinned and 7/7; E2E at Rosa scale — 1.32M-char session (331k tok) compacts to 599k chars (~150k tok kept), L0 entry + regenerated system prompt; a 900k-char session correctly does NOT compact; 600-msg session tail-caps at 500 with the system prompt kept; the provider-down degrade path (raw tail as entry) accidentally exercised at full scale — turn unaffected.
- **R74** — merge mechanics + issue #8 final shape (2026-10-09, after the review-driven bug-fix train): (a) ALL merges are local `git merge --no-ff` authored under the sanctioned identity — NEVER GitHub API/web merges. A server-side merge stamps the account's primary (private) email on the merge commit and bypasses the pre-push privacy gate entirely; the gate caught exactly that once and the merge commit had to be re-authored and force-pushed. The gate only sees locally created commits, so authorship discipline is the control. (b) Issue #8 shipped per review as: per-turn reset of `_reply_ctx` and `_pending_senders` at the top of `_run_turn_inner` (they used to survive the life of the agent, so later heartbeats got auto-posted into a stale DM); the defensive sentence-parse hunk was DROPPED — `_pending_senders` (#18) owns the reply target and any text heuristic over notification sentences re-introduces issue #7's garbage target; the auto-post outcome is a status line (`[auto-posted to …]` / `[auto-post FAILED: …]`, non-user role) so tool error text can never be read back as a human turn.
- **R75** — lifetime-coverage in-context memory (2026-10-11, operator ruling): the in-context memory moves from "the latest summary at every level" to full "life time coverage" with as few summaries as possible. Ruling verbatim:
  > Also I think we should change the incontext memory system from containing the latest summary at every level to having a full "life time coverage" with as few summaries as possible.
  > Like now we allow all chunks in a level that are not yet included in the level above it to be in context so like at most 3 chunks at a level and if we just compressed it we may need 0 at a level at all.
  > This way we still use a few tokens while still maintaining "life time memory"
  > We should also not have any issues with the amount of context window the memory consums cause at 4000 char per summary (lets say ~ 1.300 tokens)  thats only 3.900 tokens in the context window per tree level at once.
  > so even if we reached like 10 levels ~39.000 tokens which for modern mdoels is no problem at all while one chunk at level 10 would cover a enourmous amount of compations, Basically our covered time is quadratic while a full "gapless" (but compressed) memory of a agents entire life time only grows linearly in context window (even with up to 3 entries per level

  Implementation: `context_block()` now carries every UNROLLED entry of every level (entries[rolled:] — the unrolled sets partition the agent's whole compacted life: gapless coverage of all time with ≤3 entries per level by fanout-4 construction and 0 at a level right after a rollup). Per-level era tags unchanged (L0 = "recent", Ln = "~4^n compactions ago"), entries chronological within a level, levels fine→coarse. Default context budget raised 6k → 200k chars [D] (≈50k tokens ≈ 16 levels × 3 × 4000) so the default can never silently defeat the ruling; for explicit small budgets the trim still drops coarsest material first, shedding a level's OLDEST unrolled entries before its newest (its newest is kept longest; the newest L0 entry is the last to go). Rollup mechanics, append-only level files, raw persistence, R73 triggers and the entry cap are untouched — this is a VIEW change. Proofs: suite 61 passed (zero regressions); the 21-compaction scenario pins the exact partition L0[20]/L1[4]/L2[0] (1+4+16 = 21 eras covered) and the 22nd compaction growing to 2 L0 lines with L1/L2 unchanged.
- **R76** — derived-memory layer: deriver + conclusions store + vector search + dreamer (2026-10-11, operator ruling on the Honcho-inspired proposal). Proposal verbatim:
  > Like we already run regular summaries. We could adjust the prompt to do both
  > summarizing and deriving at the same time. Then we only need to add a dreamer
  > agent + vector store/search (Vector store/seach is already supported my
  > mongodb) we just need to embed + the retrival agent query path.
  > This for "extended" out of context window memory for details + out "lifelong"
  > in context quadtree system could really be a good backbone for the system we are building

  The four decided parameters, verbatim:
  1. Query path:
     > "1. yes only top-k vector search results for now"
  2. Derivation scope:
     > "2. yes both"
  3. Embedding routing:
     > "3. yes"
  4. Dreamer cadence:
     > "4. I think we do 2 factors first like you said amount of conclusions added
     > second time like a min amount like maybe 6 hours between runs and a max
     > amount maybe like 2 weeks this way for low or high amount of conclusions we
     > do not run excessivly often or rare"

  Correction to the proposal (verified against the live self-managed mongod 8.0.12): `$vectorSearch` is Atlas-ONLY and is NOT available on our Mongo — the "vector store/search is already supported by mongodb" premise does not hold. Vector storage therefore stays workspace FILES (`memory/conclusions.jsonl`) and similarity is brute-force cosine computed in the runtime process, with a documented ceiling in code: fine to ~50k conclusions per agent; beyond that a real ANN index becomes worth adding. The file-store rationale is R56's: hosted runtimes have NO Mongo — every memory surface must live in the workspace and work on a pure-HTTPS client.

  Implementation: the existing compaction generation becomes a TWO-OUTPUT pass (same generation — no extra call): the summary, then a `===CONCLUSIONS===` line, then one `CONCLUSION: <text> || KIND: peer:<username>|task|state || FROM: <short premise refs>` line per derived conclusion — both peer conclusions and task/state conclusions ("2. yes both"). Old-format outputs (no section) still compact unchanged (back-compat). `record_compaction` extracts them (cap 12/pass, ≤400 chars/conclusion, kinds validated), embeds them via the proxy and appends to `memory/conclusions.jsonl` — append-only like the qtree L-files: `{id "cnc_<rand>", text, premises (model refs + the raw/<file> source pointer), kind, era "L0:<n>", ts, embedding, superseded_by}`. Embedding failure (no model configured / provider down) appends WITHOUT the embedding field and counts it — search skips such rows; the entire conclusions side is wrapped so compaction NEVER fails from it. `memory/dreamer.json` holds `{last_dream, conclusions_at_last}`.
  Embedding routing ("3. yes" — new class designation in the Models system): proxy `POST /v1/embeddings {"input": str|[str], "designation": "embedding"}` → OpenAI shape `{"data":[{"embedding":[...],"index":i}]}`, routed through the existing class/modality/rank machinery — the `embedding` designation is seeded like the other classes (now `$setOnInsert` upserts so existing deploys gain it too) and the catalog filter additionally requires the model to SUPPORT embeddings (modality filter pattern; catalog refresh learns it from provider listings). Same X-Agent-Key service identity, same metering/usage rows (class "embedding"), same failover-rank cooldown behavior as /v1/jobs. No embedding model configured → clean 503 "no embedding model" and the runtime degrades gracefully (a fresh deploy boots degraded-by-default).
  Search ("1. yes only top-k vector search results for now"): `memory.search` embeds the query (one short-timeout proxy call) and returns top-k (default 5, arg k, max 20) conclusions VERBATIM with their premises (`[peer:alice, era L0:21] <text> (premises: …)`), cosine-ranked in-process over the JSONL; superseded and unembedded rows are skipped. The existing MEMORY.md/notes regex grep rides along as separately labeled results `(source: notes)`; if the embedding call fails the tool degrades to notes-only plus a one-line `(vector memory unavailable)` — it never errors.
  Dreamer (per agent; trigger checked after each compaction — cheap): run when (a) new conclusions since the last dream ≥ DREAMER_FACTOR(2) × max(1, conclusions at last dream) AND ≥ DREAMER_MIN_S(6h) elapsed, OR unconditionally when ≥ DREAMER_MAX_S(14d) elapsed; first-ever run at ≥ 20 conclusions. The dream is ONE task-class generation (tools off) fed the conclusions since the last dream (cap 200) returning JSON: dedupes/merges (supersede listed ids + replacement text), contradictions (keep newest, supersede older), and cross-cutting patterns as NEW conclusions of kind "dreamer". Applied crash-safe: new records appended plus `superseded_by` patched in a rewritten temp file swapped via os.replace (lines are NEVER deleted — same append-only philosophy as the qtree); dreamer.json advances ONLY on success, so a failed dream retries on the next compaction. Proofs: fail-before pinned (all new tests red against the old tree); runtime suite 73 passed (61 + 12 new: prompt parse + back-compat, embedded/degraded/never-raise stores, cosine order with known vectors, top-k format + k clamp, labeled notes, dreamer cadence table — (2x, 5h) no / (2x, 7h) yes / (0.5x, 7h) no / (any, 15d) yes / first-run gate 20 — dreamer apply atomicity + failure-leaves-state); proxy suite 6 passed over a real uvicorn subprocess (503 unconfigured, 200 + OpenAI shape + usage rows written, 401 bad key, 400 unknown designation); mail suite 22 passed 8 skipped (untouched).
- **R77** — the real hierarchical file system + files API tools (2026-10-12, operator ruling: "please create an issue and start building the real file system" after reviewing the audit in issue #51; amends R42: FILES NOT via API tools no longer holds — **files API tools exist**, the OS-mounted share intent is DEFERRED). Shape: `services/files` is a real path-addressed tree — file docs carry normalized absolute paths (`/home/<username>/…`, `name` = basename) and folder docs are first-class (`{kind:folder, path, owner}`); `normalize_path` is the single guard (absolute-only, trailing / stripped, `//` collapsed, `..` rejected, depth ≤ 16, len ≤ 512, `[A-Za-z0-9._- ]` per segment → None ⇒ 400 everywhere). `POST /folders` = mkdir -p (idempotent), `GET /files?path=` = direct children, `DELETE /folders/<path>` = empty-only (409 otherwise); boot migration (idempotent) gives every legacy doc `/home/<owner-username>/<name>` and ensures home/ancestor folder docs. Sharing is prefix-based (`file_shares {path_prefix, with}`): a share covers the path AND everything below it, access is resolved PER REQUEST (files added under a shared folder are covered immediately; `DELETE /shares/{id}` revokes instantly), `POST /shares` requires subtree ownership (`/home/<me>` or the top folder they created; `/home/*` is a reserved personal namespace) and validates `with` against `db.actors` — NO member re-sharing (the audit hole) and `PUT /files/{id}/share` is now SET-REPLACEMENT (owner-only; `with: []` = full revoke). Listings stay R40 own-scope (own + member + covering shares; superiors named-read down but never list down). Agent tools (amends R42): `files.ls` / `files.pull` (workspace write, no silent overwrite) / `files.push` (own subtree only, 100 MB cap) over the agent-key Services client — NO approval gating (R26 keeps mail.send the only gated action); the stale "files" system-prompt mention now lists the three tools. Storage: `storage.py` with S3 (prod) + TG_DEV=1 in-memory backends behind identical functions; no service code touches the boto layer. Web: FilesPane breadcrumb/children browsing, upload-into-folder, new-folder, per-file + per-folder share dialogs with revoke; filesMeta publishes subtree metadata like secretsMeta (reach-down picker). Proofs: first-ever services/files suite **24 passed** (real uvicorn subprocess + disposable mongod + TG_DEV=1: normalize guard table, mkdir -p idempotency, direct-children listing, upload w/ path, folder-share inheritance incl. late uploads, member re-share 403, unknown-actor 400, set-replacement add→replace→empty, immediate revocation, superior named-read vs own-scope listing, presign 403 for strangers, empty-folder delete / 409, legacy migration + reboot idempotency); runtime suite **84 passed** (73 baseline + 11 tool tests: ls format, pull writes/refuses-overwrite/escapes, push own-subtree rejection incl. the username fail-closed case, upload shape, 100 MB guard, schema registration); mail suite 30 passed (mongod-gated run, untouched); web has no test harness (unchanged) — FilesPane/main.js proven by parse check + the rehearsal build.

- **R78** — token-based compaction budget (2026-10-13, Felix's ruling). Ruling verbatim:
  > we want to compress at > 50% of the models maximum context window

  Token count is the ONLY real signal; chars and message-count are retired proxies. Compaction triggers when the last MAIN-LOOP generation's provider-reported `usage.prompt_tokens` (already riding back inside the proxy job result — read it, never estimate or tokenize locally) EXCEEDS 50% of the active model's context window — strictly greater-than (0.50x exactly does NOT compact). The window is a proxy model-catalog field: catalog docs gain optional `context_window` (int tokens; admin API accepts/returns it on the model policy endpoint and GET /admin/catalog); the dispatcher stamps `context_window` on every DONE job doc from a cached catalog map (refresh on miss / every 5 min — never a Mongo read per job), falling back to `TG_DEFAULT_CONTEXT_WINDOW` else **32768** (conservative: compaction fires early rather than late) and NEVER failing a job over the lookup. The runtime loop captures `usage.prompt_tokens` → `last_prompt_tokens` and the job doc's `context_window` → `context_window` (compact/dreamer generations are separate contexts and never move these fields) and persists both in the life doc (agent_sessions; hosted runtimes: a `sessions/current.meta.json` sidecar next to the transcript) so restarts keep the measurement. maybe_compact token-targets the drop: the OLDEST non-system messages go until the char-proportion estimate (est = prompt_tokens × kept_chars/total_chars — token density is roughly uniform and the exact provider total anchors it) lands at **<= 25% of the window** (`COMPACT_TARGET_FRACTION`); the newest message always survives the floor. Backstop ONLY (no usage data yet / failure storm): >= **2000** non-system messages (`COMPACT_TRIGGER_MSGS`, was 400), halving to the newest 1000. **SUPERSEDED: R70's char/msg triggers and R73's recalibrated numbers (1.2M chars / 400 msgs) are RETIRED with this ruling** — chars are a proxy whose token density varies with content (a 10M-char log of tiny tokens is NOT over budget and must not compact). The safety ceilings moved with the trigger: `_sanitize` tail-cap 500 → **2500** so the 2000-msg backstop still fires BEFORE silent tail-cap loss (R70's invariant preserved: trigger < cap — information must never silently leave the context; the 2M-char budget trim stays as the compaction-failure ceiling). Every compaction logs its numbers (`[qtree] compacted: prompt_tokens=… window=… msgs=a -> b`). Proofs: runtime suite **94 passed** (88 baseline + 6: needs_compaction boundary table — 0.49x/0.50x exactly/0.51x no-yes-no, 1999/2000/2001, missing-usage backstop, 10M-char irrelevance; maybe_compact token targeting — lands <= 25% est with the minimal drop set keeping the newest tail, floor keeps the newest message; loop capture + life-doc persist/restore from a faked job result; hosted meta sidecar round-trip; _sanitize 2500 boundary + trigger<cap invariant); proxy suite **10 passed** over a real uvicorn subprocess + disposable mongod (6 baseline + 4: catalog context_window set/get + 400 on non-positive, done job doc carries the served model's window on the agent poll and in Mongo, missing-window → 32768 default stamped (TG_DEFAULT_CONTEXT_WINDOW override unit-checked), stub chat provider end-to-end); mail suite 34 passed (mongod-gated, untouched); chat live-app suite 26 passed (untouched). NOTE: the agent-facing `GET /v1/jobs/{id}` projection gained one field (`context_window`) so the runtime can read the stamp from the polled job — jobs.py was outside the briefed file scope but the design cannot work without it (single additive field, no auth/metering change).

- **R79** — mail attachments: NOTIFY-ONLY metadata + agent-controlled download (2026-10-13, operator ruling). Ruling verbatim:
  > "We need to add this feature but we should not store files by default
  > this is dangerous. Instead we should let the agent know there is a file /
  > files attached. And give it a tool where it can decide to download
  > attachments and specify storage location."

  **Notify-only doctrine (the non-negotiable core): NO attachment bytes are ever stored by default, anywhere.** The mail service persists only compact METADATA per inbound message — `attachments: [{id, filename, content_type, size}]`, captured at import from the Resend LIST payload (no extra API call) and backfilled for pre-existing rows by the `_heal_bodies` sweep (bounded by the existing `body_attempts` cap; a metadata-only backfill never re-wakes — that would replay #45's duplicate-wake bug class). `download_url` (signed, ~1h expiry) and `content_disposition`/`content_id` are STRIPPED — never persisted; a fresh signed URL is fetched per download request. The agent SEES what is attached: `mail.read` renders `| attach: report.pdf (13 KB, application/pdf)` per attachment from the messages listing (the compact array rides the public message shape), and mail wakes append `(N attachments)` so the agent knows to look.

  **The tool contract:** `mail.save_attachment {mail (mail row id), filename, to, overwrite=false}` — the ONLY path by which attachment bytes reach an agent's workspace. It resolves the attachment id from the stored metadata by filename (exact match; an ambiguity error LISTS the candidates with their ids, and an id may be passed to disambiguate; unknown name lists what IS attached), calls the mail service's download endpoint, and writes via `safe_ws_path` — default destination `attachments/<filename>`, path traversal REJECTED (same guard as files.pull), no overwrite without `overwrite=true`. No approval gate: `mail.send` stays the only gated action (R26) — the bytes land in the agent's own workspace, which it already fully owns.

  **Download endpoint:** `GET /mailboxes/{mbx}/messages/{id}/attachments/{att}` (agent-key or service-token auth like all mail routes): mailbox owner (personal) / member (shared) scope enforced BEFORE any lookup, message must belong to the mailbox, att id must exist in the STORED metadata (no metadata, no download), then Resend `GET /emails/receiving/{emailId}/attachments/{attId}` (named UA — the Cloudflare rule) for a fresh signed URL, bytes streamed through server-side with content-type + content-disposition. **Cap: `TG_MAIL_ATTACH_MAX`, default 26214400 bytes (25 MB)** — enforced at the stored metadata size BEFORE the provider fetch (413) and again while streaming (abort mid-transfer). Errors are bounded and friendly; remote bodies are never echoed.

  **OPEN (not ruled, not built): outbound attachments** — attaching files to sent mail is NOT part of this ruling and remains open; nothing in this change touches the outbound path (mail.send is unchanged and still sends text/html only).

  Proofs: mail suite **61 passed** (34 baseline + 27: compact-metadata-at-import incl. field-stripping, empty-list marker, wake-detail count + no-suffix; heal backfill for pre-#60 rows with no-re-wake pin + body+metadata one-pass heal; endpoint matrix over a real FastAPI app with real header auth — 401 no credential / 401 bad token / 403 unknown actor / 403 stranger (owner 200, shared member 200) / 404 unknown att / 404 wrong mailbox / 404 no provider id / 413 over-cap with the provider NEVER called / streaming round-trip with stub adapters asserting body + content-type + content-disposition / mid-stream over-cap abort / bounded 502 on provider errors / no-download-link 502; named-UA + documented path on the retrieve-attachment call; real `iter_attachment_bytes` cap + HTTP-error behavior over a stubbed httpx; env knob `TG_MAIL_ATTACH_MAX` binds); runtime suite **107 passed** (94 baseline + 13: mail.read attach-line rendering, save_attachment round-trip + custom `to`, traversal rejection, overwrite refusal + opt-in, ambiguity listing candidates + id disambiguation, unknown mail/attachment errors, tool + schema registration, system-prompt discovery next to mail.read, hosted + central wake '(N attachments)' rendering both with and without attachments). Every suite run used stub adapters/fake DBs — no real Resend call was made.

## R80 — self-hosted Forgejo forge; GitHub migration; auto-accounts (2026-10-15)

**Trigger:** the rosa-mar GitHub account was suspended for TOS violations;
our review flow lost a reviewer and two PR/issue objects vanished with no
appeal (work restored as PR #75). Ruling (Felix, 2026-10-15): move off
GitHub onto self-hosted Forgejo in Docker on the shared server, managed
through the API.

Ruling verbatim:
> "we need to move of github and have the shared server 'bundle' forgejo
> where we then can also auto setup account for every human or agent
> created in treegent. basically we run forgejo in a docker and connect
> our management through the api"

Follow-up rulings, same day:
1. **Public**, configurable domain (`TG_FORGE_DOMAIN`, e.g. git.nstern.de),
   Caddy-fronted TLS.
2. **Migrate but do not change anything on GitHub yet** — migration pulls
   (issues, PRs, labels, milestones, releases); the GitHub repo stays
   untouched until a later explicit cutover ruling.
3. **Management connection + automatic accounts** for every human/agent
   actor at creation. **PR and review-request events are NOT chat
   messages** (accounts only).

Review follow-up (Felix, PR #76): no extra deploy commands, and the domain
lives in the same env file as every other setting. So: `/etc/treegent/env`
gains `TG_FORGE_DOMAIN` (the only hand-set value); `treegent update` runs
stage 4b = `ensure-forgejo.sh` (container up, health-wait, first-run
bootstrap incl. token mint + env wiring, idempotent; skipped entirely when
the domain is unset or docker is absent). Migration stays a deliberate
one-shot script.

Implementation (PR feat/forgejo-integration):
- `deploy/forgejo/` — docker-compose (127.0.0.1:3001 bind, registration
  disabled, HTTPS-git only), `ensure-forgejo.sh` (update stage 4b),
  `bootstrap.sh` (health-wait, admin via container CLI, token mint,
  .env wiring, idempotent), `migrate-from-github.sh` (Forgejo migrate
  API; read-only on the GitHub side), README runbook.
- `services/chat/app/forge.py` — total/idempotent provisioner hooked into
  create_actor (fire-and-forget): forge down NEVER blocks or fails actor
  creation (actor doc records `forge.status=pending`; `GET /internal/forge/sync`
  retries). Disabled entirely when `TG_CHAT_FORGE_URL` is empty.
  Usernames mirror TreeGent usernames (lowercased `[a-z0-9-]`, max 40);
  placeholder mailbox `<user>@forge.treegent.invalid`; random 28-char
  password; `login_name` carries the actor id for traceability.
- `deploy/Caddyfile.example` — `git.<domain>` site block.
- No notification bridge: forge events deliberately do not become chat
  messages (ruling 3).

Proofs: chat suite **33 passed** (26 baseline + 7 new) against a live
uvicorn + disposable mongod; connector unit tests cover username mapping,
password alphabet, create, idempotent-422, forge-down totality,
disabled-by-default, pending retry via /internal/forge/sync.

## R81 — Agent identity: generated names, `agent-N` as infra handle (2026-10-16)

Felix: "I want to have her name! If we have many agents we need to have a
identifier like agent-1 which is setup by me and helps me know which agent
belongs to which vps but I will never say agent-1 do x and agent-32 do y!
This is stupid, the name i use is the one randomly generated and i need to
be able to use it in issues and pr too of course"

RULING (FIRM):
- **Generated name = the username.** Every agent gets a randomly generated
  given-name username (`rosa`, `mila`, …). This is what Felix addresses
  agents by: chat `@mentions`, forge issues/PRs, mailbox local-part,
  workspace dir. No one is ever asked to say "agent-1 do x".
- **`agent-N` is an infra handle, owned by Felix.** It stays as a separate
  operator-set field (`infra_id`) shown in Hosts/admin views for VPS
  mapping — it is NOT the username and never addressed.
- **Forge account = the name.** Forge username mirrors the TreeGent
  username (R80); with R81 the username IS the generated name, so
  `@rosa` in a forge issue works because the account is `rosa`.
- Renames: admin may rename an agent; rename propagates (actor doc,
  forge account via admin API rename, workspace path pinned via
  `infra_id`-anchored symlink, mailbox migration heals itself at mail
  startup per R49/R52). Old forge handles 307-redirect (verified in
  Forgejo 12.0.4 lab), so existing issue/PR references keep resolving.

Name generation: `first [SYLLABLES]` from a curated given-name pool,
title-case for display, plain lowercase username. Collision-checked at
creation against Mongo; retry with next candidate. Generated names are
stable — stored on the actor doc at creation, never regenerated.

Lab-verified (Forgejo 12.0.4, scratch/forgejo-lab):
- `POST /api/v1/admin/users` with `full_name` → 201
- `PATCH /api/v1/admin/users/{u}` `{"full_name": ...}` → 200
- `POST /api/v1/admin/users/{u}/rename` `{"new_username": ...}` → 204;
  old handle 307s. Rename endpoint exists in Forgejo 12.

Implementation notes:
- `ActorIn.username` remains a plain identifier field; agents created via
  host provisioning no longer force `agent-N` usernames — the generated
  name becomes the username, `infra_id` carries the operator handle.
- Existing `agent-N` actors: renamed via the new admin route at deploy
  time (one-shot), workspace symlinked, forge accounts renamed.

## R82 — Identity model final: operator ID vs generated name (2026-10-17)

Felix: "CAUSE THE ID IS SET BY ME BEFORE THE RANDOM NAME IS EVEN GENERATED!!!
YOU JUST RENAMED THE OLD USERNAME FIELD INSTEAD of making the old field the
id and adding a new display name field" + "YOU NOW HAVE ACTUALLY DESIGNED A
COHERENT SYSTEM OF USER AND DISPLAY NAMES??? AND USE IT EVERYWHERE! ALSO
YOU PASS THE CREDENTIALS TO THE AGENTS AND USE THE CORRECT EMAILS FOR
FORGEJO ACCOUNTS!!!"

RULING (FIRM, supersedes R81's username mechanics; R81's display/infra
intent survives):
- **username = the operator's stable ID** (agent-1). Set at creation,
  NEVER renamed, used verbatim. Join key for Meteor users, workspace
  directory key, VPS mapping. Immutable identity.
- **name = the generated given name** (rosa). New field. @mentions
  resolve against it (username also matches, legacy messages keep
  working). Forge account mirrors it — the addressable handle in
  issues/PRs.
- **display_name = full persona name** (Rosa Marchetti). UI only.
- **Forge accounts carry the agent's REAL mailbox address**
  (rosa.marchetti@<TG_MAIL_DOMAIN>, persona-derived, same logic as the
  mail service). @forge.treegent.invalid remains only as the no-domain
  fallback.
- **Credential handoff**: provisioning mints a scoped API token
  (write:repository, write:issue, read:user) and writes it into the
  agent's own secrets scope as `forge/token` (url, token, username).
  Passwords never stored — tokens are revocable and scope-limited.
- backfill_missing() stamps `name` on legacy agents and refreshes
  email/full_name on existing forge accounts at startup.
- The rename endpoint (PUT /actors/{id}/username) is RETIRED from the
  contract — stable IDs are not renamed. (R81's rename machinery is
  dead code to be removed.)

Verification: chat suite 44 passed (live uvicorn + fresh mongod).

## R82 review round (subagent, 2026-10-17) — 4 blockers fixed

1. R81 rename endpoint REMOVED (retired by ruling; it still wrote
   renamed_from — the loaded gun that broke the Meteor join).
2. Forge account now mirrors the GENERATED name (rosa), stable ID only
   as legacy fallback — @rosa resolves in issues/PRs. Tests updated to
   pin the correct contract.
3. Stale R81 workspace-migration test deleted (broke runtime suite at
   collection). Runtime suite: 139 passed + 3 skipped.
4. Workspace legacy-name adoption: an agent finding no dir for its
   stable ID but one for its generated name (R81-era) MOVES it — no
   silent amnesia.

Risks fixed: name-collision uniqueness now checks the NAME field (was
username — two rosas both passed); credential handoff fires for existing
accounts (422 path + backfill, agents only) with pending-state + logging
instead of silent swallow; resolve-actor matches name then username
(runtime DMs by generated name work); Admin UI + tg.createActor require
the stable username for agents (no half-created principals).
Suites: chat 49 passed (live uvicorn + fresh mongod), runtime 139+3.
Known residual (accepted): no JS test pins the Meteor myActor join —
the Python suites cannot see it; unbrick script does not rename forge
accounts back (forge handles rosa/greta stay — correct per ruling).

## R83 — Forge credential mint, spec-verified (2026-10-17)

Felix: "FORGEJO IS FULLY OPEN SOURCE YOU CAN CHECK ANY API AND BEHAVIOUR
BEFOREHAND. I DO NOT WANT ANY HALF ARSED SHIT ANYMORE"

Verified against the OFFICIAL v12.0.4 OpenAPI spec
(templates/swagger/v1_json.tmpl) and routers/api/v1/api.go BEFORE
writing code this time:
- POST /admin/users/{u}/tokens DOES NOT EXIST (my invented route —
  root cause of Rosa's missing credential).
- POST /users/{u}/tokens is real; reqBasicOrRevProxyAuth + reqToken +
  reqSelfOrAdmin: BASIC auth only (token auth refused), admin may mint
  for any user.
- DELETE /users/{u}/tokens/{id}: basic auth; GET list: admin token ok.
Full loop LIVE-PROVEN on Forgejo 12.0.4 before shipping: admin
basic-auth mint -> rosa token -> private-repo collaborator grant (PUT
/repos/{o}/{r}/collaborators/{u} 204 — required: private repos 404 for
non-collaborators even with valid token) -> clone -> branch -> push ->
PR #84 opened BY ROSA'S TOKEN.
Code: _handoff_credentials now uses the official route with admin
basic auth (TG_FORGE_ADMIN_PW), CLI fallback (TG_FORGE_COMPOSE_DIR),
deterministic 422 retry (list->delete->re-mint), failures recorded
(forge.handoff=pending + error), _grant_repo_access adds agents as
write collaborators on TG_FORGE_AGENT_REPOS. bootstrap.sh: known admin
password flow (create-or-reset) + env wiring for the three vars.
Chat suite 52 passed. Unit tests pin the spec contract (route, Basic
header, scopes, pending-not-silent).
