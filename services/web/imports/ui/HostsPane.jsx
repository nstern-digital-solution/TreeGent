import React, { useEffect, useState } from 'react';
import { Meteor } from 'meteor/meteor';

export function HostsPane() {
  const [hosts, setHosts] = useState([]);
  const [pubkey, setPubkey] = useState('');
  const [form, setForm] = useState({ name: '', address: '', port: 22, ssh_user: 'root' });
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);

  const load = () => Meteor.callAsync('tg.hosts.list').then((r) => setHosts(r.hosts || []));
  useEffect(() => {
    load();
    Meteor.callAsync('tg.hosts.pubkey').then((r) => setPubkey(r.pubkey || ''));
  }, []);

  const add = async () => {
    setMsg('');
    try {
      await Meteor.callAsync('tg.hosts.add', form.name, form.address, Number(form.port), form.ssh_user);
      setForm({ name: '', address: '', port: 22, ssh_user: 'root' });
      load();
    } catch (e) { setMsg(e.message); }
  };

  const provision = async (id) => {
    setMsg('provisioning — this takes a few minutes (deps + venv + service)...');
    setBusy(true);
    try {
      const r = await Meteor.callAsync('tg.hosts.provision', id);
      setMsg(r.ok ? '✓ provisioned — runtime active on the host'
                  : `failed: ${(r.log || r.stderr || '').slice(0, 300)}`);
      load();
    } catch (e) { setMsg(e.message); }
    setBusy(false);
  };

  const oneLiner = pubkey
    ? `echo "${pubkey}" >> ~/.ssh/authorized_keys`
    : '(generating key...)';

  return (
    <div className="pane">
      <h3>Agent hosts</h3>
      <p className="muted">
        Machines that run your agents. Add the address, authorize the central box
        once (one command on the host), then provision — everything else is
        automatic: exec user, repo, service.
      </p>

      <p className="muted">One-time on each host — authorize the central box:</p>
      <code className="key-reveal" style={{ display: 'block', marginBottom: 12 }}>
        {oneLiner}
      </code>

      <table className="usage-table">
        <thead><tr><th>Name</th><th>Address</th><th>SSH</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {hosts.map((h) => (
            <tr key={h.id}>
              <td>{h.name}</td>
              <td className="mono">{h.address}</td>
              <td className="mono">{h.ssh_user}@:{h.port}</td>
              <td>{h.status === 'active' ? '✓ active' : h.status}</td>
              <td>
                <button className="btn small" disabled={busy}
                        onClick={() => provision(h.id)}>
                  {h.status === 'active' ? 're-provision' : 'provision'}
                </button>
              </td>
            </tr>
          ))}
          {!hosts.length && <tr><td colSpan={5} className="muted">no hosts yet</td></tr>}
        </tbody>
      </table>
      {hosts.some((h) => h.status !== 'active' && h.last_log) && (
        <pre className="muted" style={{ whiteSpace: 'pre-wrap' }}>
          {hosts.find((h) => h.last_log)?.last_log}
        </pre>
      )}

      <h4 style={{ marginTop: 16 }}>Add host</h4>
      <input placeholder="name (e.g. agent-1)" value={form.name}
             onChange={(e) => setForm({ ...form, name: e.target.value })} />
      <input placeholder="address (IP or domain — dyn-dns works)" value={form.address}
             onChange={(e) => setForm({ ...form, address: e.target.value })} />
      <input placeholder="ssh port" type="number" value={form.port}
             onChange={(e) => setForm({ ...form, port: e.target.value })} />
      <input placeholder="ssh user (root or sudo-capable)" value={form.ssh_user}
             onChange={(e) => setForm({ ...form, ssh_user: e.target.value })} />
      <button onClick={add}>add</button>
      {msg && <p className="error">{msg}</p>}
    </div>
  );
}
