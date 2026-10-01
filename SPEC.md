# SPEC — constellation

Autonomous multi-agent company: agents and humans as colleagues on one shared
chat platform, with hierarchy, shared secrets, shared files, and metered
inference. Built from scratch, inspired by OpenClaw and Hermes Agent.

Working name `constellation` is a placeholder (RULINGS R1).

## Requirements (verbatim — the operator, 2026-10-01)

> I want to create a autonomous multi agent system with you inspired by
> openclaw and hermes agent.
>
> But with a few key differences like:
>
> One continues session per agent, no subagents, no multiple sessions or
> chats, a agent is a person his chatlog and hearthbeats are his "life"
> We build from the ground up for a full company with multiple agent and
> human actors that have a clear hierarchie, depending on the place in this
> hierarchy some agents might never or rarely message other humans.
> Instead of communicating through a message provider with a human, all agent
> and humans share a custom chat backend think slack or teams or discord,
> where direct messages and group chats are possible. Every agent has tools to
> check and write messages to other members and channels. If agents get a new
> message it doesnt get directly injected into their chat instead at the next
> possible turn we inject a message like "You have a new message from X
> recieved at xyz"
> additionally we also create like a custom "password/secret manager" where
> agents store important secrets and can share these with other agents in the
> company (also superiors can also always access subordinants secrets)
> we also have a custom file storage system that agents can use to store and
> share files with other agents. This needs to be mountable under linux
> somehow
> We also have a metoer based webinterface where human users can also message
> different agents, access the file system and password manager, also manage
> the agents and see their "chat and action history"
> We also need a central openai proxy that all agents run their requests
> through that can route these requests to different infernece providers and
> also logs per agent usage statics
> Every agent would need tools like browser, code/cli execution and run on a
> seperate system.
> So we would need a monorepo with different services and ways to deploy and
> update these centrally run shared services aswell as the agents.

## Interpretation (Hermes draft — not a ruling)

### Core concepts

- **Actor** — one member of the company: `human` or `agent`. Same chat API
  for both; humans additionally have web login, agents additionally have
  tools + inference.
- **Org tree** — every actor has a place; superiors can access subordinate
  secrets; hierarchy shapes who can message/summon whom. Source of truth: R6.
- **Session = life** — one agent, one continuous chatlog, ever. Turns are
  appended to it; context management (compaction/summarization) is internal
  and never forks the session. No subagents: work that would be "delegated to
  a subagent" elsewhere is delegated by *messaging a colleague* here.
- **Turn** — one agent execution: trigger → think → tool calls (incl.
  `chat.send`) → turn end. The only way an agent experiences the world.
- **Trigger** — one of: inbound message wake (policy R3), heartbeat timer,
  admin action from the web UI.
- **Injection** — at turn start, undelivered messages are rendered as a block:
  `You have a new message from X received at <timestamp>: ...` (per spec).
  Nothing is ever pushed into a running turn.

### Services (planned monorepo layout)

| Service | Owns |
|---|---|
| `services/chat` | actors, org, DMs, channels, message store, delivery queues, presence |
| `services/proxy` | OpenAI-compatible endpoint, per-agent auth, provider routing, usage stats |
| `services/secrets` | envelope-encrypted store, sharing, hierarchy-based superior access |
| `services/files` | shared file storage + ACLs, mountable on Linux (WebDAV gateway) |
| `services/web` | Meteor app: human chat client, files UI, secrets UI, agent admin, chat/action history viewer |
| `agent/` | per-agent runtime: supervisor, session persistence, heartbeats, tools (browser, exec, chat, files, secrets) |
| `deploy/` | central deploy/update for shared services and agents |

### Divergence from OpenClaw / Hermes

| There | Here |
|---|---|
| External providers (WhatsApp/Telegram/Slack) | Own chat backend is the only messaging surface |
| Subagents, ephemeral/parallel sessions | One continuous session per agent — a person |
| Single operator | Company: many humans + agents, explicit hierarchy |
| Gateway relays channels | Chat service is a first-class product (Slack/Teams-like) |
| Provider keys per agent config | Central proxy routes + meters all inference |
| Local workspace files | Multi-actor file service with ACLs, Linux-mountable |
| OS vault / per-install secrets | Own secret manager with hierarchy access rules |

Reference scan (2026-10-01): OpenClaw gateway docs confirm queue-then-inject-
at-next-turn and system-owned heartbeat cadence as a proven shape; we adapt it.
