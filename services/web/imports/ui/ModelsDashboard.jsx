import React, { useEffect, useState } from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { TaskClasses, Providers, ModelCatalog } from '../proxyCollections.js';

const call = (path, method, body) => Meteor.callAsync('proxy.admin', path, method, body);
const fmtUsd1M = (p) => (p == null ? '—' : `$${p.toFixed(2)}/M`);

function ProvidersTab() {
  const { providers, ready } = useTracker(() => {
    const s = Meteor.subscribe('proxyProviders');
    return { providers: Providers.find().fetch(), ready: s.ready() };
  }, []);
  // readiness is computed on the proxy (env access) — pull via admin API
  const [readyById, setReadyById] = useState({});
  const [form, setForm] = useState({ id: '', base_url: 'https://', key_env: '', kind: 'openai-compat' });
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
          kind: form.kind, base_url: form.base_url, key_env: form.key_env, enabled: true,
        });
        setForm({ id: '', base_url: 'https://', key_env: '', kind: 'openai-compat' });
        return `Provider ${form.id} saved. Run "Refresh catalog" to pull its models.`;
      }); }} className="provider-form">
        <input placeholder="name (lowercase id)" value={form.id}
               onChange={(e) => setForm({ ...form, id: e.target.value })} />
        <input placeholder="https://api.example.com/v1" value={form.base_url}
               onChange={(e) => setForm({ ...form, base_url: e.target.value })} />
        <input placeholder="API key env var name (e.g. MYPROVIDER_API_KEY)" value={form.key_env}
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
  const [form, setForm] = useState({ id: '', max_price_out: '8', requires: '' });
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);

  const run = async (fn) => {
    setMsg(null); setErr(null);
    try { setMsg(await fn()); } catch (ex) { setErr(ex.reason || ex.message); }
  };

  return (
    <div>
      <h3>Task classes</h3>
      {msg && <p className="ok-msg">{msg}</p>}
      {err && <p className="err-msg">{err}</p>}
      <table className="usage-table">
        <thead><tr><th>Class</th><th>Criteria</th><th>Default</th><th /></tr></thead>
        <tbody>
          {classes.map((c) => (
            <tr key={c._id}>
              <td>{c._id}</td>
              <td className="mono">{JSON.stringify(c.criteria)}</td>
              <td>{c.default ? 'yes' : ''}</td>
              <td>{!c.default && (
                <button className="btn small danger" onClick={() => run(async () => {
                  await call(`/admin/classes/${encodeURIComponent(c._id)}`, 'DELETE');
                  return `Class ${c._id} deleted`;
                })}>delete</button>
              )}</td>
            </tr>
          ))}
          {ready && classes.length === 0 && (
            <tr><td colSpan={4} className="muted">No classes — the proxy seeds four defaults on startup.</td></tr>
          )}
        </tbody>
      </table>
      <h4>Add / update class</h4>
      <form onSubmit={(e) => { e.preventDefault(); run(async () => {
        await call(`/admin/classes/${encodeURIComponent(form.id)}`, 'PUT', {
          description: '',
          criteria: {
            max_price_out: form.max_price_out === '' ? null : Number(form.max_price_out),
            requires: form.requires.split(',').map((s) => s.trim()).filter(Boolean),
          },
          models: null,
          default: false,
        });
        setForm({ id: '', max_price_out: '8', requires: '' });
        return `Class ${form.id} saved.`;
      }); }} className="provider-form">
        <input placeholder="class name (e.g. deep-research)" value={form.id}
               onChange={(e) => setForm({ ...form, id: e.target.value })} />
        <input placeholder="max $/1M output tokens (blank = none)" value={form.max_price_out}
               onChange={(e) => setForm({ ...form, max_price_out: e.target.value })} />
        <input placeholder="required caps, comma-sep (e.g. vision, reasoning)" value={form.requires}
               onChange={(e) => setForm({ ...form, requires: e.target.value })} />
        <button className="btn" type="submit">Save class</button>
      </form>
    </div>
  );
}

function CatalogTab() {
  const { models, ready } = useTracker(() => {
    const s = Meteor.subscribe('proxyCatalog', 200);
    // nulls (dynamic/unknown pricing) sort after priced models
    const sorted = ModelCatalog.find().fetch()
      .sort((a, b) => (a.price_out ?? Infinity) - (b.price_out ?? Infinity));
    return { models: sorted, ready: s.ready() };
  }, []);
  return (
    <div>
      <h3>Model catalog ({models.length} listed)</h3>
      <p className="muted small">Cheapest first. Live from the providers&apos; /models endpoints — refresh pulls anew.</p>
      <table className="usage-table">
        <thead><tr><th>Model</th><th>Provider</th><th>$/1M in</th><th>$/1M out</th><th>Context</th><th>Caps</th></tr></thead>
        <tbody>
          {models.map((m) => (
            <tr key={m._id}>
              <td className="mono">{m._id}</td>
              <td>{m.provider}</td>
              <td>{fmtUsd1M(m.price_in)}</td>
              <td>{fmtUsd1M(m.price_out)}</td>
              <td>{m.ctx ? m.ctx.toLocaleString() : '—'}</td>
              <td>{(m.caps || []).join(', ')}</td>
            </tr>
          ))}
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
