import { Meteor } from 'meteor/meteor';
import { Accounts } from 'meteor/accounts-base';
import { Actors, Conversations, Messages } from '../imports/collections.js';

const CHAT_URL = (Meteor.settings && Meteor.settings.private && Meteor.settings.private.chatUrl) || 'http://127.0.0.1:8000';
const SERVICE_TOKEN = (Meteor.settings && Meteor.settings.private && Meteor.settings.private.serviceToken) || 'dev-service-token';

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
});
