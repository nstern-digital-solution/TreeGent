import { Meteor } from 'meteor/meteor';
import { Accounts } from 'meteor/accounts-base';
import { Actors, Conversations, Messages , AgentTranscripts, Jobs } from '../imports/collections.js';
import { TaskClasses, Providers, ModelCatalog, UsageEvents } from '../imports/proxyCollections.js';
import { Mailboxes, MailMessages, Approvals } from '../imports/mailCollections.js';
import { Secrets, Files, RuntimeTurns } from '../imports/coreCollections.js';

// Server-only config. In production the supervisor injects env vars;
// Meteor.settings is the DEV fallback. The service token must NEVER live in
// anything client-reachable — env injection keeps it out of every bundle.
const CHAT_URL = process.env.TG_CHAT_URL || (Meteor.settings && Meteor.settings.private && Meteor.settings.private.chatUrl) || 'http://127.0.0.1:8000';
const SERVICE_TOKEN = process.env.TG_SERVICE_TOKEN || (Meteor.settings && Meteor.settings.private && Meteor.settings.private.serviceToken) || 'dev-service-token';
const PROXY_URL = process.env.TG_PROXY_URL || (Meteor.settings && Meteor.settings.private && Meteor.settings.proxyUrl) || 'http://127.0.0.1:8001';
const MAIL_URL = process.env.TG_MAIL_URL || (Meteor.settings && Meteor.settings.private && Meteor.settings.mailUrl) || 'http://127.0.0.1:8002';
const RUNTIME_URL = process.env.TG_RUNTIME_URL || (Meteor.settings && Meteor.settings.private && Meteor.settings.private.runtimeUrl) || 'http://127.0.0.1:8010';

// --- helpers (Meteor 3: async collection access on the server) -----------

// Self-heal: if users exist but NONE is admin (partial bootstrap from a
// broken deploy era), the OLDEST user becomes admin on next login screen
// load. First user is admin by definition.
async function ensureAtLeastOneAdmin() {
  try {
    const admins = await Meteor.users.find({ isAdmin: true }).countAsync();
    if (admins > 0) return;
    const first = await Meteor.users.findOneAsync({}, { sort: { createdAt: 1 } });
    if (first) {
      await Meteor.users.updateAsync(first._id, { $set: { isAdmin: true } });
      console.log('[self-heal] no admin found — promoted first user:', first.username);
    }
  } catch (e) {
    console.error('[self-heal] failed:', e.message);
  }
}
Meteor.startup(() => { ensureAtLeastOneAdmin(); });

// Security: ALL account creation flows through server methods (bootstrap +
// admin-only tg.createActor). Kill the default client-callable createUser.
Accounts.config({ forbidClientAccountCreation: true });

async function myActor(user) {
  if (!user) return null;
  return await Actors.findOneAsync({ username: user.username });
}

