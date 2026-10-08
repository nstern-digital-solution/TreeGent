import React, { useState } from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { Actors } from '../collections.js';
import { Files } from '../coreCollections.js';

const call = (path, method, body, asActorId) =>
  Meteor.callAsync('files.api', path, method, body, asActorId);

export function FilesPane() {
  const me = useTracker(() => {
    Meteor.subscribe('actors');
    const u = Meteor.user();
    return u ? Actors.findOne({ username: u.username }) : null;
  }, []);

  const [q, setQ] = useState('');
  const items = useTracker(() => {
    Meteor.subscribe('filesMeta');
    return Files.find().fetch().map((f) => ({ ...f, id: f._id }));
  }, []).filter((f) => !q || (f.name || '').toLowerCase().includes(q.toLowerCase()));
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);
  const [target, setTarget] = useState('');
  const [shareFor, setShareFor] = useState(null); // file being shared
  const actors = useTracker(() => Actors.find().fetch(), []);

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
        { name: f.name, type: f.type, b64 });
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
          onChange={(e) => setQ(e.target.value)} />
        <label className="btn small" style={target ? { opacity: 0.5 } : undefined}
          title={target ? 'uploads always land in YOUR scope — clear the reach-down view to upload' : undefined}>
          upload
          <input type="file" hidden onChange={upload} disabled={!!target} />
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
                {/* R68: writes always act as the real caller — no reach-down
                    impersonation on share/delete (web tier rejects it) */}
                {f.owner === me._id && (
                  <button className="btn small" onClick={() => setShareFor(f)}>share</button>
                )}{' '}
                {f.owner === me._id && (
                  <button className="btn small danger" onClick={() => run(async () => {
                    await call(`/files/${f.id}`, 'DELETE', null);
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
      {shareFor && (
        <div className="compose-overlay" onClick={() => setShareFor(null)}>
          <div className="login-card compose-card" onClick={(e) => e.stopPropagation()}>
            <h3>Share “{shareFor.name}”</h3>
            {actors.filter((a) => a._id !== me._id).map((a) => (
              <label key={a._id} className="muted small">
                <input type="checkbox" checked={(shareFor.shared_with || []).includes(a._id)}
                  onChange={(e) => {
                    const cur = new Set(shareFor.shared_with || []);
                    if (e.target.checked) cur.add(a._id); else cur.delete(a._id);
                    setShareFor({ ...shareFor, shared_with: [...cur] });
                  }} />
                {' '}{a.display_name} ({a.kind})
              </label>
            ))}
            <button className="btn" onClick={() => run(async () => {
              await call(`/files/${shareFor.id}/share`, 'PUT',
                { with: shareFor.shared_with });
              setShareFor(null);
                      return 'sharing updated';
            })}>save</button>
            <button className="btn small" onClick={() => setShareFor(null)}>cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}
