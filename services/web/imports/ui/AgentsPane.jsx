import React, { useState, useRef, useEffect } from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { Actors } from '../collections.js';
import { AgentTranscripts } from '../collections.js';

// turn timestamps may be ISO strings (new rows) or Mongo Dates (legacy) — normalize
const iso = (v) => (v instanceof Date ? v.toISOString() : (v || ''));

// Parse an injection line into {sender, ts, body} — the canonical shape is
// "You have a new message from <X> received at <ts>: <body>"
// ts may itself contain colons (18:59, ISO stamps) → anchor on the final ": "
const parseInjection = (content) => {
  const m = /^You have a new message from (.+?) received at (.+): ([\s\S]*)$/.exec(content || '');
  if (m) return { sender: m[1], ts: m[2], body: m[3] };
  return null;
};

export function AgentsPane() {
  const actors = useTracker(() => Actors.find({}).fetch(), []);
  const agents = actors.filter((a) => a.kind === 'agent');
  const [selected, setSelected] = useState('');
  const me = useTracker(() => Meteor.user(), {});
  const isAdmin = me && me.isAdmin;
  const endRef = useRef(null);

  // chat view: the agent's whole session, oldest first, live
  const lines = useTracker(() => {
    if (!selected) return [];
    Meteor.subscribe('agentTranscripts', selected, 800);
    return AgentTranscripts.find({ agent_id: selected },
      { sort: { ts_received: 1 } }).fetch();
  }, [selected]);

  useEffect(() => {
    if (endRef.current) endRef.current.scrollIntoView({ block: 'end' });
  }, [lines.length]);

  if (!isAdmin) {
    return <div className="pane"><p className="muted">Admin only.</p></div>;
  }

  return (
    <div className="pane">
      <div className="secret-toolbar">
        <select value={selected} onChange={(e) => setSelected(e.target.value)}>
          <option value="">— pick agent —</option>
          {agents.map((a) => (
            <option key={a._id} value={a._id}>{a.display_name} ({a.username})</option>
          ))}
        </select>
      </div>

      {!selected && (
        <p className="muted">Select an agent to read its session — messages to and from
          the agent, tool calls with arguments and results.</p>
      )}

      {selected && (
        <div className="chatlog">
          {lines.map((l) => {
            const t = (iso(l.ts)).slice(11, 19);
            if (l.role === 'injection') {
              const p = parseInjection(l.content);
              if (p) {
                // a message from a colleague — render as a user-style chat bubble
                return (
                  <div key={l._id} className="cl-row cl-user">
                    <div className="cl-bubble">
                      <div className="cl-meta">{p.sender} · {p.ts}</div>
                      <div className="cl-body">{p.body}</div>
                    </div>
                  </div>
                );
              }
              // system-ish injection (heartbeat, mail notice) — centered quiet line
              return (
                <div key={l._id} className="cl-row cl-sys">
                  <span className="cl-sysline">{t} · {l.content}</span>
                </div>
              );
            }
            if (l.role === 'tool') {
              const meta = l.meta || {};
              return (
                <div key={l._id} className="cl-row cl-tool">
                  <div className="cl-toolcard">
                    <div className="cl-meta">🔧 {meta.tool}
                      {meta.args ? <code className="cl-args"> {JSON.stringify(meta.args).slice(0, 240)}</code> : null}
                      {' '}<span className="muted">{t}</span>
                    </div>
                    <pre className="cl-result">{l.content}</pre>
                  </div>
                </div>
              );
            }
            // assistant reply
            return (
              <div key={l._id} className="cl-row cl-assistant">
                <div className="cl-bubble">
                  <div className="cl-meta">assistant · {t}</div>
                  <div className="cl-body">{l.content}</div>
                </div>
              </div>
            );
          })}
          {lines.length === 0 && (
            <p className="muted">no session yet — messages appear here as the agent works</p>
          )}
          <div ref={endRef} />
        </div>
      )}
    </div>
  );
}
