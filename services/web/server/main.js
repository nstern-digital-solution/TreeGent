import { Meteor } from 'meteor/meteor';
import { Accounts } from 'meteor/accounts-base';
import { Actors, Conversations, Messages } from '../imports/collections.js';
import { TaskClasses, Providers, ModelCatalog, UsageEvents } from '../imports/proxyCollections.js';
import { Mailboxes, MailMessages, Approvals } from '../imports/mailCollections.js';

const CHAT_URL = (Meteor.settings && Meteor.settings.private && Meteor.settings.private.chatUrl) || 'http://127.0.0.1:8000';
const SERVICE_TOKEN = (Meteor.settings && Meteor.settings.private && Meteor.settings.private.serviceToken) || 'dev-service-token';
const PROXY_URL = (Meteor.settings && Meteor.settings.private && Meteor.settings.proxyUrl) || 'http://127.0.0.1:8001';
const MAIL_URL = (Meteor.settings && Meteor.settings.private && Meteor.settings.mailUrl) || 'http://127.0.0.1:8002';

// --- helpers (Meteor 3: async collection access on the server) -----------

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

Meteor.publish('proxyUsage', function (days) {
  if (!this.userId) return this.ready();
  const cutoff = new Date(Date.now() - (days || 7) * 86400 * 1000);
  return UsageEvents.find({ ts: { $gte: cutoff } },
    { fields: { agent_id: 1, tokens_in: 1, tokens_out: 1,
                status: 1, model: 1, provider: 1, ts: 1, class: 1 } });
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

  async 'tg.createActor'(username, password, display, kind, parentId) {
    check(username, String); check(password, String); check(display, String);
    check(kind, String);
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
    await api('/actors', 'POST', me._id, {
      username, display_name: display, kind, parent_id: parentId || null,
    });
    return true;
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
    const res = await fetch(`${PROXY_URL}${path}`, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Service-Token': SERVICE_TOKEN },
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
    let url = `${MAIL_URL}${path}`;
    if (actorId) url += (path.includes('?') ? '&' : '?') + `actor_id=${encodeURIComponent(actorId)}`;
    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Service-Token': SERVICE_TOKEN },
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
      headers: { 'Content-Type': 'application/json', 'X-Service-Token': SERVICE_TOKEN },
      body: body && method !== 'GET' ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Meteor.Error('secrets-api', `${res.status}: ${
        typeof data.detail === 'string' ? data.detail : JSON.stringify(data)}`);
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
      headers: { 'Content-Type': 'application/json', 'X-Service-Token': SERVICE_TOKEN },
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
