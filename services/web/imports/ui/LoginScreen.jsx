import React, { useEffect, useState } from 'react';
import { Meteor } from 'meteor/meteor';

export function LoginScreen() {
  const [bootstrapping, setBootstrapping] = useState(null); // null = unknown
  const [error, setError] = useState('');
  const [display, setDisplay] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  useEffect(() => {
    Meteor.call('tg.userCount', (err, n) => {
      if (!err) setBootstrapping(n === 0);
      else setBootstrapping(false);
    });
  }, []);

  const submit = (e) => {
    e.preventDefault();
    setError('');
    if (bootstrapping) {
      Meteor.call('tg.bootstrapAdmin', username.trim(), password, display.trim(), (err) => {
        if (err) return setError(err.message);
        Meteor.loginWithPassword(username.trim(), password);
      });
    } else {
      Meteor.loginWithPassword(username.trim(), password, (err) => {
        if (err) setError(err.reason || err.message);
      });
    }
  };

  return (
    <div className="login-wrap">
      <div className="login-card">
        <h1>🌳 TreeGent</h1>
        <p className="muted">Autonomous agent company</p>
        {bootstrapping === null ? <p className="muted">…</p> : (
          <form className="login-form" onSubmit={submit}>
            {bootstrapping && (
              <input value={display} onChange={(e) => setDisplay(e.target.value)}
                placeholder="Your name (e.g. the operator)" required />
            )}
            <input value={username} onChange={(e) => setUsername(e.target.value)}
              placeholder="username" required />
            <input value={password} onChange={(e) => setPassword(e.target.value)}
              type="password" placeholder="password" required />
            <button>{bootstrapping ? 'Create first admin account' : 'Sign in'}</button>
            {error && <p className="error">{error}</p>}
          </form>
        )}
      </div>
    </div>
  );
}
