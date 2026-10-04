import React, { useState } from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { Actors, Conversations, Messages } from '../collections.js';
import { LoginScreen } from './LoginScreen.jsx';
import { ChatPane } from './ChatPane.jsx';
import { Roster } from './Roster.jsx';
import { OrgChart } from './OrgChart.jsx';
import { Admin } from './Admin.jsx';
import { UsageDashboard } from './UsageDashboard.jsx';
import { ModelsDashboard } from './ModelsDashboard.jsx';
import { ApprovalsPane, MailPane } from './MailPane.jsx';
import { SecretsPane } from './SecretsPane.jsx';
import { FilesPane } from './FilesPane.jsx';
import { AgentsPane } from './AgentsPane.jsx';
import { HostsPane } from './HostsPane.jsx';

export function App() {
  const user = useTracker(() => Meteor.user(), []);
  const loggingIn = useTracker(() => Meteor.loggingIn(), []);
  const [tab, setTab] = useState('chat');

  const actor = useTracker(() => {
    if (!user) return null;
    Meteor.subscribe('actors');
    return Actors.findOne({ username: user.username });
  }, [user]);

  const isAdmin = !!(user && user.isAdmin);

  if (loggingIn) return <div className="loading">loading…</div>;
  if (!user) return <LoginScreen />;

  return (
    <div className="app-root">
      <div className="topbar">
        <span className="brand">🌳 TreeGent</span>
        <nav>
          <button className={`tab ${tab === 'chat' ? 'on' : ''}`} onClick={() => setTab('chat')}>Chat</button>
          <button className={`tab ${tab === 'roster' ? 'on' : ''}`} onClick={() => setTab('roster')}>Roster</button>
          <button className={`tab ${tab === 'org' ? 'on' : ''}`} onClick={() => setTab('org')}>Org</button>
          {isAdmin && <button className={`tab ${tab === 'admin' ? 'on' : ''}`} onClick={() => setTab('admin')}>Admin</button>}
          {isAdmin && <button className={`tab ${tab === 'usage' ? 'on' : ''}`} onClick={() => setTab('usage')}>Usage</button>}
          {isAdmin && <button className={`tab ${tab === 'models' ? 'on' : ''}`} onClick={() => setTab('models')}>Models</button>}
          {isAdmin && <button className={`tab ${tab === 'hosts' ? 'on' : ''}`} onClick={() => setTab('hosts')}>Hosts</button>}
          <button className={`tab ${tab === 'approvals' ? 'on' : ''}`} onClick={() => setTab('approvals')}>Approvals</button>
          <button className={`tab ${tab === 'mail' ? 'on' : ''}`} onClick={() => setTab('mail')}>Mail</button>
          <button className={`tab ${tab === 'secrets' ? 'on' : ''}`} onClick={() => setTab('secrets')}>Secrets</button>
          <button className={`tab ${tab === 'files' ? 'on' : ''}`} onClick={() => setTab('files')}>Files</button>
          <button className={`tab ${tab === 'agents' ? 'on' : ''}`} onClick={() => setTab('agents')}>Agents</button>
        </nav>
        <span className="me">
          {actor ? `${actor.display_name} · ${actor.kind}` : user.username}
          <button className="logout" onClick={() => Meteor.logout()}>log out</button>
        </span>
      </div>
      {tab === 'chat' && <ChatPane me={actor} />}
      {tab === 'roster' && <Roster me={actor} />}
      {tab === 'org' && <OrgChart />}
      {tab === 'admin' && isAdmin && <Admin />}
      {tab === 'usage' && isAdmin && <UsageDashboard />}
      {tab === 'models' && isAdmin && <ModelsDashboard />}
      {tab === 'approvals' && <ApprovalsPane />}
      {tab === 'mail' && <MailPane />}
      {tab === 'secrets' && <SecretsPane />}
      {tab === 'files' && <FilesPane />}
      {tab === 'agents' && <AgentsPane />}
      {tab === 'hosts' && isAdmin && <HostsPane />}
    </div>
  );
}
