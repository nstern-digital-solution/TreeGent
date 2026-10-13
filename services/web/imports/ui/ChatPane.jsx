import React, { useState } from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { Actors, Conversations, Messages } from '../collections.js';
import { Markdown } from './Markdown.jsx';

export function ChatPane({ me }) {
  const [activeId, setActiveId] = useState(null);
  const [error, setError] = useState('');
  const [showNew, setShowNew] = useState(false);

  const { convs, actorsById, ready } = useTracker(() => {
    const s1 = Meteor.subscribe('conversations');
    const s2 = Meteor.subscribe('actors');
    const convs = me ? Conversations.find({}, { sort: { kind: 1, name: 1 } }).fetch() : [];
    const actorsById = {};
    Actors.find({}).fetch().forEach((a) => { actorsById[a._id] = a; });
    return { convs, actorsById, ready: s1.ready() && s2.ready() };
  }, [me && me._id]);

  const label = (c) => {
    if (c.kind === 'dm') {
      const other = (c.members || []).find((m) => m !== (me && me._id));
      const a = actorsById[other];
      return a ? a.display_name : 'DM';
    }
    return `# ${c.name}`;
  };

  const active = convs.find((c) => c._id === activeId) || null;

  return (
    <div className="layout">
      <aside className="sidebar">
        <h3>Conversations</h3>
        <ul>
          {convs.map((c) => (
            <li key={c._id}
              className={`conv-row ${c._id === activeId ? 'selected' : ''}`}
              onClick={() => setActiveId(c._id)}>
              {label(c)}
            </li>
          ))}
          {ready && convs.length === 0 && <li className="muted">No conversations yet — open one from the Roster or create a group</li>}
        </ul>
        <button className="new-group-btn" onClick={() => setShowNew(!showNew)}>+ New group</button>
        {showNew && <NewGroupForm me={me} actorsById={actorsById} onDone={(c) => { setShowNew(false); if (c) setActiveId(c.id); }} onError={setError} />}
      </aside>
      <main className="chatpane">
        {active ? <MessageList conv={active} me={me} actorsById={actorsById} /> : <div className="empty">Select a conversation</div>}
      </main>
    </div>
  );
}

function NewGroupForm({ me, actorsById, onDone, onError }) {
  const [name, setName] = useState('');
  const [selected, setSelected] = useState({});
  const [busy, setBusy] = useState(false);
  const candidates = Object.values(actorsById).filter((a) => a._id !== (me && me._id));
  const toggle = (id) => setSelected((s) => ({ ...s, [id]: !s[id] }));

  const create = (e) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    const memberIds = Object.keys(selected).filter((id) => selected[id]);
    Meteor.call('tg.createChannel', trimmed, memberIds, (err, convId) => {
      setBusy(false);
      if (err) { onError(err.message); return; }
      onDone(convId);
    });
  };

  return (
    <form className="new-group" onSubmit={create}>
      <input className="ng-name" value={name} onChange={(e) => setName(e.target.value)}
        placeholder="Group name" autoComplete="off" autoFocus />
      <div className="ng-members">
        {candidates.map((a) => (
          <label key={a._id} className="ng-member">
            <input type="checkbox" checked={!!selected[a._id]} onChange={() => toggle(a._id)} />
            {a.display_name} <span className="muted">@{a.username}</span>
          </label>
        ))}
        {candidates.length === 0 && <div className="muted">No other actors yet</div>}
      </div>
      <div className="ng-actions">
        <button type="button" className="ghost" onClick={() => onDone(null)}>Cancel</button>
        <button disabled={!name.trim() || busy}>{busy ? 'Creating…' : 'Create'}</button>
      </div>
    </form>
  );
}

function MessageList({ conv, me, actorsById }) {
  const [text, setText] = useState('');
  const [error, setError] = useState('');
  const messages = useTracker(() => {
    const sub = Meteor.subscribe('messages', conv._id, 200);
    return Messages.find({ conversation_id: conv._id }, { sort: { _id: 1 } }).fetch();
  }, [conv._id]);
  const listRef = React.useRef(null);

  React.useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [messages.length]);

  const send = (e) => {
    e.preventDefault();
    const body = text.trim();
    if (!body) return;
    setText('');
    Meteor.call('tg.sendMessage', conv._id, body, (err) => {
      if (err) setError(err.message);
    });
  };

  return (
    <div className="chat">
      <div className="messages" ref={listRef}>
        {messages.map((m) => {
          const a = actorsById[m.sender_id] || {};
          return (
            <div key={m._id} className={`msg ${a.kind || ''}`}>
              <span className="who">{a.display_name || m.sender_username || 'unknown'}</span>
              <div className="text"><Markdown text={m.body} /></div>
            </div>
          );
        })}
      </div>
      <form className="msg-form" onSubmit={send}>
        <input value={text} onChange={(e) => setText(e.target.value)}
          placeholder="Message… (@username mentions an agent)" autoComplete="off" />
        <button>Send</button>
      </form>
      {error && <div className="error-bar">{error}</div>}
    </div>
  );
}
