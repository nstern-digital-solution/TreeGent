import React from 'react';
import { Meteor } from 'meteor/meteor';
import { useTracker } from 'meteor/react-meteor-data';
import { Actors } from '../collections.js';

export function OrgChart() {
  const actors = useTracker(() => {
    Meteor.subscribe('actors');
    return Actors.find({}, { sort: { username: 1 } }).fetch();
  }, []);

  const setParent = (actorId, parentId) => {
    Meteor.call('tg.setParent', actorId, parentId || null, (err) => {
      if (err) alert(err.message);
    });
  };

  const sorted = [...actors].sort((a, b) =>
    ((a.org && a.org.depth) || 0) - ((b.org && b.org.depth) || 0));

  return (
    <div className="page">
      <h2>Org chart</h2>
      <p className="muted">Re-parent with the select; sub-trees follow automatically.</p>
      <ul className="org">
        {sorted.map((a) => (
          <li key={a._id}>
            <span className="indent">{'— '.repeat((a.org && a.org.depth) || 0)}</span>
            <b>{a.display_name}</b>
            <span className="kind">{a.kind}</span>
            <select
              className="parent-select"
              value={(a.org && a.org.parent_id) || ''}
              onChange={(e) => setParent(a._id, e.target.value)}>
              <option value="">— root —</option>
              {actors.filter((x) => x._id !== a._id).map((x) => (
                <option key={x._id} value={x._id}>{x.display_name}</option>
              ))}
            </select>
          </li>
        ))}
      </ul>
    </div>
  );
}
