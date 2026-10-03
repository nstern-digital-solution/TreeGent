import React, { useState } from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { Actors } from '../collections.js';

const call = (path, method, body, asActorId) =>
  Meteor.callAsync('files.api', path, method, body, asActorId);

export function FilesPane() {
  const me = useTracker(() => {
    Meteor.subscribe('actors');
    const u = Meteor.user();
    return u ? Actors.findOne({ username: u.username }) : null;
  }, []);
  const [items, setItems] = useState(null);
  const [q, setQ] = useState('');
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);
  const [target, setTarget] = useState('');
  const actors = useTracker(() => Actors.find().fetch(), []);

  const refresh = async (query = q) => {
    setErr(null);
    try {
      const r = await call(`/files?q=${encodeURIComponent(query)}`, 'GET', null, target || undefined);
      setItems(r);
    } catch (ex) { setErr(ex.reason || ex.message); }
  };

  React.useEffect(() => { if (me) refresh(''); }, [me && me._id, target]);

  const run = async (fn) => {
    setMsg(null); setErr(null);
    try { setMsg(await fn()); } catch (ex) { setErr(ex.reason || ex.message); }
  };

  const upload = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    e.target.value = '';
    await run(async () => {
      const b64 = await new Promise((resolve) => {
        const r = new FileReader();
        r.onload = () => resolve(String(r.result).split(',')[1]);
        r.readAsDataURL(f);
      });
      const r = await call('/files', 'UPLOAD',
        { name: f.name, type: f.type, b64 }, target || undefined);
      refresh();
      return `Uploaded ${r.name} (${r.size} bytes)`;
    });
  };

  const download = async (id, name) => {
    setErr(null);
    try {
      const r = await call(`/files/${id}/download`, 'GET', null, target || undefined);
      const blob = new Blob([r], { type: 'application/octet-stream' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
    } catch (ex) { setErr(ex.reason || ex.message); }
  };

  const presign = async (id) => {
    await run(async () => {
      const r = await call(`/files/${id}/url`, 'GET', null, target || undefined);
      window.open(r.url, '_blank');
      return null;
    });
  };

  if (!me) return <div className="page"><p className="muted">loading…</p></div>;

  return (
    <div className="page">
      <h2>Files</h2>
      {msg && <p className="ok-msg">{msg}</p>}
      {err && <p className="err-msg">{err}</p>}
      <p className="muted small">
        Bytes on the deployment's S3 host; Mongo holds metadata. Same rules:
        your own + shared; superiors reach down by name.
      </p>
      <div className="secret-toolbar">
        <input placeholder="search files" value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && refresh()} />
        <button className="btn small" onClick={() => refresh()}>search</button>
        <label className="btn small">
          upload
          <input type="file" hidden onChange={upload} />
        </label>
        <select value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="">my scope</option>
          {actors.filter((a) => a._id !== me._id).map((a) => (
            <option key={a._id} value={a._id}>open as: {a.display_name}</option>
          ))}
        </select>
        {target && <span className="muted small">reach-down view (metadata + named access)</span>}
      </div>
      <table className="usage-table">
        <thead><tr><th>Name</th><th>Size</th><th>Type</th><th>Shared with</th><th>Updated</th><th /></tr></thead>
        <tbody>
          {(items || []).map((f) => (
            <tr key={f.id}>
              <td>{f.name}</td>
              <td>{f.size ? `${(f.size / 1024).toFixed(1)}k` : '—'}</td>
              <td className="mono">{f.content_type || '—'}</td>
              <td>{(f.shared_with || []).length}</td>
              <td>{f.updated_at ? new Date(f.updated_at).toLocaleDateString() : ''}</td>
              <td>
                <button className="btn small" onClick={() => download(f.id, f.name)}>download</button>{' '}
                <button className="btn small" onClick={() => presign(f.id)}>direct URL</button>{' '}
                {f.owner === (target || me._id) && (
                  <button className="btn small danger" onClick={() => run(async () => {
                    await call(`/files/${f.id}`, 'DELETE', null, target || undefined);
                    refresh();
                    return `deleted ${f.name}`;
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
    </div>
  );
}
