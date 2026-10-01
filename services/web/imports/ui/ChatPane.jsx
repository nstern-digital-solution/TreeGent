import React, { useState } from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { Actors, Conversations, Messages } from '../collections.js';

export function ChatPane({ me }) {
  const [activeId, setActiveId] = useState(null);
  const [error, setError] = useState('');

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
          {ready && convs.length === 0 && <li className="muted">No conversations yet — open one from the Roster</li>}
        </ul>
      </aside>
      <main className="chatpane">
        {active ? <MessageList conv={active} me={me} actorsById={actorsById} /> : <div className="empty">Select a conversation</div>}
      </main>
    </div>
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
              <span className="text">{m.body}</span>
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
