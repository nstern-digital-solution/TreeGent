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

Meteor.publish('agentTranscripts', function (agentId, limit) {
  if (!this.userId) return this.ready();
  const user = Meteor.users.findOneAsync ? null : null;
  // admin-only enforced below via user lookup
  const u = Meteor.users.findOne(this.userId);
  if (!u || !u.isAdmin) return this.ready();
  return AgentTranscripts.find({ agent_id: agentId },
    { sort: { ts_received: 1 }, limit: limit || 500,
      fields: { agent_id: 1, ts: 1, role: 1, content: 1, meta: 1 } });
});

Meteor.publish('proxyUsage', function (days) {
  if (!this.userId) return this.ready();
  const cutoff = new Date(Date.now() - (days || 7) * 86400 * 1000);
  return UsageEvents.find({ ts: { $gte: cutoff } },
    { fields: { agent_id: 1, tokens_in: 1, tokens_out: 1,
                status: 1, model: 1, provider: 1, ts: 1, class: 1,
                error: 1, reason: 1, queue_wait_s: 1, job_id: 1 } });
});

// --- mail + approvals (M3) ----------------------------------------------

Meteor.publish('mailboxesData', function () {
  if (!this.userId) return this.ready();
  return Mailboxes.find();
});

Meteor.publish('mailMessages', function (mailboxId) {
  if (!this.userId || !mailboxId) return this.ready();
  return MailMessages.find({ mailbox_id: mailboxId },
    { sort: { ts: -1 }, limit: 100 });
});

Meteor.publish('approvalsData', function () {
  if (!this.userId) return this.ready();
  // humans see all approvals (they are the approvers/admins in v1)
  return Approvals.find({}, { sort: { created_at: -1 }, limit: 200 });
});

// --- live metadata for Secrets / Files / Agents panes -------------------

Meteor.publish('secretsMeta', async function () {
  if (!this.userId) return this.ready();
  const user = await Meteor.users.findOneAsync(this.userId);
  const me = await myActor(user);
  if (!me) return this.ready();
  // R40 own-scope: own + shared-with-me. Superiors do NOT see subtree here.
  return Secrets.find(
    { $or: [{ owner: me._id }, { shared_with: me._id }] },
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

Meteor.publish('agentTurns', function (agentId, limit) {
  if (!this.userId || !agentId) return this.ready();
  return RuntimeTurns.find({ agent_id: agentId },
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
  async 'runtime.session'(agentId, what, opts) {
    check(agentId, String);
    check(what, String);
    check(opts, Match.Maybe(Object));
    const caller = await Meteor.userAsync();
    if (!caller || !caller.isAdmin) throw new Meteor.Error('forbidden', 'admin only');
    if (what === 'transcript') {
      const docs = await AgentTranscripts.rawCollection().find(
        { agent_id: agentId },
        { sort: { ts_received: 1 }, limit: (opts && opts.limit) || 300 },
      ).toArray();
      return { lines: docs };
    }
    if (what === 'jobs') {
      const docs = await Jobs.rawCollection().find(
        {},
        { sort: { created_at: -1 }, limit: (opts && opts.limit) || 100 },
      ).projection({ messages: 0, tools: 0 }).toArray().catch(async () => {
        // projection-after-sort fallback for older drivers
        const all = await Jobs.rawCollection().find({}).sort({ created_at: -1 })
          .limit((opts && opts.limit) || 100).toArray();
        return all.map((j) => { delete j.messages; delete j.tools; return j; });
      });
      return { jobs: docs.filter((j) => j.agent_id === agentId) };
    }
    throw new Meteor.Error('bad-request', 'unknown what');
  },

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
    const r = await fetch(`${RUNTIME_URL}/internal/hosts`, {
      headers: { 'X-Service-Token': SERVICE_TOKEN } });
    return r.json();
  },
  async 'tg.hosts.pubkey'(hostId) {
    check(hostId, String);
    const r = await fetch(`${RUNTIME_URL}/internal/hosts/${hostId}/pubkey`, {
      headers: { 'X-Service-Token': SERVICE_TOKEN } });
    if (!r.ok) throw new Meteor.Error('hosts', `${r.status}`);
    return r.json();
  },
  async 'tg.hosts.check'(hostId) {
    check(hostId, String);
    const r = await fetch(`${RUNTIME_URL}/internal/hosts/${hostId}/check`, {
      method: 'POST', headers: { 'X-Service-Token': SERVICE_TOKEN } });
    if (!r.ok) throw new Meteor.Error('hosts', `${r.status}`);
    return r.json();
  },
  async 'tg.hosts.update'(hostId) {
    check(hostId, String);
    const r = await fetch(`${RUNTIME_URL}/internal/hosts/${hostId}/update`, {
      method: 'POST', headers: { 'X-Service-Token': SERVICE_TOKEN } });
    if (!r.ok) throw new Meteor.Error('hosts', `${r.status}`);
    return r.json();
  },
  async 'tg.hosts.remove'(hostId) {
    check(hostId, String);
    const r = await fetch(`${RUNTIME_URL}/internal/hosts/${hostId}`, {
      method: 'DELETE',
      headers: { 'X-Service-Token': SERVICE_TOKEN } });
    if (!r.ok) throw new Meteor.Error('hosts', `${r.status}`);
    return r.json();
  },
  async 'tg.hosts.add'(name, address, port, sshUser) {
    check(name, String); check(address, String); check(port, Number); check(sshUser, String);
    const r = await fetch(`${RUNTIME_URL}/internal/hosts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Service-Token': SERVICE_TOKEN },
      body: JSON.stringify({ name, address, port, ssh_user: sshUser }) });
    if (!r.ok) throw new Meteor.Error('hosts', `${r.status}`);
    return r.json();
  },
  async 'tg.hosts.provision'(hostId) {
    check(hostId, String);
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
    const actor = actorId || ((await myActor(caller)) || {})._id || '';
    let url = `${MAIL_URL}${path}`;
    if (actorId) url += (path.includes('?') ? '&' : '?') + `actor_id=${encodeURIComponent(actorId)}`;
    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Service-Token': SERVICE_TOKEN, 'X-Actor-Id': actor },
      body: body ? JSON.stringify(body) : undefined,
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
