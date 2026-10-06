import React, { useEffect, useState } from 'react';
import { Meteor } from 'meteor/meteor';

// R57 session explorer: the agent's own transcript — injections it received,
// its replies, every tool call and result, plus its inference job history.
export function SessionPane({ actors }) {
  const [sel, setSel] = useState('');
  const [lines, setLines] = useState(null);
  const [jobs, setJobs] = useState(null);
  const [err, setErr] = useState('');

  const load = (aid) => {
    setSel(aid); setLines(null); setJobs(null); setErr('');
    if (!aid) return;
    Meteor.callAsync('runtime.session', aid, 'transcript', { limit: 300 })
      .then((r) => setLines(r.lines || []))
      .catch((e) => setErr(`transcript: ${e.reason || e.message}`));
    Meteor.callAsync('runtime.session', aid, 'jobs', { limit: 100 })
      .then((r) => setJobs(r.jobs || []))
      .catch((e) => setErr(`jobs: ${e.reason || e.message}`));
  };

  const agents = (actors || []).filter((a) => a.kind === 'agent');

  return (
    <div>
      <h3>Session explorer</h3>
      <p className="muted">Pick an agent to read its full session: injected
        messages/heartbeats, replies, tool calls with arguments and results —
        and its inference job history (model, tokens, status).</p>
      <select value={sel} onChange={(e) => load(e.target.value)}>
        <option value="">— choose agent —</option>
        {agents.map((a) => (
          <option key={a._id} value={a._id}>
            {a.display_name} ({a.username})
          </option>
        ))}
      </select>

      {err && <p style={{ color: '#e55' }}>{err}</p>}

      {jobs && (
        <div>
          <h4>Inference jobs (newest first)</h4>
          <table className="data">
            <thead>
              <tr><th>time</th><th>model</th><th>status</th>
                  <th>tok in</th><th>tok out</th><th>reason</th><th>error</th></tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.job_id}>
                  <td>{(j.created_at || '').slice(11, 19)}</td>
                  <td>{j.model || '—'}</td>
                  <td>{j.status}</td>
                  <td>{j.tokens_in ?? '—'}</td>
                  <td>{j.tokens_out ?? '—'}</td>
                  <td>{j.reason || ''}</td>
                  <td style={{ color: '#e55' }}>{(j.error || '').slice(0, 120)}</td>
                </tr>
              ))}
              {jobs.length === 0 && (
                <tr><td colSpan="7" className="muted">no jobs yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {lines && (
        <div>
          <h4>Transcript</h4>
          {lines.map((l) => (
            <div key={l._id} className={
              'tr-line ' + (l.role === 'injection' ? 'tr-inj'
                : l.role === 'tool' ? 'tr-tool' : 'tr-assistant')}>
              <div className="tr-head">
                <b>{l.role}</b>
                <span className="muted"> {(l.ts || '').slice(11, 19)}</span>
                {l.role === 'tool' && (
                  <span className="muted">
                    {' '}· {(l.meta || {}).tool}
                    {(l.meta || {}).args
                      ? ` (${JSON.stringify((l.meta || {}).args).slice(0, 200)})`
                      : ''}
                  </span>
                )}
              </div>
              <pre className="tr-body">{l.content}</pre>
            </div>
          ))}
          {lines.length === 0 && (
            <p className="muted">no transcript yet — send the agent a message</p>
          )}
        </div>
      )}
    </div>
  );
}
