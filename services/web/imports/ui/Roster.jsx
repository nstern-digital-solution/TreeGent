import React from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { Actors } from '../collections.js';

export function Roster({ me }) {
  const actors = useTracker(() => {
    Meteor.subscribe('actors');
    return Actors.find({}, { sort: { kind: 1, username: 1 } }).fetch();
  }, []);

  const startDM = (id) => {
    Meteor.call('tg.openDM', id, (err) => { if (err) alert(err.message); });
  };

  return (
    <div className="page">
      <h2>Roster</h2>
      <p className="muted">Click DM to open (or reuse) a direct conversation.</p>
      <ul className="roster">
        {actors.filter((a) => a._id !== (me && me._id)).map((a) => (
          <li key={a._id}>
            <b>{a.display_name}</b>
            <span className="kind">{a.kind}</span>
            <span className="muted">@{a.username}</span>
            <button className="start-dm" onClick={() => startDM(a._id)}>DM</button>
          </li>
        ))}
      </ul>
    </div>
  );
}
