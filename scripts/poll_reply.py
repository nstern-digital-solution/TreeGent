"""Live poll: wait for the worker agent to reply in the felix DM after a
given time, print the reply + latest turn."""
import sys
from datetime import datetime

from pymongo import MongoClient

db = MongoClient("mongodb://127.0.0.1:27017/?directConnection=true")["treegent"]
CID = "dm_agt_a9f96a657d2d0962__hum_3b1a86e25fb16826"
SINCE = datetime(2026, 10, 3, 12, 35)

deadline = 180
waited = 0
while waited < deadline:
    ms = list(db.messages.find({
        "conversation_id": CID,
        "sender_id": "agt_a9f96a657d2d0962",
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
