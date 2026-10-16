import React, { useState, useEffect, useCallback } from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { Actors } from '../collections.js';
import { Files } from '../coreCollections.js';

const call = (path, method, body, asActorId) =>
  Meteor.callAsync('files.api', path, method, body, asActorId);

// folder paths ride in URLs — encode per segment, keep the slashes
const encPath = (p) => p.split('/').map(encodeURIComponent).join('/');

export function FilesPane() {
  const me = useTracker(() => {
    Meteor.subscribe('actors');
    const u = Meteor.user();
    return u ? Actors.findOne({ username: u.username }) : null;
  }, []);
  const actors = useTracker(() => Actors.find().fetch(), []);
  const isAdmin = useTracker(() => {
    const u = Meteor.user();
    return !!(u && u.isAdmin);
  }, []);
  // flat search rows + reach-down metadata — filesMeta now covers the whole
  // subtree (like secretsMeta), so the "open as:" picker lists subordinates'
  // files
  const allFiles = useTracker(() => {
    Meteor.subscribe('filesMeta');
    return Files.find().fetch().map((f) => ({ ...f, id: f._id }));
  }, []);

  const [q, setQ] = useState('');
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);
  const [target, setTarget] = useState('');              // reach-down actor id
  const [path, setPath] = useState('');                  // current folder
  const [items, setItems] = useState({ folders: [], files: [] });
  const [newFolder, setNewFolder] = useState('');
  const [shareFor, setShareFor] = useState(null);        // per-file dialog
  const [shareFolder, setShareFolder] = useState(null);  // per-folder dialog

  const run = async (fn) => {
    setMsg(null); setErr(null);
    try { const r = await fn(); if (r) setMsg(r); } catch (ex) { setErr(ex.reason || ex.message); }
  };

  const load = useCallback(async (p) => {
    const r = await call(`/files?path=${encodeURIComponent(p)}`, 'GET',
                         null, target || undefined);
    setItems(r || { folders: [], files: [] });
    setPath(p);
  }, [target]);

  // browse root: my home, or the reach-down target's home
  useEffect(() => {
    if (!me) return;
    const actor = target ? actors.find((a) => a._id === target) : null;
    const home = `/home/${(actor || me).username}`;
    setPath(home);
    load(home).catch((ex) => setErr(ex.reason || ex.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me && me._id, target, actors.length]);

  const canManage = (owner) => owner === me._id || isAdmin;

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
                           { name: f.name, type: f.type, b64, path });
      await load(path);
      return `Uploaded ${r.name} (${r.size} bytes) into ${path}`;
    });
  };

  const mkdir = () => run(async () => {
    const name = newFolder.trim();
    if (!name) return null;
    setNewFolder('');
    const r = await call('/folders', 'POST',
                         { path: `${path.replace(/\/$/, '')}/${name}` });
    await load(path);
    return `created ${r.path} (${(r.created || []).length} new)`;
  });

  const download = async (id, name) => {
    setErr(null);
    try {
      const r = await call(`/files/${id}/download`, 'GET', null, target || undefined);
      // R12: server returns {b64} — decode to bytes; UTF-8 text decode corrupted binaries
      const bytes = r && typeof r === 'object' && typeof r.b64 === 'string'
        ? Uint8Array.from(atob(r.b64), c => c.charCodeAt(0))
        : new TextEncoder().encode(r);
      const blob = new Blob([bytes], { type: 'application/octet-stream' });
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

  const delFile = (f) => run(async () => {
    await call(`/files/${f.id}`, 'DELETE', null);
    await load(path);
    return `deleted ${f.name}`;
  });

  const delFolder = (d) => run(async () => {
    await call(`/folders/${encPath(d.path)}`, 'DELETE', null);
    await load(path);
    return `deleted ${d.path}`;
  });

  // ---- prefix shares (folder = everything inside it) -----------------

  const folderGrants = async (folderPath) =>
    (await call('/shares', 'GET', null))
      .filter((s) => s.path_prefix === folderPath);

  const openFolderShare = async (d) => {
    await run(async () => {
      setShareFolder({ path: d.path, name: d.name, grants: await folderGrants(d.path) });
      return null;
    });
  };

  const coveredBy = (actorId) =>
    (shareFolder.grants || []).some((g) => (g.with || []).includes(actorId));

  const toggleFolderShare = (actorId, on) => run(async () => {
    if (on) {
      await call('/shares', 'POST', { path: shareFolder.path, with: [actorId] });
    } else {
      // revoke = remove (DELETE); a multi-actor grant keeps its others
      for (const g of (shareFolder.grants || [])
        .filter((x) => (x.with || []).includes(actorId))) {
        await call(`/shares/${g.id}`, 'DELETE', null);
        const rest = (g.with || []).filter((x) => x !== actorId);
        if (rest.length) {
          await call('/shares', 'POST', { path: shareFolder.path, with: rest });
        }
      }
    }
    setShareFolder({ ...shareFolder, grants: await folderGrants(shareFolder.path) });
    return null;
  });

  if (!me) return <div className="page"><p className="muted">loading…</p></div>;

  // search runs over the flat published set (own + shared + subtree);
  // otherwise the table shows the direct children of the current folder
  const searching = !!q;
  const rows = searching
    ? allFiles.filter((f) => (!target || f.owner === target)
      && (f.name || '').toLowerCase().includes(q.toLowerCase()))
    : (items.files || []);
  const folderRows = searching ? [] : (items.folders || []);

  const crumbs = [{ label: '/', p: '/' }];
  let acc = '';
  for (const s of path.split('/').filter(Boolean)) {
    acc += `/${s}`;
    crumbs.push({ label: s, p: acc });
  }

  return (
    <div className="page">
      <h2>Files</h2>
      {msg && <p className="ok-msg">{msg}</p>}
      {err && <p className="err-msg">{err}</p>}
      <p className="muted small">
        Bytes on the deployment's S3 host; Mongo holds metadata. Same rules:
        your own + shared; superiors reach down by name. Folder shares cover
        everything inside the folder.
      </p>
      <div className="secret-toolbar">
        <input placeholder="search files" value={q}
          onChange={(e) => setQ(e.target.value)} />
        <label className="btn small" style={target ? { opacity: 0.5 } : undefined}
          title={target ? 'uploads always land in YOUR scope — clear the reach-down view to upload' : undefined}>
          upload
          <input type="file" hidden onChange={upload} disabled={!!target} />
        </label>
        <input placeholder="new folder name" value={newFolder} style={{ maxWidth: '12em' }}
          onChange={(e) => setNewFolder(e.target.value)}
          disabled={!!target}
          onKeyDown={(e) => { if (e.key === 'Enter') mkdir(); }} />
        <button className="btn small" onClick={mkdir} disabled={!!target}>new folder</button>
        <select value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="">my scope</option>
          {actors.filter((a) => a._id !== me._id).map((a) => (
            <option key={a._id} value={a._id}>open as: {a.display_name}</option>
          ))}
        </select>
        {target && <span className="muted small">reach-down view (metadata + named access)</span>}
      </div>
      <p className="muted small" style={{ marginBottom: '0.3em' }}>
        {crumbs.map((c, i) => (
          <span key={c.p}>
            {i > 0 && <span className="muted"> / </span>}
            <button className="btn small" onClick={() => run(() => load(c.p))}>{c.label}</button>
          </span>
        ))}
      </p>
      <table className="usage-table">
        <thead><tr><th>Name</th><th>Size</th><th>Type</th><th>Shared with</th><th>Updated</th><th /></tr></thead>
        <tbody>
          {folderRows.map((d) => (
            <tr key={d.path}>
              <td><button className="btn small" onClick={() => run(() => load(d.path))}>{d.name}/</button></td>
              <td>—</td>
              <td className="mono">folder</td>
              <td>—</td>
              <td>—</td>
              <td>
                {!target && canManage(d.owner) && (
                  <button className="btn small" onClick={() => openFolderShare(d)}>share</button>
                )}{' '}
                {!target && canManage(d.owner) && (
                  <button className="btn small danger" onClick={() => delFolder(d)}>delete</button>
                )}
              </td>
            </tr>
          ))}
          {rows.map((f) => (
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
                {!target && canManage(f.owner) && (
                  <button className="btn small" onClick={() => setShareFor(f)}>share</button>
                )}{' '}
                {!target && canManage(f.owner) && (
                  <button className="btn small danger" onClick={() => delFile(f)}>delete</button>
                )}
              </td>
            </tr>
          ))}
          {folderRows.length === 0 && rows.length === 0 && (
            <tr><td colSpan={6} className="muted">Nothing here.</td></tr>
          )}
        </tbody>
      </table>
      {shareFor && (
        <div className="compose-overlay" onClick={() => setShareFor(null)}>
          <div className="login-card compose-card" onClick={(e) => e.stopPropagation()}>
            <h3>Share “{shareFor.name}”</h3>
            <p className="muted small">the full list replaces the current one (set-replacement)</p>
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
              await load(path);
              return 'sharing updated';
            })}>save</button>
            <button className="btn small" onClick={() => setShareFor(null)}>cancel</button>
          </div>
        </div>
      )}
      {shareFolder && (
        <div className="compose-overlay" onClick={() => setShareFolder(null)}>
          <div className="login-card compose-card" onClick={(e) => e.stopPropagation()}>
            <h3>Share folder “{shareFolder.name}”</h3>
            <p className="muted small">
              a folder share covers everything inside this folder — files added
              later are included automatically; uncheck to revoke (instant)
            </p>
            {actors.filter((a) => a._id !== me._id).map((a) => (
              <label key={a._id} className="muted small">
                <input type="checkbox" checked={coveredBy(a._id)}
                  onChange={(e) => toggleFolderShare(a._id, e.target.checked)} />
                {' '}{a.display_name} ({a.kind})
              </label>
            ))}
            <button className="btn small" onClick={() => setShareFolder(null)}>close</button>
          </div>
        </div>
      )}
    </div>
  );
}
