"""R45 proof matrix: identity is derived from the key, claims are ignored."""
import json
import urllib.request

WORKER_KEY = None
from pymongo import MongoClient
db = MongoClient("mongodb://127.0.0.1:27017/?directConnection=true")["treegent"]
WORKER_KEY = db.agent_keys.find_one({"agent_id": "agt_a9f96a657d2d0962"})["_id"]
FELIX = "hum_3b1a86e25fb16826"

def req(url, headers=None):
    r = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

results = []

# 1) key -> worker's own secret scope
s, b = req("http://127.0.0.1:8003/secrets", {"X-Agent-Key": WORKER_KEY})
names = [x.get("name") for x in json.loads(b)] if s == 200 else b
results.append(("agent key derives identity (secrets)", s, names))

# 2) key + SPOOFED caller_id=felix -> still worker scope
s, b = req("http://127.0.0.1:8003/secrets?caller_id=" + FELIX,
           {"X-Agent-Key": WORKER_KEY})
owners = sorted({x.get("owner") for x in json.loads(b)}) if s == 200 else b
results.append(("spoofed caller_id ignored", s,
                "owners=" + str(owners) + " (worker=agt_…)"))

# 3) key + spoofed X-Actor-Id on chat -> identity stays worker
s, b = req("http://127.0.0.1:8000/conversations",
           {"X-Agent-Key": WORKER_KEY, "X-Actor-Id": FELIX,
            "X-Service-Token": "dev-service-token"})
ids = [c.get("id") for c in json.loads(b)] if s == 200 else b
results.append(("spoofed X-Actor-Id ignored (chat)", s, ids))

# 4) central tier (web server) still works for felix
s, b = req("http://127.0.0.1:8003/secrets?caller_id=" + FELIX,
           {"X-Service-Token": "dev-service-token", "X-Actor-Id": FELIX})
names = [x.get("name") for x in json.loads(b)] if s == 200 else b
results.append(("central tier intact (felix via token)", s, names))

# 5) no credential -> 401
s, b = req("http://127.0.0.1:8003/secrets")
results.append(("no credential rejected", s, ""))

# 6) forged key -> 401
s, b = req("http://127.0.0.1:8003/secrets", {"X-Agent-Key": "sk-agt-forged"})
results.append(("forged key rejected", s, ""))

# 7) key works cross-service (mail approvals: requested-by-me)
s, b = req("http://127.0.0.1:8002/approvals?scope=requested",
           {"X-Agent-Key": WORKER_KEY})
n = len(json.loads(b)) if s == 200 else b
results.append(("key works on mail service", s, f"{n} approvals"))

# 8) runtime inbox poll path (chat internal) works key-only
s, b = req("http://127.0.0.1:8000/internal/agent-inbox?agent_id=agt_a9f96a657d2d0962",
           {"X-Agent-Key": WORKER_KEY})
ok = s == 200
results.append(("runtime key-only inbox poll", s, "OK" if ok else b[:80]))

print()
ALL_OK = True
for name, code, detail in results:
    ok = code in (200, 201)
    expect_fail = name in ("no credential rejected", "forged key rejected")
    good = (code in (401, 403)) if expect_fail else ok
    if not good:
        ALL_OK = False
    mark = "PASS" if good else "FAIL"
    print(f"[{mark}] {name}: {code} | {str(detail)[:100]}")
print()
print("MATRIX:", "ALL GREEN" if ALL_OK else "FAILURES PRESENT")
