"""Poll for the LATEST turn (not reply) — prints when a new turn starts
after a given time, then its final + any chat replies it produced."""
import sys
import time
from datetime import datetime

from pymongo import MongoClient

db = MongoClient("mongodb://127.0.0.1:27017/?directConnection=true")["treegent"]
CID = "dm_agt_a9f96a657d2d0962__hum_3b1a86e25fb16826"
SINCE = datetime.strptime(sys.argv[1], "%H%M") if len(sys.argv) > 1 else datetime(2026, 10, 3, 12, 40)
SINCE = SINCE.replace(year=2026, month=10, day=3)

deadline = 240
waited = 0
while waited < deadline:
    t = db.runtime_turns.find_one({"started": {"$gt": SINCE}},
                                  sort=[("started", -1)])
    if t and t["steps"] > 0:
        # wait for turn completion (final set / ended present)
        if t.get("ended"):
            print(f"[{waited}s] turn {t['started'].strftime('%H:%M:%S')} "
                  f"trigger={t['trigger']} steps={t['steps']}")
            print("final:", (t.get("final") or "")[:300])
            ms = list(db.messages.find({
                "conversation_id": CID,
                "sender_id": "agt_a9f96a657d2d0962",
                "created_at": {"$gt": t["started"]},
            }).sort("created_at", 1))
            for m in ms:
                print("replied:", m["body"][:250])
            break
    time.sleep(10)
    waited += 10
else:
    print("no new completed turn within", deadline, "s")
