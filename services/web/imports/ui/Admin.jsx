import React, { useEffect, useState } from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { Actors } from '../collections.js';

export function Admin() {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [display, setDisplay] = useState('');
  const [kind, setKind] = useState('human');
  const [parent, setParent] = useState('');
  const [msg, setMsg] = useState('');
  const [issued, setIssued] = useState(null);
  const [hosts, setHosts] = useState([]);
  useEffect(() => {
    Meteor.callAsync('tg.hosts.list').then((r) => setHosts(r.hosts || []));
  }, []);
 // {agentId, key} shown ONCE

  const issueKey = (agentId) => {
    setMsg(''); setIssued(null);
    Meteor.call('tg.issueAgentKey', agentId, (err, key) => {
      if (err) return setMsg(err.message);
      setIssued({ agentId, key });
    });
  };

  const { actors, users } = useTracker(() => {
    Meteor.subscribe('actors');
    Meteor.subscribe('allUsernames');
    return {
      actors: Actors.find({}, { sort: { username: 1 } }).fetch(),
      users: Meteor.users.find({}, { sort: { username: 1 } }).fetch(),
    };
  }, []);

  const create = (e) => {
    e.preventDefault();
    setMsg('');
    Meteor.call('tg.createActor', username.trim(), password, display.trim(), kind,
      parent || null, hostId || null, (err) => {
        if (err) return setMsg(err.message);
        setMsg(`created ${username.trim()}`);
        setUsername(''); setPassword(''); setDisplay(''); setKind('human'); setParent(''); setHostId('');
      });
  };

  const actorOf = (u) => actors.find((a) => a.username === u.username);

  return (
    <div className="page">
      <h2>Admin — users &amp; actors</h2>
      <form className="new-user" onSubmit={create}>
        <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="username" required />
        <input value={password} onChange={(e) => setPassword(e.target.value)} type="password" placeholder="password" required />
        <input value={display} onChange={(e) => setDisplay(e.target.value)} placeholder="display name" required />
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="human">human</option>
          <option value="agent">agent</option>
        </select>
        <select value={hostId} onChange={(e) => setHostId(e.target.value)}>
          <option value="">(central)</option>
          {hosts.map((h) => <option key={h.id} value={h.id}>{h.name} ({h.status})</option>)}
        </select>
        <select value={parent} onChange={(e) => setParent(e.target.value)}>
          <option value="">— no parent —</option>
          {actors.map((a) => <option key={a._id} value={a._id}>{a.display_name}</option>)}
        </select>
        <button>Create</button>
      </form>
      {msg && <p className="muted">{msg}</p>}
      <h3>Users</h3>
      <ul>
        {users.map((u) => {
          const a = actorOf(u);
          return <li key={u._id}>{u.username} — {a ? `${a.display_name} (${a.kind})` : 'no actor'}</li>;
        })}
      </ul>

      <h3>Agents</h3>
      <p className="muted">Issue a key when the agent first needs to authenticate
        (agent host config). The key is shown once and never again — store it
        where the agent runs.</p>
      <ul>
        {actors.filter((a) => a.kind === 'agent').map((a) => (
          <li key={a._id} className="agent-key-row">
            <span>{a.display_name} ({a.username})</span>
            <button onClick={() => issueKey(a._id)}>issue key</button>
            {issued && issued.agentId === a._id && (
              <code className="key-reveal">{issued.key}</code>
            )}
          </li>
        ))}
        {actors.filter((a) => a.kind === 'agent').length === 0 && (
          <li className="muted">no agents yet — create one above (kind: agent)</li>
        )}
      </ul>
    </div>
  );
}