async function api(path, method, actorId, body) {
  const res = await fetch(`${CHAT_URL}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      'X-Service-Token': SERVICE_TOKEN,
      'X-Actor-Id': actorId,
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data);
    throw new Meteor.Error('chat-api', `${res.status}: ${detail}`);
  }
  return data;
}

// R62: ONE admin guard for every fleet RPC. A hidden admin pane is not an
// authorization boundary — deny BEFORE any runtime fetch, mirroring the
// tg.setAgentHost / tg.issueAgentKey check.
async function requireAdmin() {
  const caller = await Meteor.userAsync();
  if (!caller || !caller.isAdmin) throw new Meteor.Error('forbidden', 'admin only');
  return caller;
}

// R62: the mail service trusts X-Actor-Id from the central tier, so mail.api
// must only ever declare an identity it has authorized. These are the routes
// the web tier forwards — nothing else (internal hooks, adapters, …).
const MAIL_ROUTES = [
  ['GET', /^\/mailboxes$/],
  ['GET', /^\/mailboxes\/[^/?]+\/messages$/],
  ['POST', /^\/send$/],
  ['GET', /^\/approvals$/],
  ['POST', /^\/approvals\/[^/?]+\/decide$/],
];

// --- R62: resource-scoped publications ----------------------------------
// REST-side checks cannot protect data published over DDP, so every
// publication enforces its own resource scope. Admin/root sees the company;
// anyone else sees SELF plus their org subtree (R61 reach-down, same
// org.ancestors test as secretsMeta). The scope is REACTIVE: a shared
// Actors/Mailboxes watcher re-runs every subscriber's scope recompute when
// the org tree or mailbox membership changes and RE-SETS the cursor, so
// moving an actor (revoking reach-down) withdraws rows from EXISTING
// subscriptions. (Meteor 3 removed Subscription#autorun; manual
// added/changed/removed over a re-armed observeChanges is the supported
// publication shape for a selector that itself must change.)

const scopeSubs = new Set();
let scopeWatchers = null;

function fireScopeDeps() {
  for (const fn of [...scopeSubs]) fn();
}

function startScopeWatchers() {
  if (scopeWatchers) return;
  const opts = { nonMutatingCallbacks: true };
  const ping = () => fireScopeDeps();
  scopeWatchers = Promise.resolve()
    .then(() => Actors.find({}, { fields: { org: 1 } })
      .observeChanges({ added: ping, changed: ping, removed: ping }, opts))
    .then(() => Mailboxes.find({}, { fields: { owner: 1, members: 1 } })
      .observeChanges({ added: ping, changed: ping, removed: ping }, opts))
    .catch((e) => { console.error('[scope-watch]', e.message); });
}

// ids a non-admin may see: self + whole org subtree (org.ancestors is the
// stored root..parent path, R21)
async function scopeIds(meId) {
  const rows = await Actors.rawCollection()
    .find({}, { projection: { _id: 1, 'org.ancestors': 1 } }).toArray();
  const ids = [meId];
  for (const a of rows) {
    if (a._id === meId) continue;
    if (((a.org && a.org.ancestors) || []).includes(meId)) ids.push(a._id);
  }
  return ids;
}

// Publishes coll rows matching (async) selectorFor(scopeIds): live within the
// scope, withdrawn when the scope shrinks.
function publishScoped(pub, coll, collName, meId, selectorFor, opts) {
  startScopeWatchers();
  const published = new Map();   // _id -> fields last sent
  let obs = null;
  let seq = 0;
  let first = true;
  const markReady = () => {
    if (!first) return;
    first = false;
    try { pub.ready(); } catch (e) { /* sub already stopped */ }
  };
  const withdraw = (id) => {
    if (!published.has(id)) return;
    published.delete(id);
    try { pub.removed(collName, id); } catch (e) { /* sub already stopped */ }
  };
  const handlers = {
    added(id, fields) {
      if (published.has(id)) {
        published.set(id, { ...published.get(id), ...fields });
        try { pub.changed(collName, id, fields); } catch (e) { /* stopped */ }
      } else {
        published.set(id, { ...fields });
        try { pub.added(collName, id, fields); } catch (e) { /* stopped */ }
      }
    },
    changed(id, fields) {
      if (!published.has(id)) return;
      published.set(id, { ...published.get(id), ...fields });
      try { pub.changed(collName, id, fields); } catch (e) { /* stopped */ }
    },
    removed(id) { withdraw(id); },
  };
  const rescope = async () => {
    const mySeq = ++seq;
    try {
      const ids = await scopeIds(meId);
      if (mySeq !== seq) return;              // superseded by a newer scope
      const selector = await selectorFor(ids);
      if (mySeq !== seq) return;
      const cursor = coll.find(selector, opts || {});
      const nextObs = await cursor.observeChanges(handlers, { nonMutatingCallbacks: true });
      const snapshot = await cursor.fetchAsync();
      if (mySeq !== seq) { await nextObs.stop(); return; }
      // rows that fell out of scope are withdrawn here — the new observation
      // never saw them, so no removed callback will come for them
      const keep = new Set(snapshot.map((d) => d._id));
      for (const id of [...published.keys()]) if (!keep.has(id)) withdraw(id);
      const prev = obs;
      obs = nextObs;
      if (prev) await prev.stop();
      markReady();
    } catch (e) {
      console.error(`[scoped-publish ${collName}]`, e.message);
      markReady();
    }
  };
  const trigger = () => { rescope(); };
  scopeSubs.add(trigger);
  pub.onStop(() => {
    scopeSubs.delete(trigger);
    const handle = obs;
    obs = null;
    if (handle) Promise.resolve(handle.stop()).catch(() => {});
  });
  trigger();
}

// --- publications ------------------------------------------------------

// auto-subscribed: publish our custom fields for the logged-in user
Meteor.publish(null, function () {
  if (!this.userId) return this.ready();
  return Meteor.users.find({ _id: this.userId },
    { fields: { isAdmin: 1, display: 1, username: 1 } });
});

Meteor.publish('actors', function () {
  if (!this.userId) return this.ready();
  return Actors.find();
});

Meteor.publish('conversations', async function () {
  if (!this.userId) return this.ready();
  const user = await Meteor.users.findOneAsync(this.userId);
  const actor = await myActor(user);
  if (!actor) return this.ready();
  return Conversations.find({ members: actor._id });
});

Meteor.publish('messages', async function (conversationId, limit) {
  check(conversationId, String);
  check(limit, Match.Integer);
  if (!this.userId) return this.ready();
  const user = await Meteor.users.findOneAsync(this.userId);
  const actor = await myActor(user);
  if (!actor) return this.ready();
  const conv = await Conversations.findOneAsync(
    { _id: conversationId, members: actor._id });
  if (!conv) return this.ready();
  return Messages.find({ conversation_id: conversationId },
    { sort: { _id: -1 }, limit: Math.min(limit || 200, 500) });
});

Meteor.publish('allUsernames', function () {
  if (!this.userId) return this.ready();
  return Meteor.users.find({}, { fields: { username: 1 } });
});

// --- proxy dashboards (reads reactive off the shared DB) ---------------

Meteor.publish('proxyClasses', function () {
  if (!this.userId) return this.ready();
  return TaskClasses.find();
});

Meteor.publish('proxyProviders', function () {
  if (!this.userId) return this.ready();
  // never publish anything secret; provider rows only carry key_env NAMES
  return Providers.find({}, { fields: { kind: 1, base_url: 1, key_env: 1, enabled: 1 } });
});

Meteor.publish('proxyCatalog', function (limit) {
  if (!this.userId) return this.ready();
  return ModelCatalog.find({ listed: true },
    { sort: { rank: -1 }, limit: Math.min(limit || 400, 800),
      fields: { provider: 1, designations: 1, rank: 1, modalities: 1,
                excluded: 1, ctx: 1 } });
});

Meteor.publish('agentTranscripts', async function (agentId, limit) {
  check(agentId, String);
  if (!this.userId) return this.ready();
  const u = await Meteor.users.findOneAsync(this.userId);
  if (!u || !u.isAdmin) return this.ready();
  return AgentTranscripts.find({ agent_id: agentId },
    { sort: { ts_received: 1 }, limit: limit || 500,
      fields: { agent_id: 1, ts: 1, role: 1, content: 1, meta: 1 } });
});

Meteor.publish('proxyUsage', async function (days) {
  if (!this.userId) return this.ready();
  const cutoff = new Date(Date.now() - (days || 7) * 86400 * 1000);
  const fields = { agent_id: 1, tokens_in: 1, tokens_out: 1,
                   status: 1, model: 1, provider: 1, ts: 1, class: 1,
                   error: 1, reason: 1, queue_wait_s: 1, job_id: 1 };
  const user = await Meteor.users.findOneAsync(this.userId);
  if (user && user.isAdmin) {
    return UsageEvents.find({ ts: { $gte: cutoff } }, { fields });
  }
  const me = await myActor(user);
  if (!me) return this.ready();
  // R62: own + subtree usage only (was: company-wide)
  publishScoped(this, UsageEvents, 'usage_events', me._id,
    (ids) => ({ ts: { $gte: cutoff }, agent_id: { $in: ids } }), { fields });
});

// --- mail + approvals (M3) ----------------------------------------------

Meteor.publish('mailboxesData', async function () {
  if (!this.userId) return this.ready();
  const user = await Meteor.users.findOneAsync(this.userId);
  if (user && user.isAdmin) return Mailboxes.find();
  const me = await myActor(user);
  if (!me) return this.ready();
  // R62: own + subtree mailboxes only (was: every mailbox row)
  publishScoped(this, Mailboxes, 'mailboxes', me._id,
    (ids) => ({ $or: [{ owner: { $in: ids } }, { members: { $in: ids } }] }));
});

Meteor.publish('mailMessages', async function (mailboxId) {
  if (!this.userId || !mailboxId) return this.ready();
  const user = await Meteor.users.findOneAsync(this.userId);
  if (user && user.isAdmin) {
    return MailMessages.find({ mailbox_id: mailboxId },
      { sort: { ts: -1 }, limit: 100 });
  }
  const me = await myActor(user);
  if (!me) return this.ready();
  // R62: only from a mailbox inside my scope (owner/member/subtree)
  publishScoped(this, MailMessages, 'mail_messages', me._id,
    async (ids) => {
      const mb = await Mailboxes.findOneAsync({ _id: mailboxId });
      const visible = mb
        && ((mb.owner && ids.includes(mb.owner))
            || (mb.members || []).some((m) => ids.includes(m)));
      return visible ? { mailbox_id: mailboxId } : { _id: '__r62_no_access__' };
    },
    { sort: { ts: -1 }, limit: 100 });
});

Meteor.publish('approvalsData', async function () {
  if (!this.userId) return this.ready();
  const user = await Meteor.users.findOneAsync(this.userId);
  if (user && user.isAdmin) {
    return Approvals.find({}, { sort: { created_at: -1 }, limit: 200 });
  }
  const me = await myActor(user);
  if (!me) return this.ready();
  // R62: own + subtree approvals only (was: company-wide incl. payloads)
  publishScoped(this, Approvals, 'approvals', me._id,
    (ids) => ({ $or: [{ approver_id: { $in: ids } },
                      { requester_id: { $in: ids } }] }),
    { sort: { created_at: -1 }, limit: 200 });
});

// --- live metadata for Secrets / Files / Agents panes -------------------

Meteor.publish('secretsMeta', async function () {
  if (!this.userId) return this.ready();
  const user = await Meteor.users.findOneAsync(this.userId);
  const me = await myActor(user);
  if (!me) return this.ready();
  // R40 own-scope: own + shared-with-me. R61: superiors ALSO receive their
  // whole subtree's secrets (metadata only) so the reach-down picker filters
  // live on the client — the service API still enforces per-request auth.
  const selector = { $or: [{ owner: me._id }, { shared_with: me._id }] };
  const actorsRaw = await Actors.rawCollection().find(
    {}, { projection: { _id: 1, org: 1, kind: 1 } }).toArray();
  const subtree = actorsRaw
    .filter((a) => (a.org && a.org.ancestors || []).includes(me._id))
    .map((a) => a._id);
  if (subtree.length) {
    selector.$or.push({ owner: { $in: subtree } });
  }
  return Secrets.find(
    selector,
    { fields: { value_enc: 0 } });   // ciphertext never leaves the server
});

Meteor.publish('filesMeta', async function () {
  if (!this.userId) return this.ready();
  const user = await Meteor.users.findOneAsync(this.userId);
  const me = await myActor(user);
  if (!me) return this.ready();
  return Files.find(
    { $or: [{ owner: me._id }, { shared_with: me._id }] });
});

Meteor.publish('agentTurns', async function (agentId, limit) {
  if (!this.userId || !agentId) return this.ready();
  const user = await Meteor.users.findOneAsync(this.userId);
  if (user && user.isAdmin) {
    return RuntimeTurns.find({ agent_id: agentId },
      { sort: { started: -1 }, limit: limit || 50 });
  }
  const me = await myActor(user);
  if (!me) return this.ready();
  // R62: only own/subtree agents' turns (was: any requested agent)
  publishScoped(this, RuntimeTurns, 'runtime_turns', me._id,
    (ids) => (ids.includes(agentId)
      ? { agent_id: agentId } : { _id: '__r62_no_access__' }),
    { sort: { started: -1 }, limit: limit || 50 });
});

// --- accounts bootstrap (R28: admin-provisioned) -----------------------

Meteor.methods({


  async 'tg.userCount'() {
    return await Meteor.users.find().countAsync();
  },

  async 'tg.bootstrapAdmin'(username, password, display) {
    check(username, String); check(password, String); check(display, String);
    if (await Meteor.users.find().countAsync() > 0) {
      throw new Meteor.Error('forbidden', 'bootstrap closed: users already exist');
    }
    const uid = Accounts.createUser({ username, password });
    await Meteor.users.updateAsync(uid, { $set: { isAdmin: true, display } });
    // first actor is created through the chat service's bootstrap pseudo-actor
    await api('/actors', 'POST', 'boot', {
      username, display_name: display, kind: 'human', parent_id: null,
    });
    return true;
  },

  async 'tg.createActor'(username, password, display, kind, parentId, hostId) {
    check(username, String); check(password, String); check(display, String);
    check(kind, String);
    if (hostId !== undefined && hostId !== null) check(hostId, String);
    const caller = await Meteor.userAsync();
    if (!caller || !caller.isAdmin) throw new Meteor.Error('forbidden', 'admin only');
    // idempotent: reuse an existing Meteor user if the actor is missing
    let uid;
    const existing = await Meteor.users.findOneAsync({ username });
    if (existing) {
      uid = existing._id;
      await Meteor.users.updateAsync(uid, { $set: { display } });
    } else {
      uid = Accounts.createUser({ username, password });
      await Meteor.users.updateAsync(uid, { $set: { display } });
    }
    const me = await myActor(caller);
    if (!me) {
      throw new Meteor.Error('no-actor', 'caller has no actor record');
    }
    const body = {
      username, display_name: display, kind, parent_id: parentId || null,
    };
    if (hostId) body.host_id = hostId;   // R53: run on a specific agent host
    await api('/actors', 'POST', me._id, body);
    return true;
  },

  // ---- agent hosts (R53) ----
  async 'tg.setAgentHost'(agentId, hostId) {
    check(agentId, String);
    const caller = await Meteor.userAsync();
    if (!caller || !caller.isAdmin) throw new Meteor.Error('forbidden', 'admin only');
    const target = hostId === '' ? null : hostId;  // '' = central option -> null
    const actor = await myActor(caller);   // central tier needs X-Actor-Id (R45)
    const res = await fetch(`${CHAT_URL}/internal/agents/${agentId}/host`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json',
                 'X-Service-Token': SERVICE_TOKEN, 'X-Actor-Id': actor._id },
      body: JSON.stringify({ host_id: target }),
    });
    if (!res.ok) throw new Meteor.Error(res.status, await res.text());
    return { ok: true };
  },

  async 'tg.hosts.list'() {
    await requireAdmin();
    const r = await fetch(`${RUNTIME_URL}/internal/hosts`, {
      headers: { 'X-Service-Token': SERVICE_TOKEN } });
    return r.json();
  },
  async 'tg.hosts.pubkey'(hostId) {
    check(hostId, String);
    await requireAdmin();
    const r = await fetch(`${RUNTIME_URL}/internal/hosts/${hostId}/pubkey`, {
      headers: { 'X-Service-Token': SERVICE_TOKEN } });
    if (!r.ok) throw new Meteor.Error('hosts', `${r.status}`);
    return r.json();
  },
  async 'tg.hosts.check'(hostId) {
    check(hostId, String);
    await requireAdmin();
    const r = await fetch(`${RUNTIME_URL}/internal/hosts/${hostId}/check`, {
      method: 'POST', headers: { 'X-Service-Token': SERVICE_TOKEN } });
    if (!r.ok) throw new Meteor.Error('hosts', `${r.status}`);
    return r.json();
  },
  async 'tg.hosts.update'(hostId) {
    check(hostId, String);
    await requireAdmin();
    const r = await fetch(`${RUNTIME_URL}/internal/hosts/${hostId}/update`, {
      method: 'POST', headers: { 'X-Service-Token': SERVICE_TOKEN } });
    if (!r.ok) throw new Meteor.Error('hosts', `${r.status}`);
    return r.json();
  },
  async 'tg.hosts.remove'(hostId) {
    check(hostId, String);
    await requireAdmin();
    const r = await fetch(`${RUNTIME_URL}/internal/hosts/${hostId}`, {
      method: 'DELETE',
      headers: { 'X-Service-Token': SERVICE_TOKEN } });
    if (!r.ok) throw new Meteor.Error('hosts', `${r.status}`);
    return r.json();
  },
  async 'tg.hosts.add'(name, address, port, sshUser) {
    check(name, String); check(address, String); check(port, Number); check(sshUser, String);
    await requireAdmin();
    const r = await fetch(`${RUNTIME_URL}/internal/hosts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Service-Token': SERVICE_TOKEN },
      body: JSON.stringify({ name, address, port, ssh_user: sshUser }) });
    if (!r.ok) throw new Meteor.Error('hosts', `${r.status}`);
    return r.json();
  },
  async 'tg.hosts.provision'(hostId) {
    check(hostId, String);
    await requireAdmin();
    const r = await fetch(`${RUNTIME_URL}/internal/hosts/${hostId}/provision`, {
      method: 'POST',
      headers: { 'X-Service-Token': SERVICE_TOKEN } });
    return r.json();
  },

  async 'tg.issueAgentKey'(agentId) {
    check(agentId, String);
    const caller = await Meteor.userAsync();
    if (!caller || !caller.isAdmin) throw new Meteor.Error('forbidden', 'admin only');
    const me = await myActor(caller);
    if (!me) throw new Meteor.Error('no-actor', 'caller has no actor record');
    const r = await api(`/internal/agents/${agentId}/keys`, 'POST', me._id);
    return r.key;   // shown ONCE in the UI, never stored client-side
  },

  async 'tg.setParent'(actorId, parentId) {
    check(actorId, String);
    check(parentId === undefined ? null : parentId, Match.OneOf(String, null));
    const caller = await Meteor.userAsync();
    if (!caller || !caller.isAdmin) throw new Meteor.Error('forbidden', 'admin only');
    const me = await myActor(caller);
    return await api(`/actors/${actorId}/parent`, 'PUT', me._id,
      { parent_id: parentId || null });
  },

  async 'tg.openDM'(otherActorId) {
    check(otherActorId, String);
    const me = await myActor(await Meteor.userAsync());
    if (!me) throw new Meteor.Error('no-actor', 'user has no actor record');
    return await api(`/conversations/dm/${otherActorId}`, 'POST', me._id);
  },

  async 'tg.createChannel'(name) {
    check(name, String);
    const me = await myActor(await Meteor.userAsync());
    if (!me) throw new Meteor.Error('no-actor', 'user has no actor record');
    // M1 demo shortcut: company-wide channel (every current actor is a member)
    const everyone = (await Actors.find().fetchAsync()).map((a) => a._id);
    return await api('/conversations', 'POST', me._id, {
      kind: 'channel', name, member_ids: everyone,
    });
  },

  async 'tg.sendMessage'(conversationId, body) {
    check(conversationId, String); check(body, String);
    const me = await myActor(await Meteor.userAsync());
    if (!me) throw new Meteor.Error('no-actor', 'user has no actor record');
    return await api(`/conversations/${conversationId}/messages`, 'POST', me._id, { body });
  },

  // --- proxy admin (writes via proxy admin API; single writer) ----------

  async 'proxy.admin'(path, method, body) {
    check(path, String); check(method, String);
    const caller = await Meteor.userAsync();
    if (!caller || !caller.isAdmin) throw new Meteor.Error('forbidden', 'admin only');
    const actor = ((await myActor(caller)) || {})._id || '';
    const res = await fetch(`${PROXY_URL}${path}`, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Service-Token': SERVICE_TOKEN, 'X-Actor-Id': actor },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Meteor.Error('proxy-api', `${res.status}: ${
        typeof data.detail === 'string' ? data.detail : JSON.stringify(data)}`);
    }
    return data;
  },

  async 'mail.api'(path, method, body, actorId) {
    check(path, String); check(method, String);
    const caller = await Meteor.userAsync();
    if (!caller) throw new Meteor.Error('forbidden', 'login required');
    // fail closed: no actor mapping, no forwarding
    const me = await myActor(caller);
    if (!me) throw new Meteor.Error('no-actor', 'user has no actor record');
    // R62: forward only the intended mail API contract
    const pathname = path.split('?')[0];
    const routeOk = MAIL_ROUTES.some(([m, re]) => m === method && re.test(pathname));
    if (!routeOk) throw new Meteor.Error('forbidden', 'unsupported mail route');
    // R62: the forwarded identity is ALWAYS the caller's own actor. A supplied
    // actorId is honored only as READ-ONLY reach-down into my own org subtree
    // (R61 parity with secrets.api) — never on writes or approval decisions,
    // which stay bound to the real caller. Peers/strangers 403.
    let actor = me._id;
    if (actorId && actorId !== me._id) {
      if (method !== 'GET') {
        throw new Meteor.Error('forbidden',
          'writes/decisions cannot impersonate another actor');
      }
      const target = await Actors.findOneAsync({ _id: actorId });
      const anc = (target && target.org && target.org.ancestors) || [];
      if (!caller.isAdmin && !anc.includes(me._id)) {
        throw new Meteor.Error('forbidden', 'reach-down requires superior position');
      }
      actor = actorId;
    }
    const res = await fetch(`${MAIL_URL}${path}`, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Service-Token': SERVICE_TOKEN, 'X-Actor-Id': actor },
      body: body && method !== 'GET' ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Meteor.Error('mail-api', `${res.status}: ${
        typeof data.detail === 'string' ? data.detail : JSON.stringify(data)}`);
    }
    return data;
  },

  async 'secrets.api'(path, method, body, asActorId) {
    check(path, String); check(method, String);
    const caller = await Meteor.userAsync();
    if (!caller) throw new Meteor.Error('forbidden', 'login required');
    const SECRETS_URL = (Meteor.settings && Meteor.settings.private && Meteor.settings.private.secretsUrl) || 'http://127.0.0.1:8003';
    let url = `${SECRETS_URL}${path}`;
    const myId = (await myActor(caller) || {})._id || '';
    let actor = myId;
    if (asActorId && asActorId !== myId) {
      // reach-down: ONLY allowed if I am above the target in the org
      const target = await Actors.findOneAsync({ _id: asActorId });
      const anc = (target && target.org && target.org.ancestors) || [];
      if (!anc.includes(myId)) {
        throw new Meteor.Error('forbidden', 'reach-down requires superior position');
      }
      actor = asActorId;
    }
    url += (path.includes('?') ? '&' : '?') + `caller_id=${encodeURIComponent(actor)}`;
    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Service-Token': SERVICE_TOKEN,
                 'X-Actor-Id': actor },
      body: body && method !== 'GET' ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Meteor.Error('secrets-api', `${res.status}: ${
        typeof data.detail === 'string' ? data.detail : JSON.stringify(data)}`);
    }
    return data;
  },

  async 'runtime.api'(path, method, body) {
    check(path, String); check(method, String);
    const caller = await Meteor.userAsync();
    if (!caller || !caller.isAdmin) {
      throw new Meteor.Error('forbidden', 'admin only');
    }
    const RUNTIME_URL = (Meteor.settings && Meteor.settings.private && Meteor.settings.private.runtimeUrl) || 'http://127.0.0.1:8010';
    const res = await fetch(`${RUNTIME_URL}${path}`, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Service-Token': SERVICE_TOKEN, 'X-Actor-Id': actor },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Meteor.Error('runtime-api', `${res.status}: ${JSON.stringify(data)}`);
    }
    return data;
  },

  async 'files.api'(path, method, body, asActorId) {
    check(path, String); check(method, String);
    const caller = await Meteor.userAsync();
    if (!caller) throw new Meteor.Error('forbidden', 'login required');
    const FILES_URL = (Meteor.settings && Meteor.settings.private && Meteor.settings.private.filesUrl) || 'http://127.0.0.1:8004';
    let url = `${FILES_URL}${path}`;
    const myId = (await myActor(caller) || {})._id || '';
    let actor = myId;
    if (asActorId && asActorId !== myId) {
      const target = await Actors.findOneAsync({ _id: asActorId });
      const anc = (target && target.org && target.org.ancestors) || [];
      if (!anc.includes(myId)) {
        throw new Meteor.Error('forbidden', 'reach-down requires superior position');
      }
      actor = asActorId;
    }
    if (method === 'UPLOAD') {
      // File objects can't cross the Meteor method boundary — arrive as
      // {name, type, b64}; rebuild a Blob and multipart it server-side.
      const { name, type, b64 } = body;
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      const fd = new FormData();
      fd.append('file', new Blob([bytes], { type: type || 'application/octet-stream' }), name);
      const res = await fetch(`${FILES_URL}/files${url.includes('?') ? '&' : '?'}caller_id=${encodeURIComponent(actor)}`, {
        method: 'POST',
        headers: { 'X-Service-Token': SERVICE_TOKEN },
        body: fd,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Meteor.Error('files-api', `${res.status}: ${JSON.stringify(data)}`);
      return data;
    }
    url += (path.includes('?') ? '&' : '?') + `caller_id=${encodeURIComponent(actor)}`;
    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Service-Token': SERVICE_TOKEN, 'X-Actor-Id': actor },
      body: body && method !== 'GET' ? JSON.stringify(body) : undefined,
    });
    if (path.includes('/download')) {
      if (!res.ok) throw new Meteor.Error('files-api', `${res.status}`);
      return await res.text();
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Meteor.Error('files-api', `${res.status}: ${
        typeof data.detail === 'string' ? data.detail : JSON.stringify(data)}`);
    }
    return data;
  },
});
