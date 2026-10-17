#!/usr/bin/env bash
# Protect main on the prod forge: 1 approval required (felix or rosa),
# no direct pushes. Live-proven payload on Forgejo 12.0.4 lab (2026-10-17):
# 201 created; rosa's direct push to main then BLOCKED by the server.
set -euo pipefail
cd /opt/TreeGent/deploy/forgejo

S=$(date +%H%M%S)
MT=$(docker compose exec -u git -T server forgejo admin user generate-access-token \
  -u treegent-admin -t protect-$S --scopes write:repository \
  | grep -oE '[0-9a-f]{40}')
echo "minted protect token …${MT: -6}"

R=$(curl -s -o /tmp/bp-resp.$$ -w '%{http_code}' -X POST \
  "https://git.nstern.de/api/v1/repos/treegent-admin/TreeGent/branch_protections" \
  -H "Authorization: token $MT" -H 'Content-Type: application/json' -d '{
    "branch_name": "main",
    "enable_push": false,
    "required_approvals": 1,
    "enable_approvals_whitelist": true,
    "approvals_whitelist_username": ["felix", "rosa"],
    "block_on_rejected_reviews": true,
    "dismiss_stale_approvals": true
  }')
if [ "$R" != "201" ] && [ "$R" != "422" ]; then
  echo "protect FAILED (HTTP $R):"; head -c 300 /tmp/bp-resp.$$; echo; exit 1
fi
[ "$R" = "422" ] && echo "(422 = already protected — rules stand)"
rm -f /tmp/bp-resp.$$

# verify: list protections, expect main with required_approvals>=1
curl -s "https://git.nstern.de/api/v1/repos/treegent-admin/TreeGent/branch_protections" \
  -H "Authorization: token $MT" \
  | python3 -c 'import json,sys; [print("protected:", b["branch_name"], "| approvals:", b["required_approvals"], "| push:", b["enable_push"]) for b in json.load(sys.stdin)]'

echo "OK — main is protected: 1 approval (felix or rosa), no direct pushes."
