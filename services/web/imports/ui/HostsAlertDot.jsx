import React, { useEffect, useState } from 'react';
import { Meteor } from 'meteor/meteor';

/**
 * R71: red dot on the Hosts tab when fleet versions drift.
 *
 * Two separate states, same dot:
 *  - central server itself behind origin/main (central_behind > 0) — the
 *    shared box needs `treegent update`; this is the state where every
 *    host could be "equal to central" yet the whole fleet is stale.
 *  - any single host's host_version_short differing from central_version
 *    (only for reachable hosts with a known version — 'none'/blank while
 *    provisioning is not a mismatch).
 *
 * Polls every 60s (aligned with the runtime's host-health loop). Renders
 * nothing at all when everything matches, so a dot-free navbar is the
 * healthy state. Admin-only by construction (tg.hosts.list requires it);
 * a non-admin or a failing call also renders nothing — the Hosts pane
 * itself is the place to see errors, the dot must never cry wolf.
 */
export function HostsAlertDot() {
  const [state, setState] = useState('ok');   // ok | behind

  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const r = await Meteor.callAsync('tg.hosts.list');
        if (!alive) return;
        const centralBehind = (r.central_behind || 0) > 0;
        const cv = r.central_version || '';
        const staleHost = (r.hosts || []).some((h) => {
          const v = h.host_version_short || '';
          return v && v !== 'none' && cv && v !== cv;
        });
        setState((centralBehind || staleHost) ? 'behind' : 'ok');
      } catch (e) {
        if (alive) setState('ok');   // never a false alarm
      }
    };
    check();
    const iv = setInterval(check, 60000);
    return () => { alive = false; clearInterval(iv); };
  }, []);

  if (state !== 'behind') return null;
  return (
    <span
      className="hosts-alert-dot"
      title="version drift: a host or the central server is not on the latest version — open Hosts to update"
    />
  );
}
