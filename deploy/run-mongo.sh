#!/usr/bin/env bash
# Start local MongoDB for M1 dev (single-node replica set → oplog for Meteor reactivity).
set -euo pipefail
cd "$(dirname "$0")"
MONGOD=~/opt/mongodb/bin/mongod
DBPATH="$(pwd)/mongo-data"
mkdir -p "$DBPATH"
if pgrep -f "mongod.*treegent" >/dev/null 2>&1; then
  echo "mongod (treegent) already running"
else
  "$MONGOD" --dbpath "$DBPATH" --bind_ip 127.0.0.1 --port 27017 \
    --replSet rs0 --logpath "$DBPATH/mongod.log" --fork \
    --pidfilepath "$DBPATH/mongod.pid"
  echo "mongod started (pid $(cat "$DBPATH/mongod.pid"))"
fi
# initiate replica set once (idempotent)
uv run --with pymongo python3 - <<'EOF'
from pymongo import MongoClient
c = MongoClient("mongodb://127.0.0.1:27017/?directConnection=true", serverSelectionTimeoutMS=3000)
try:
    st = c.admin.command("replSetGetStatus")
    print("replSet:", st.get("myState"), "(1=PRIMARY, already initiated)")
except Exception:
    c.admin.command("replSetInitiate", {"_id": "rs0", "members": [{"_id": 0, "host": "127.0.0.1:27017"}]})
    print("replSet initiated")
EOF
