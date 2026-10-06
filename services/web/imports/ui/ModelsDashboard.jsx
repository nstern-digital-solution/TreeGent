import React, { useEffect, useState } from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { TaskClasses, Providers, ModelCatalog } from '../proxyCollections.js';

const call = (path, method, body) => Meteor.callAsync('proxy.admin', path, method, body);

function ProvidersTab() {
  const { providers, ready } = useTracker(() => {
    const s = Meteor.subscribe('proxyProviders');
    return { providers: Providers.find().fetch(), ready: s.ready() };
  }, []);
  // readiness is computed on the proxy (env access) — pull via admin API
  const [readyById, setReadyById] = useState({});
  const [form, setForm] = useState({ id: '', base_url: 'https://', key_env: '', kind: 'openai-compat', key: '' });
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    call('/admin/providers', 'GET')
      .then((rows) => setReadyById(Object.fromEntries(rows.map((r) => [r.id, r.ready]))))
      .catch(() => {});
  }, [providers.length]);

  const run = async (fn) => {
    setMsg(null); setErr(null);
    try { setMsg(await fn()); } catch (ex) { setErr(ex.reason || ex.message); }
  };

  return (
    <div>
      <h3>
        Providers{' '}
        <button className="btn small" onClick={() => run(async () => {
          const r = await call('/admin/providers/refresh-catalog', 'POST');
          return `Catalog refreshed: ${r.catalog_models} models from ${r.ready_providers.join(', ') || 'no ready providers'}`;
        })}>Refresh catalog</button>
      </h3>
      {msg && <p className="ok-msg">{msg}</p>}
      {err && <p className="err-msg">{err}</p>}
      <table className="usage-table">
        <thead><tr><th>Name</th><th>Kind</th><th>Base URL</th><th>Key env var</th><th>Enabled</th><th>Ready</th><th /></tr></thead>
        <tbody>
          {providers.map((p) => (
            <tr key={p._id}>
              <td>{p._id}</td>
              <td>{p.kind}</td>
              <td className="mono">{p.base_url}</td>
              <td className="mono">{p.key_env}</td>
              <td>{p.enabled ? 'yes' : 'no'}</td>
              <td>{readyById[p._id] ? '✓ ready' : 'not ready'}</td>
              <td>
                <button className="btn small danger" onClick={() => run(async () => {
                  await call(`/admin/providers/${encodeURIComponent(p._id)}`, 'DELETE');
                  return `Provider ${p._id} removed`;
                })}>remove</button>
              </td>
            </tr>
          ))}
          {ready && providers.length === 0 && (
            <tr><td colSpan={7} className="muted">No providers configured. Fresh deployments start empty — add one below.</td></tr>
          )}
        </tbody>
      </table>
      <h4>Add / update provider</h4>
      <form onSubmit={(e) => { e.preventDefault(); run(async () => {
        await call(`/admin/providers/${encodeURIComponent(form.id)}`, 'PUT', {
          kind: form.kind, base_url: form.base_url, key_env: form.key_env, enabled: true, key: form.key || undefined,
        });
        setForm({ id: '', base_url: 'https://', key_env: '', kind: 'openai-compat', key: '' });
        return `Provider ${form.id} saved. Run "Refresh catalog" to pull its models.`;
      }); }} className="provider-form">
        <input placeholder="name (lowercase id)" value={form.id}
               onChange={(e) => setForm({ ...form, id: e.target.value })} />
        <input placeholder="https://api.example.com/v1" value={form.base_url}
               onChange={(e) => setForm({ ...form, base_url: e.target.value })} />
        <input placeholder="API key (stored encrypted — leave blank to keep)" type="password" value={form.key}
               onChange={(e) => setForm({ ...form, key: e.target.value })} />
        <input placeholder="or env var name (optional)" value={form.key_env}
               onChange={(e) => setForm({ ...form, key_env: e.target.value })} />
        <button className="btn" type="submit">Save provider</button>
        <p className="muted small">Keys are never stored in TreeGent — only the env-var NAME. The proxy reads the value from its environment at call time.</p>
      </form>
    </div>
  );
}

