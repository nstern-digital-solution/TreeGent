import React, { useState } from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { Actors } from '../collections.js';
import { Secrets } from '../coreCollections.js';

const call = (path, method, body, asActorId) =>
  Meteor.callAsync('secrets.api', path, method, body, asActorId);

export function SecretsPane() {
  const me = useTracker(() => {
    Meteor.subscribe('actors');
    const u = Meteor.user();
    return u ? Actors.findOne({ username: u.username }) : null;
  }, []);
  const [q, setQ] = useState('');
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);
  const [reveal, setReveal] = useState(null);   // {id, name, value}
  const [form, setForm] = useState(null);
  const [target, setTarget] = useState('');     // reach-down: actor id to open as

  const actors = useTracker(() => Actors.find().fetch(), []);

  // LIVE: oplog-reactive metadata (value_enc never published); search runs
  // client-side in Minimongo -> instant, no refresh button.
  const allSecrets = useTracker(() => {
    const sub = Meteor.subscribe('secretsMeta');
    return Secrets.find().fetch();
  }, []);
  const items = (allSecrets || []).map((s) => ({ ...s, id: s._id })).filter((s) => {
    if (target) {
      if (s.owner !== target) return false;           // reach-down scope
    } else if (s.owner !== me._id) {
      return false;                                   // my scope: own rows only
    }
    if (!q) return true;
    const rx = new RegExp(q.replace(/[^\w@.\- ]/g, '\\$&'), 'i');
    return rx.test(s.name || '') || rx.test(s.username || '') ||
           rx.test(s.url || '');
  });

  const run = async (fn) => {
    setMsg(null); setErr(null);
    try { setMsg(await fn()); } catch (ex) { setErr(ex.reason || ex.message); }
  };

  const revealValue = async (id) => {
    setErr(null);
    try {
      const r = await call(`/secrets/${id}/value`, 'GET');
      setReveal(r);
      setTimeout(() => setReveal(null), 15000); // auto-hide after 15s
    } catch (ex) { setErr(ex.reason || ex.message); }
  };

  const save = async (e) => {
    e.preventDefault();
    await run(async () => {
      const body = { ...form };
      let note = `Secret ${form.name} saved.`;
      if (target) {
        // creating while viewing someone's scope = create FOR them:
        // they own it, caller is shared back in (superior-only, enforced
        // server-side)
        body.owner = target;
        body.shared_with = Array.from(new Set([...(form.shared_with || []), me._id]));
        note = `Secret ${form.name} saved for ${actors.find((a) => a._id === target)?.display_name} (shared with you).`;
      }
      await call('/secrets', 'POST', body);
      setForm(null);
      return note;
    });
  };

  if (!me) return <div className="page"><p className="muted">loading…</p></div>;

  return (
    <div className="page">
      <h2>Secrets</h2>
      {msg && <p className="ok-msg">{msg}</p>}
      {err && <p className="err-msg">{err}</p>}
      <p className="muted small">
          My scope shows your own secrets only. Superiors open a named subordinate's list via the actor picker (reach-down); secrets you create there land in their scope.
        </p>
      <div className="secret-toolbar">
        <input placeholder="search (live)" value={q}
          onChange={(e) => setQ(e.target.value)} />
        <button className="btn small" onClick={() => setForm({
          name: '', value: '', username: '', url: '', notes: '', shared_with: [],
        })}>new secret</button>
        <select value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="">my scope</option>
          {actors.filter((a) => a._id !== me._id).map((a) => (
            <option key={a._id} value={a._id}>open as: {a.display_name}</option>
          ))}
        </select>
      </div>
      {target && (
        <p className="muted small">
          reach-down: showing <b>{actors.find((a) => a._id === target)?.display_name}</b>'s
          secrets ({items.length} shown — live, no reload needed). New secrets you create now
          land in their scope only — not in yours (share explicitly via shared_with if needed).
        </p>
      )}
      <table className="usage-table">
        <thead><tr><th>Name</th><th>Username</th><th>URL</th><th>Shared with</th><th>Updated</th><th /></tr></thead>
        <tbody>
          {(items || []).map((s) => (
            <tr key={s.id}>
              <td>{s.name}</td>
              <td className="mono">{s.username || '—'}</td>
              <td className="mono">{s.url || '—'}</td>
              <td>{(s.shared_with || []).length}</td>
              <td>{s.updated_at ? new Date(s.updated_at).toLocaleDateString() : ''}</td>
              <td>
                <button className="btn small" onClick={() => run(async () => {
                  const r = await call(`/secrets/${s.id}/value`, 'GET', null, target || undefined);
                  setReveal(r);
                  setTimeout(() => setReveal(null), 15000);
                  return null;
                })}>reveal</button>{' '}
                {s.owner === me._id && (
                  <button className="btn small danger" onClick={() => run(async () => {
                    await call(`/secrets/${s.id}`, 'DELETE', null);
                    return `deleted ${s.name}`;
                  })}>delete</button>
                )}
              </td>
            </tr>
          ))}
          {items && items.length === 0 && (
            <tr><td colSpan={6} className="muted">Nothing here.</td></tr>
          )}
        </tbody>
      </table>
      {reveal && (
        <div className="compose-overlay" onClick={() => setReveal(null)}>
          <div className="login-card compose-card" onClick={(e) => e.stopPropagation()}>
            <h3>{reveal.name}</h3>
            <p className="muted small">auto-hides in 15 s — click anywhere to close</p>
            <textarea rows="4" readOnly value={reveal.value}
              onFocus={(e) => e.target.select()} />
            <button className="btn small" onClick={() => {
              navigator.clipboard?.writeText(reveal.value);
              setMsg('copied to clipboard');
              setReveal(null);
            }}>copy &amp; close</button>
          </div>
        </div>
      )}
      {form && (
        <div className="compose-overlay">
          <form className="login-card compose-card" onSubmit={save}>
            <h3>New secret{target ? ` — for ${actors.find((a) => a._id === target)?.display_name}` : ''}</h3>
            <input placeholder="name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <input placeholder="value" value={form.value} onChange={(e) => setForm({ ...form, value: e.target.value })} />
            <input placeholder="username" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
            <input placeholder="url" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
            <input placeholder="notes" value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
            <button className="btn" type="submit">save</button>
            <button className="btn small" type="button" onClick={() => setForm(null)}>cancel</button>
          </form>
        </div>
      )}
    </div>
  );
}
