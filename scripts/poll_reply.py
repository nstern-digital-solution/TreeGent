"""Live poll: wait for the worker agent to reply in the peer DM after a
given time, print the reply + latest turn.

R68: the agent/peer come from env/args (no hardcoded demo actor id — the
demo key is only auto-seeded on TG_DEV=1 boxes).
"""
import os
import sys
from datetime import datetime
from pymongo import MongoClient
db = MongoClient("mongodb://127.0.0.1:27017/?directConnection=true")["treegent"]
AGENT_ID = os.environ.get("TG_AGENT_ID") or (sys.argv[2] if len(sys.argv) > 2 else "")
PEER_ID = os.environ.get("TG_PEER_ID", "hum_3b1a86e25fb16826")
if not AGENT_ID:
    sys.exit("usage: TG_AGENT_ID=agt_... [TG_PEER_ID=hum_...] python3 poll_reply.py [agent_id]")
CID = f"dm_{AGENT_ID}__{PEER_ID}"
SINCE = datetime(2026, 10, 3, 12, 35)

deadline = 180
waited = 0
while waited < deadline:
    ms = list(db.messages.find({
        "conversation_id": CID,
        "sender_id": AGENT_ID,
        "created_at": {"$gt": SINCE},
    }).sort("created_at", -1).limit(1))
    if ms:
        print(f"[{waited}s] AGENT REPLIED IN CHAT: {ms[0]['body'][:400]}")
        break
    import time
    time.sleep(10)
    waited += 10
else:
    print("NO REPLY within", deadline, "s")

for t in db.runtime_turns.find().sort("started", -1).limit(1):
    print("turn:", t["started"].strftime("%H:%M:%S"), t["trigger"],
          "| steps:", t["steps"], "| final:", (t.get("final") or "")[:200])
