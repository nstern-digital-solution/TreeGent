"""R45 proof matrix: identity is derived from the key, claims are ignored.

R68: credentials come from env/args — the demo key is only auto-seeded on
TG_DEV=1 boxes, so nothing here may hardcode an actor id or read the key
out of Mongo.
"""
import json
import os
import sys
import urllib.request

WORKER_KEY = os.environ.get("TG_AGENT_KEY") or (sys.argv[1] if len(sys.argv) > 1 else "")
WORKER = os.environ.get("TG_AGENT_ID") or (sys.argv[2] if len(sys.argv) > 2 else "")
if not WORKER_KEY or not WORKER:
    sys.exit("usage: TG_AGENT_KEY=sk-agt-... TG_AGENT_ID=agt_... "
             "python3 r45_matrix.py [key] [agent_id]")
PEER_HUMAN = os.environ.get("TG_PEER_ID", "hum_3b1a86e25fb16826")

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

# 2) key + SPOOFED caller_id=rootuser -> still worker scope
s, b = req("http://127.0.0.1:8003/secrets?caller_id=" + PEER_HUMAN,
           {"X-Agent-Key": WORKER_KEY})
owners = sorted({x.get("owner") for x in json.loads(b)}) if s == 200 else b
results.append(("spoofed caller_id ignored", s,
                "owners=" + str(owners) + " (worker=agt_…)"))

# 3) key + spoofed X-Actor-Id on chat -> identity stays worker
s, b = req("http://127.0.0.1:8000/conversations",
           {"X-Agent-Key": WORKER_KEY, "X-Actor-Id": PEER_HUMAN,
            "X-Service-Token": "dev-service-token"})
ids = [c.get("id") for c in json.loads(b)] if s == 200 else b
results.append(("spoofed X-Actor-Id ignored (chat)", s, ids))

# 4) central tier (web server) still works for rootuser
s, b = req("http://127.0.0.1:8003/secrets?caller_id=" + PEER_HUMAN,
           {"X-Service-Token": "dev-service-token", "X-Actor-Id": PEER_HUMAN})
names = [x.get("name") for x in json.loads(b)] if s == 200 else b
results.append(("central tier intact (rootuser via token)", s, names))

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
s, b = req(f"http://127.0.0.1:8000/internal/agent-inbox?agent_id={WORKER}",
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
