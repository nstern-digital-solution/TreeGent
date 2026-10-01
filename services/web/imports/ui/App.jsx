import React, { useState } from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { Actors, Conversations, Messages } from '../collections.js';
import { LoginScreen } from './LoginScreen.jsx';
import { ChatPane } from './ChatPane.jsx';
import { Roster } from './Roster.jsx';
import { OrgChart } from './OrgChart.jsx';
import { Admin } from './Admin.jsx';

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
    </div>
  );
}
