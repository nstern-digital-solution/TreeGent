import React, { useState } from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { Actors } from '../collections.js';
import { RuntimeTurns } from '../coreCollections.js';
import { AgentTranscripts } from '../collections.js';

const call = (path, method, body, asActorId) =>
  Meteor.callAsync('runtime.api', path, method, body, asActorId);

export function AgentsPane() {
  const actors = useTracker(() => Actors.find({}).fetch(), []);
  const agents = actors.filter((a) => a.kind === 'agent');
  const [selected, setSelected] = useState('');

  const [expanded, setExpanded] = useState(null); // turn id with details open
  const [msg, setMsg] = useState('');
  const me = useTracker(() => Meteor.user(), {});
  const isAdmin = me && me.isAdmin;

  const turns = useTracker(() => {
    if (!selected) return [];
    Meteor.subscribe('agentTurns', selected, 50);
    return RuntimeTurns.find({ agent_id: selected },
      { sort: { started: -1 }, limit: 50 }).fetch();
  }, [selected]);
  const transcript = useTracker(() => {
    if (!selected || !expanded) return [];
    Meteor.subscribe('agentTranscripts', selected, 500);
    return AgentTranscripts.find({ agent_id: selected },
      { sort: { ts_received: 1 } }).fetch();
  }, [selected, expanded]);
  const load = (agentId) => setSelected(agentId);

  if (!isAdmin) {
    return <div className="pane"><p className="muted">Admin only.</p></div>;
  }

  return (
    <div className="pane">
      <div className="secret-toolbar">
        <select value={selected} onChange={(e) => load(e.target.value)}>
          <option value="">— pick agent —</option>
          {agents.map((a) => (
            <option key={a._id} value={a._id}>{a.display_name}</option>
          ))}
        </select>
        {selected && (
          <button className="btn small" onClick={() => load(selected)}>refresh</button>
        )}
      </div>
      {msg && <p className="err-msg">{msg}</p>}
      {!selected && <p className="muted">Select an agent to see turn and action history.</p>}
      {selected && (
        <table className="usage-table">
          <thead>
            <tr><th>started</th><th>trigger</th><th>steps</th><th>last activity / final</th></tr>
          </thead>
          <tbody>
            {turns.map((t) => (
              <React.Fragment key={t.id}>
                <tr className="clickable-row" onClick={() => setExpanded(expanded === t.id ? null : t.id)}>
                  <td>{t.started ? new Date(t.started).toLocaleTimeString() : ''}</td>
                  <td>{t.trigger}</td>
                  <td>{t.steps}</td>
                  <td>{(t.final || '').slice(0, 120) || <span className="muted">—</span>}</td>
                </tr>
                {expanded === t.id && (
                  <tr className="detail-row">
                    <td colSpan={4}>
                      <div className="mono-block">
                        <div className="muted small">session log — messages and tool calls this turn</div>
                        {(transcript.filter((l) => (l.ts || '') >= (t.started || '').slice(0, 19)
                          && (l.ts || '') <= ((t.started || '').slice(0, 11)
                            + (t.ended ? new Date(t.ended).toISOString().slice(11, 19) : '99:99:99')))).map((l) => (
                          <div key={l._id} className={'tr-line '
                            + (l.role === 'injection' ? 'tr-inj'
                              : l.role === 'tool' ? 'tr-tool' : 'tr-assistant')}>
                            <div className="tr-head">
                              <b>{l.role}</b>
                              <span className="muted"> {(l.ts || '').slice(11, 19)}</span>
                              {l.role === 'tool' && (
                                <span className="muted">
                                  {' '}· {(l.meta || {}).tool}
                                  {(l.meta || {}).args ? ` (${JSON.stringify((l.meta || {}).args).slice(0, 160)})` : ''}
                                </span>
                              )}
                            </div>
                            <pre className="tr-body">{l.content}</pre>
                          </div>
                        ))}
                        <div className="muted small" style={{ marginTop: 6 }}>final</div>
                        <div className="mono-line">{t.final || '—'}</div>
                      </div>
                    </td>
                  </tr>
                )}
              </React.Fragment>
            ))}
            {turns.length === 0 && (
              <tr><td colSpan={4} className="muted">no turns recorded</td></tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
