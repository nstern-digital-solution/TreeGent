#!/usr/bin/env bash
# Import the GitHub repository into Forgejo (issues, PRs incl. discussion
# history, labels, milestones, releases). READ-ONLY against GitHub: this
# pulls via the GitHub API; nothing on the GitHub side is changed or
# deleted (ruling 2026-10-15).
#
# usage: ./migrate-from-github.sh <owner>/<repo> [target-owner]
#   GITHUB_TOKEN  read-only token (public repo: any token works)
#   TG_FORGE_DOMAIN  forge domain
#   FORGE_ADMIN_TOKEN  admin-scoped forge token (from bootstrap.sh)
set -euo pipefail
cd "$(dirname "$0")"

SRC="${1:?usage: migrate-from-github.sh <owner>/<repo> [target-owner]}"
TARGET_OWNER="${2:-treegent-admin}"
DOMAIN="${TG_FORGE_DOMAIN:?set TG_FORGE_DOMAIN}"
FORGE="https://$DOMAIN"
TOKEN="${FORGE_ADMIN_TOKEN:?set FORGE_ADMIN_TOKEN (bootstrap.sh mints it)}"
GH="${GITHUB_TOKEN:?set GITHUB_TOKEN to a read-only GitHub token}"

# Forgejo migrate API: creates the repo server-side from the GitHub source
curl -sf -X POST "$FORGE/api/v1/repos/migrate" \
    -H "Authorization: token $TOKEN" \
    -H "Content-Type: application/json" \
    -d @- <<JSON
{
  "clone_addr": "https://github.com/$SRC.git",
  "auth_token": "$GH",
  "mirror": false,
  "repo_owner": "$TARGET_OWNER",
  "repo_name": "${SRC#*/}",
  "private": false,
  "issues": true,
  "pull_requests": true,
  "labels": true,
  "milestones": true,
  "releases": true,
  "comments": true,
  "wiki": false,
  "lfs": false,
  "service": "github"
}
JSON

echo
echo "migration queued — watch $FORGE/$TARGET_OWNER/${SRC#*/}"
echo "verify (run before any GitHub-side decision):"
echo "  branches:  git ls-remote $FORGE/$TARGET_OWNER/${SRC#*/}.git | wc -l   vs GitHub"
echo "  issues:    curl -s $FORGE/api/v1/repos/$TARGET_OWNER/${SRC#*/}/issues?limit=1 -I   (200 + total in Link header)"
echo "  prs:       curl -s $FORGE/api/v1/repos/$TARGET_OWNER/${SRC#*/}/pulls?limit=1 -I"
echo "GitHub repo left untouched."