function ClassesTab() {
  const { classes, ready } = useTracker(() => {
    const s = Meteor.subscribe('proxyClasses');
    return { classes: TaskClasses.find().fetch(), ready: s.ready() };
  }, []);
  return (
    <div>
      <h3>Classes</h3>
      <p className="muted small">
        A class is a designation models can carry. Selection = designation → modality
        (request needs image/audio/video/file) → availability (rate limits, errors) →
        highest rank. Rank and designations are set per model in the catalog tab.
      </p>
      <table className="usage-table">
        <thead><tr><th>Class</th><th>Description</th><th>Default</th></tr></thead>
        <tbody>
          {classes.map((c) => (
            <tr key={c._id}>
              <td>{c._id}</td>
              <td>{c.description}</td>
              <td>{c.default ? 'yes' : ''}</td>
            </tr>
          ))}
          {ready && classes.length === 0 && (
            <tr><td colSpan={3} className="muted">No classes — the proxy seeds agent/task on startup.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function CatalogTab() {
  const { models, ready } = useTracker(() => {
    const s = Meteor.subscribe('proxyCatalog', 400);
    const sorted = ModelCatalog.find().fetch()
      .sort((a, b) => (b.rank || 0) - (a.rank || 0) || (a._id < b._id ? -1 : 1));
    return { models: sorted, ready: s.ready() };
  }, []);
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);

  const setPolicy = async (id, body, note) => {
    setMsg(null); setErr(null);
    try {
      await call(`/admin/models/${id}/policy`, 'PUT', body);
      setMsg(note);
    } catch (ex) { setErr(ex.reason || ex.message); }
  };

  return (
    <div>
      <h3>Model catalog ({models.length} listed)</h3>
      {msg && <p className="ok-msg">{msg}</p>}
      {err && <p className="err-msg">{err}</p>}
      <p className="muted small">
        Ranked first. designations: a = agent loop, t = auxiliary tasks. New models
        default to task/rank 0 — promote and rank the ones you trust.
      </p>
      <table className="usage-table">
        <thead><tr><th>Model</th><th>Desig.</th><th title="Higher rank = tried FIRST by the dispatcher (descending). Failover to lower ranks only on failure.">Rank ↓ (highest first)</th><th>Input modalities</th><th>Context</th><th>Actions</th></tr></thead>
        <tbody>
          {models.map((m) => {
            const des = m.designations || [];
            return (
              <tr key={m._id} style={m.excluded ? { opacity: 0.45 } : undefined}>
                <td className="mono">{m._id}</td>
                <td>{des.includes('agent') ? 'a' : ''}{des.includes('task') ? 't' : ''}</td>
                <td>
                  <input className="rank-input" type="number" defaultValue={m.rank || 0}
                    onBlur={(e) => {
                      const v = Number(e.target.value);
                      if (v !== (m.rank || 0)) setPolicy(m._id, { rank: v }, `${m._id} rank → ${v}`);
                    }} />
                </td>
                <td>{(m.modalities || []).join(', ')}</td>
                <td>{m.ctx ? m.ctx.toLocaleString() : '—'}</td>
                <td>
                  <button className="btn small" onClick={() => setPolicy(m._id,
                    { designations: des.includes('agent') ? ['task'] : ['agent', 'task'] },
                    des.includes('agent') ? `${m._id} → task only` : `${m._id} → agent+task`)}>
                    {des.includes('agent') ? '→ task only' : '→ agent+task'}
                  </button>{' '}
                  <button className="btn small danger" onClick={() => setPolicy(m._id,
                    { excluded: !m.excluded }, m.excluded ? `${m._id} un-excluded` : `${m._id} excluded`)}>
                    {m.excluded ? 'un-exclude' : 'exclude'}
                  </button>
                </td>
              </tr>
            );
          })}
          {!ready && <tr><td colSpan={6} className="muted">loading…</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

export function ModelsDashboard() {
  const [tab, setTab] = useState('providers');
  return (
    <div className="page">
      <h2>Providers &amp; Models</h2>
      <div className="tabbar">
        {['providers', 'classes', 'catalog'].map((t) => (
          <button key={t} className={`btn small ${tab === t ? 'active' : ''}`}
                  onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>
      {tab === 'providers' && <ProvidersTab />}
      {tab === 'classes' && <ClassesTab />}
      {tab === 'catalog' && <CatalogTab />}
    </div>
  );
}
