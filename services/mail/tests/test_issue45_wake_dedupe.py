"""Issue #45 regression: the pull-sync IMPORT path and the body-HEAL path
(#26 healed-then-wake) wrote wakes for the SAME mail under DIFFERENT id
conventions, so $setOnInsert never deduped them:

- import (sync_inbound) wrote inline   wke_pull_<id[:24]>   (truncated, no
  recipient suffix);
- heal (_heal_bodies -> _wake_pull) wrote
  wke_pull_<full provider id>_<recipient>   (#24/#34 convention).

Live fit: a backfill imported mail with bodies that failed to fetch -> a
later walk healed them -> the heal's differently-keyed wake sailed past the
import wake's dedupe -> a second full batch of wakes for mail the agent had
already read.

Both paths now go through _wake_pull, so import + heal produce exactly ONE
wake row per (mail, agent recipient). Fake provider + in-memory fake DB,
doubles imported from the #26 suite (same stub style, no Mongo, no network).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import adapters  # noqa: E402
from test_issue26_sync_hygiene import (  # noqa: E402
    AGT_A, AGT_B, MB_A, FakeProvider, mail, setup)


def test_import_wake_uses_full_pull_wake_id(monkeypatch):
    """A NEW imported mail wakes its owner under exactly _wake_pull's
    convention: wke_pull_<FULL provider id>_<owner> (the old inline form
    truncated the id to 24 chars and dropped the recipient)."""
    m = mail(0, "a@example.com")
    p = FakeProvider([m])
    p.bodies[m["id"]] = "the body"
    db = setup(monkeypatch, p, [MB_A], [AGT_A])

    r = asyncio.run(adapters.sync_inbound())
    assert r["imported"] == 1
    wakes = db.wake_events.docs
    assert len(wakes) == 1
    assert wakes[0]["_id"] == adapters.pull_wake_id(m["id"], "agt_a")
    assert wakes[0]["_id"] == f"wke_pull_{m['id']}_agt_a", \
        "full provider id + recipient, never a truncated prefix"
    assert wakes[0]["agent_id"] == "agt_a" and wakes[0]["reason"] == "mail"
    assert wakes[0]["consumed"] is False


def test_resync_same_mail_writes_no_second_wake(monkeypatch):
    """The same mail still listed on a later walk is skipped (per-mailbox
    dedupe) and its wake id is $setOnInsert-idempotent — one row forever."""
    m = mail(0, "a@example.com")
    p = FakeProvider([m])
    p.bodies[m["id"]] = "the body"
    db = setup(monkeypatch, p, [MB_A], [AGT_A])

    r1 = asyncio.run(adapters.sync_inbound())
    assert r1["imported"] == 1
    r2 = asyncio.run(adapters.sync_inbound())
    assert r2["imported"] == 0 and r2["skipped"] == 1
    assert len(db.mail_messages.docs) == 1
    assert len(db.wake_events.docs) == 1, \
        "re-sync must not add a wake row for already-imported mail"


def test_import_plus_heal_produces_one_wake_row(monkeypatch):
    """#45 core regression: import (body fetch fails) and the later heal
    (body arrives) used to write TWO differently-keyed wakes for the same
    mail. Unified through _wake_pull they share one id -> ONE row total."""
    m = mail(0, "a@example.com")
    p = FakeProvider([m])
    p.bodies[m["id"]] = Exception("resend body hiccup")
    db = setup(monkeypatch, p, [MB_A], [AGT_A])

    r1 = asyncio.run(adapters.sync_inbound())   # import: body fetch fails
    assert r1["imported"] == 1
    assert len(db.wake_events.docs) == 1, "import-path wake"
    import_id = db.wake_events.docs[0]["_id"]
    assert import_id == adapters.pull_wake_id(m["id"], "agt_a")

    p.bodies[m["id"]] = "the healed body"       # provider recovers
    r2 = asyncio.run(adapters.sync_inbound())   # heal path writes the body
    assert r2["backfilled"] == 1
    assert db.mail_messages.docs[0]["text"] == "the healed body"

    wakes = db.wake_events.docs
    assert len(wakes) == 1, \
        f"import+heal must dedupe onto ONE wake row, got {[w['_id'] for w in wakes]}"
    assert wakes[0]["_id"] == import_id == \
        adapters.pull_wake_id(m["id"], "agt_a")

    asyncio.run(adapters.sync_inbound())        # later walks add nothing
    assert len(db.wake_events.docs) == 1


def test_shared_mailbox_wakes_each_agent_member(monkeypatch):
    """Shared mailbox: the import path wakes every AGENT member with a
    per-recipient id (same convention as _wake_pull) — human members are
    filtered out."""
    m = mail(0, "s@example.com")
    p = FakeProvider([m])
    p.bodies[m["id"]] = "hi team"
    mb_s = {"_id": "mbx_s", "address": "s@example.com", "kind": "shared",
            "members": ["agt_a", "agt_b", "usr_h"]}
    db = setup(monkeypatch, p, [mb_s],
               [AGT_A, AGT_B, {"_id": "usr_h", "kind": "human"}])

    r = asyncio.run(adapters.sync_inbound())
    assert r["imported"] == 1
    wakes = sorted(db.wake_events.docs, key=lambda w: w["_id"])
    assert [w["_id"] for w in wakes] == [
        adapters.pull_wake_id(m["id"], "agt_a"),
        adapters.pull_wake_id(m["id"], "agt_b")], \
        f"one per-recipient wake per agent member, got {[w['_id'] for w in wakes]}"
    assert all(w["agent_id"] in ("agt_a", "agt_b") for w in wakes)
    assert all(w["reason"] == "mail" for w in wakes)
