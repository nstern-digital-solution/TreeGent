#!/usr/bin/env bash
# TreeGent unbrick: username back to your stable IDs (agent-N).
set -euo pipefail
cd "${TG_REPO_DIR:-/opt/TreeGent}"
SHA="0ff362e5375a8f85b2a6cf4d4c580beca9d9ba2ea9ef2f45b922d19c3c42d9e3"
TMP="$(mktemp)"
cat > "$TMP" <<'PYEOF'
import os, pymongo
env = {}
for ln in open(os.environ.get("TG_ENV_FILE", "/etc/treegent/env")):
    ln = ln.strip()
    if "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        env[k.strip()] = v.strip()
MONGO = env.get("TG_MONGO_URL", "mongodb://127.0.0.1:27017/?directConnection=true")
db = pymongo.MongoClient(MONGO).treegent
n = 0
for a in list(db.actors.find({"kind": "agent", "renamed_from": {"$exists": True}})):
    db.actors.update_one({"_id": a["_id"]},
                         {"$set": {"username": a["renamed_from"]},
                          "$unset": {"renamed_from": "", "renamed_at": ""}})
    print("restored", a["renamed_from"], "- display:", a.get("display_name"))
    n += 1
print("done,", n, "actor(s) re-keyed")
PYEOF
GOT="$(printf '%s\n' "$(cat "$TMP")" | sha256sum | cut -d' ' -f1)"
if [ "$GOT" != "$SHA" ]; then
    echo "INTEGRITY FAIL (sha $GOT)"
    exit 1
fi
echo "integrity OK — running"
.venv/bin/python3 "$TMP"
