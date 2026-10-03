import React, { useState } from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { Actors } from '../collections.js';
import { UsageEvents } from '../proxyCollections.js';

const fmtTok = (x) => {
  x = Number(x || 0);
  return x >= 1e6 ? `${(x / 1e6).toFixed(1)}M` : x >= 1e3 ? `${(x / 1e3).toFixed(1)}k` : String(x);
};

export function UsageDashboard() {
  const [days, setDays] = useState(7);

  const { events, agentsById, ready } = useTracker(() => {
    const s1 = Meteor.subscribe('proxyUsage', days);
    const s2 = Meteor.subscribe('actors');
    return {
      events: UsageEvents.find({}, { sort: { ts: -1 }, limit: 2000 }).fetch(),
      agentsById: Object.fromEntries(Actors.find().fetch().map((a) => [a._id, a])),
      ready: s1.ready() && s2.ready(),
    };
  }, [days]);

  const agg = {};
  for (const e of events) {
    const a = (agg[e.agent_id] ||= { jobs: 0, ok: 0, tokensIn: 0, tokensOut: 0 });
    a.jobs += 1;
    if (e.status === 'ok') a.ok += 1;
    a.tokensIn += e.tokens_in || 0;
    a.tokensOut += e.tokens_out || 0;
  }
  const rows = Object.entries(agg).sort((x, y) => y[1].tokensIn + y[1].tokensOut - x[1].tokensIn - x[1].tokensOut);
  const totalIn = rows.reduce((s, [, a]) => s + a.tokensIn, 0);
  const totalOut = rows.reduce((s, [, a]) => s + a.tokensOut, 0);
  const totalJobs = rows.reduce((s, [, a]) => s + a.jobs, 0);

  return (
    <div className="page">
      <h2>Usage — inference</h2>
      <p className="muted">
        Last {days} days · {totalJobs} jobs · {fmtTok(totalIn)} in / {fmtTok(totalOut)} out
        {' '}
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={1}>1 day</option>
          <option value={7}>7 days</option>
          <option value={30}>30 days</option>
        </select>
      </p>
      {!ready && <p className="muted">loading…</p>}
      <table className="usage-table">
        <thead>
          <tr><th>Agent</th><th>Jobs</th><th>OK</th><th>Tokens in</th><th>Tokens out</th></tr>
        </thead>
        <tbody>
          {rows.map(([id, a]) => (
            <tr key={id}>
              <td>{agentsById[id] ? `${agentsById[id].display_name} (${agentsById[id].kind})` : id}</td>
              <td>{a.jobs}</td>
              <td>{a.ok}</td>
              <td>{fmtTok(a.tokensIn)}</td>
              <td>{fmtTok(a.tokensOut)}</td>
            </tr>
          ))}
          {ready && rows.length === 0 && (
            <tr><td colSpan={5} className="muted">No usage yet — submit a job and it lands here live.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
