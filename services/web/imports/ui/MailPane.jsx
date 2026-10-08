import React, { useState, useEffect } from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { Actors } from '../collections.js';
import { Mailboxes, MailMessages, Approvals } from '../mailCollections.js';

const call = (path, method, body) => Meteor.callAsync('mail.api', path, method, body);

export function ApprovalsPane() {
  const me = useTracker(() => {
    Meteor.subscribe('actors');
    const u = Meteor.user();
    return u ? Actors.findOne({ username: u.username }) : null;
  }, []);
  const { inbox, requested, actorsById, ready } = useTracker(() => {
    const s = Meteor.subscribe('approvalsData');
    return {
      inbox: Approvals.find({ status: 'pending' }).fetch(),
      requested: Approvals.find({}, { sort: { created_at: -1 }, limit: 100 }).fetch(),
      actorsById: Object.fromEntries(Actors.find().fetch().map((a) => [a._id, a])),
      ready: s.ready(),
    };
  }, []);
  const [err, setErr] = useState(null);
  const [rejectFor, setRejectFor] = useState(null);
  const [reason, setReason] = useState('');
  const [tab, setTab] = useState('inbox');

  // inbox = pending approvals where I am the approver (or admin sees all pending)
  const myInbox = useTracker(() => {
    const u = Meteor.user();
    if (!u) return [];
    return Approvals.find({ status: 'pending' }).fetch()
      .filter((a) => (u.isAdmin && !me) || (me && a.approver_id === me._id) || u.isAdmin);
  }, [me]);

  const decide = async (id, decision, why) => {
    setErr(null);
    try {
      await call(`/approvals/${id}/decide`, 'POST', { decision, reason: why || '' }, me ? me._id : '');
      setRejectFor(null); setReason('');
    } catch (ex) { setErr(ex.reason || ex.message); }
  };

  const name = (id) => (actorsById[id] ? actorsById[id].display_name : id);

  return (
    <div className="page">
      <h2>Approvals</h2>
      {err && <p className="err-msg">{err}</p>}
      <div className="tabbar">
        <button className={`btn small ${tab === 'inbox' ? 'active' : ''}`} onClick={() => setTab('inbox')}>
          Inbox ({myInbox.length} pending)
        </button>
        <button className={`btn small ${tab === 'all' ? 'active' : ''}`} onClick={() => setTab('all')}>
          All recent
        </button>
      </div>
      {!ready && <p className="muted">loading…</p>}
      {tab === 'inbox' && (
        <table className="usage-table">
          <thead><tr><th>Requested</th><th>By</th><th>Action</th><th>Detail</th><th>Decision</th></tr></thead>
          <tbody>
            {myInbox.map((a) => (
              <tr key={a._id}>
                <td>{a.created_at ? new Date(a.created_at).toLocaleString() : ''}</td>
                <td>{name(a.requester_id)}</td>
                <td className="mono">{a.action}</td>
                <td>
                  {a.action === 'mail.send'
                    ? `${a.payload.from} → ${a.payload.to} · ${a.payload.subject}` : JSON.stringify(a.payload)}
                </td>
                <td>
                  {rejectFor === a._id ? (
                    <span>
                      <input className="reason-input" placeholder="reason (goes back to the agent)" value={reason}
                        onChange={(e) => setReason(e.target.value)} />
                      <button className="btn small" onClick={() => decide(a._id, 'reject', reason)}>submit</button>{' '}
                      <button className="btn small" onClick={() => setRejectFor(null)}>cancel</button>
                    </span>
                  ) : (
                    <span>
                      <button className="btn small" onClick={() => decide(a._id, 'approve')}>approve</button>{' '}
                      <button className="btn small danger" onClick={() => setRejectFor(a._id)}>reject</button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
            {ready && myInbox.length === 0 && (
              <tr><td colSpan={5} className="muted">Nothing waiting on you.</td></tr>
            )}
          </tbody>
        </table>
      )}
      {tab === 'all' && (
        <table className="usage-table">
          <thead><tr><th>Requested</th><th>By</th><th>Action</th><th>Status</th><th>Decided</th><th>Reason</th></tr></thead>
          <tbody>
            {requested.map((a) => (
              <tr key={a._id}>
                <td>{a.created_at ? new Date(a.created_at).toLocaleString() : ''}</td>
                <td>{name(a.requester_id)}</td>
                <td className="mono">{a.action}</td>
                <td>{a.status}</td>
                <td>{a.decided_at ? new Date(a.decided_at).toLocaleString() : '—'}</td>
                <td>{a.reason || ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function MailPane() {
  const [selected, setSelected] = useState(null);
  const [compose, setCompose] = useState(null);
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);
  const [syncing, setSyncing] = useState(false);

  // R67 is PULL not push: the publications only read the local Mongo cache,
  // so opening the tab must trigger the mail service's read-through sync
  // (GET /mailboxes). Errors surface in the existing err slot.
  const pull = async () => {
    setSyncing(true); setErr(null);
    try {
      await call('/mailboxes', 'GET', null);
    } catch (ex) { setErr(ex.reason || ex.message); }
    setSyncing(false);
  };
  useEffect(() => { pull(); }, []);

  const me = useTracker(() => {
    Meteor.subscribe('actors');
    const u = Meteor.user();
    return u ? Actors.findOne({ username: u.username }) : null;
  }, []);

  const { mailboxes, messages, ready } = useTracker(() => {
    const s1 = Meteor.subscribe('mailboxesData');
    const s2 = Meteor.subscribe('mailMessages', selected);
    return {
      mailboxes: Mailboxes.find().fetch(),
      messages: selected ? MailMessages.find({ mailbox_id: selected }, { sort: { ts: -1 }, limit: 100 }).fetch() : [],
      ready: s1.ready() && (selected ? s2.ready() : true),
    };
  }, [selected]);

  // superiors/admins may open subordinates' mailboxes; everyone sees all in dev
  const send = async (e) => {
    e.preventDefault();
    setMsg(null); setErr(null);
    try {
      const r = await call('/send', 'POST', compose);
      setMsg(`Requested — approval ${r.approval_id.slice(4, 12)} pending with the superior.`);
      setCompose(null);
    } catch (ex) { setErr(ex.reason || ex.message); }
  };

  return (
    <div className="page">
      <h2>Mailboxes{' '}
        <button className="btn small" onClick={pull} disabled={syncing}>
          {syncing ? 'checking for new mail…' : 'refresh'}
        </button>
      </h2>
      {msg && <p className="ok-msg">{msg}</p>}
      {err && <p className="err-msg">{err}</p>}
      <div className="layout mail-layout">
        <div className="sidebar">
          <h3>Mailboxes</h3>
          <ul>
            {mailboxes.map((mb) => (
              <li key={mb._id}>
                <div className={`conv-row ${selected === mb._id ? 'on' : ''}`}
                  onClick={() => setSelected(mb._id)}>
                  {mb.address}
                  <span className="muted small"> {mb.kind === 'shared' ? '(shared)' : ''}</span>
                </div>
              </li>
            ))}
          </ul>
        </div>
        <div className="mail-main">
          {selected ? (
            <>
              <h3 className="mono">
                {mailboxes.find((m) => m._id === selected)?.address}
                <button className="btn small" style={{ marginLeft: '1rem' }} onClick={() => {
                  const mb = mailboxes.find((m) => m._id === selected);
                  setCompose({
                    from_mailbox: mb.address,
                    to: '', subject: '', text: '',
                    requester_id: mb.kind === 'personal' ? mb.owner : (mb.members || [])[0],
                  });
                }}>compose (as agent)</button>
              </h3>
              <table className="usage-table">
                <thead><tr><th>When</th><th>Dir</th><th>From</th><th>To</th><th>Subject</th><th>Status</th></tr></thead>
                <tbody>
                  {messages.map((m) => (
                    <tr key={m._id}>
                      <td>{m.ts ? new Date(m.ts).toLocaleString() : ''}</td>
                      <td>{m.direction === 'in' ? '⬅ in' : 'out ➡'}</td>
                      <td className="mono">{m.from_addr}</td>
                      <td className="mono">{m.to}</td>
                      <td>{m.subject}</td>
                      <td>{m.status}{m.via ? ` (${m.via})` : ''}</td>
                    </tr>
                  ))}
                  {ready && messages.length === 0 && (
                    <tr><td colSpan={6} className="muted">No mail in this mailbox yet.</td></tr>
                  )}
                </tbody>
              </table>
            </>
          ) : <p className="muted">Select a mailbox.</p>}
        </div>
      </div>

      {compose && (
        <div className="compose-overlay">
          <form className="login-card compose-card" onSubmit={send}>
            <h3>Compose (approval-gated)</h3>
            <p className="muted small">from {compose.from_mailbox} · requester {compose.requester_id?.slice(0, 12)}…</p>
            <input placeholder="to" value={compose.to} onChange={(e) => setCompose({ ...compose, to: e.target.value })} />
            <input placeholder="subject" value={compose.subject} onChange={(e) => setCompose({ ...compose, subject: e.target.value })} />
            <textarea rows="6" placeholder="text" value={compose.text} onChange={(e) => setCompose({ ...compose, text: e.target.value })} />
            <select value={compose.mode || 'background'} onChange={(e) => setCompose({ ...compose, mode: e.target.value })}>
              <option value="background">background — agent continues working</option>
              <option value="foreground">foreground — agent waits for the decision</option>
            </select>
            <button className="btn" type="submit">Request send</button>
            <button className="btn small" type="button" onClick={() => setCompose(null)}>cancel</button>
          </form>
        </div>
      )}
    </div>
  );
}
