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

// long content collapsed by default — one click expands
const LongBody = ({ text, cls }) => {
  const [open, setOpen] = useState(false);
  const limit = 600;
  if (!text) return null;
  if (text.length <= limit) return <div className={cls}>{text}</div>;
  return (
    <div className={cls}>
      {open ? text : `${text.slice(0, limit)}…`}
      <button className="cl-more" onClick={() => setOpen(!open)}>
        {open ? 'show less' : `show all (${text.length.toLocaleString()} chars)`}
      </button>
    </div>
  );
};

const UserBubble = ({ p }) => (
  <div className="cl-row cl-user">
    <div className="cl-bubble">
      <div className="cl-meta">{p.sender} <InfoDot label="ⓘ" text={p.ts} /></div>
      <LongBody text={p.body} cls="cl-body" />
    </div>
  </div>
);


function InfoDot({ label, text }) {
  // R63c: instant hover/click popover — native title tooltips are delayed,
  // clipped, and unselectable; this shows immediately and pins on click.
  const [open, setOpen] = React.useState(false);
  const wrap = React.useRef(null);
  React.useEffect(() => {
    if (!open) return undefined;
    const close = (e) => { if (!wrap.current || !wrap.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);
  return (
    <span className="cl-infodot" ref={wrap}
          onMouseEnter={() => setOpen(true)}
          onMouseLeave={() => setOpen(false)}>
      <button type="button" className="cl-info"
              onClick={() => setOpen((v) => !v)}>{label || 'ⓘ'}</button>
      {open ? (
        <span className="cl-infopop">
          <pre>{text}</pre>
        </span>
      ) : null}
    </span>
  );
}


// R63d: export the agent trace for offline analysis
function exportTrace(agent, format) {
  const fname = 'trace-' + (agent.username || agent._id) + '-' +
    new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  // one-shot full-depth subscription: the live view caps at 800 lines,
  // the export should carry everything available
  const sub = Meteor.subscribe('agentTranscripts', agent._id, 1000000, () => {
    const rows = AgentTranscripts.find({ agent_id: agent._id },
      { sort: { ts_received: 1 } }).fetch();
    sub.stop();
    let blob;
    if (format === 'json') {
      blob = new Blob([JSON.stringify({
        agent: { id: agent._id, name: agent.display_name, username: agent.username },
        exported_at: new Date().toISOString(),
        line_count: rows.length,
        lines: rows.map((r) => ({
          ts: r.ts, role: r.role, content: r.content, meta: r.meta || null,
        })),
      }, null, 2)], { type: 'application/json' });
    } else {
      const parts = [
        '# Agent trace — ' + (agent.display_name || agent.username),
        'agent_id: ' + agent._id + '  ',
        'username: ' + (agent.username || '') + '  ',
        'exported_at: ' + new Date().toISOString() + '  ',
        'lines: ' + rows.length,
        '',
        '---',
        '',
      ];
      for (const r of rows) {
        const t = (r.ts || '').toString().slice(0, 19).replace('T', ' ');
        if (r.role === 'tool') {
          const m = r.meta || {};
          parts.push('## ' + (t || '') + ' tool: ' + (m.tool || '?'));
          if (m.args) parts.push('args:', '```json',
            JSON.stringify(m.args, null, 2), '```', '');
          parts.push('result:', '```', String(r.content || ''), '```', '');
        } else if (r.role === 'user' || r.role === 'injection') {
          parts.push('## ' + (t || '') + ' ' + (r.role === 'user' ? 'user' : 'injection'),
            (r.meta && r.meta.sender ? '(from ' + r.meta.sender + ')' : ''));
          parts.push(String(r.content || ''), '');
        } else {
          parts.push('## ' + (t || '') + ' ' + r.role);
          parts.push(String(r.content || ''), '');
        }
      }
      blob = new Blob([parts.join('\n')], { type: 'text/markdown' });
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = fname + '.' + format;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  });
}

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
        {selected ? (
          <>
            <button className="btn small"
              onClick={() => exportTrace(agents.find((a) => a._id === selected), 'md')}>
              export trace (md)
            </button>
            <button className="btn small"
              onClick={() => exportTrace(agents.find((a) => a._id === selected), 'json')}>
              export trace (json)
            </button>
          </>
        ) : null}
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
                return <UserBubble key={l._id} p={p} />;
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
                      {meta.args ? (
                          <InfoDot label="ⓘ args" text={JSON.stringify(meta.args, null, 2)} />
                        ) : null}
                    </div>
                    <LongBody text={l.content} cls="cl-result" />
                  </div>
                </div>
              );
            }
            // assistant reply
            return (
              <div key={l._id} className="cl-row cl-assistant">
                <div className="cl-bubble">
                  <div className="cl-meta">assistant <InfoDot label="ⓘ" text={t} /></div>
                  <LongBody text={l.content} cls="cl-body" />
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
