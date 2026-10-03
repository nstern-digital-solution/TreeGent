"""Dump the latest agent job transcript tail."""
from pymongo import MongoClient

db = MongoClient("mongodb://127.0.0.1:27017/?directConnection=true")["treegent"]
j = db.jobs.find_one({"class": "agent"}, sort=[("created_at", -1)])
print("job:", j["_id"])
for m in j["messages"][-10:]:
    if m.get("role") == "assistant" and m.get("tool_calls"):
        for c in m["tool_calls"]:
            print("CALL:", c["function"]["name"], "|",
                  str(c["function"].get("arguments"))[:130])
    elif m.get("role") == "tool":
        print("  RESULT:", m.get("content", "")[:200])
    else:
        print(m.get("role", "?").upper(), ":",
              (m.get("content") or "")[:120])
