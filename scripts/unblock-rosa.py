#!/usr/bin/env python3
"""One-shot: migrate TreeGent to the self-hosted forge and unblock Rosa.

Does everything, prints progress, fails loudly:
  1. finds a VALID GitHub token in agent-1's (Rosa's) secrets scope
     (falls back to $GH env var if given; aborts with instructions if
     neither works)
  2. mints fresh forge tokens (migrate + rosa) via the container CLI
  3. pre-cleans any half-migrated repo shell, migrates the full repo
     (private, issues, PRs, labels, milestones, releases, comments)
  4. grants rosa write collaborator on the repo (private repos 404 for
     non-collaborators even with a valid token)
  5. writes forge/token into Rosa's secrets scope
  6. proves it: clones the repo with rosa's token through git.nstern.de

Run on the central box:
    .venv/bin/python3 scripts/unblock-rosa.py          # token from secrets
    GH=ghp_xxx .venv/bin/python3 scripts/unblock-rosa.py   # explicit token

stdlib only. Never prints token values (lengths/prefixes only).
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

ENV_FILE = os.environ.get("ENV_FILE", "/etc/treegent/env")
FORGE_API = os.environ.get("TG_FORGE_LOCAL",
                           "http://127.0.0.1:3001/api/v1") + "/api/v1"
SECRETS = os.environ.get("TG_SECRETS_URL",
                           "http://127.0.0.1:8003")
FORGE_PUBLIC = os.environ.get("TG_FORGE_PUBLIC", "https://git.nstern.de")
COMPOSE_DIR = os.environ.get("COMPOSE_DIR",
                             "/opt/TreeGent/deploy/forgejo")
STAMP = subprocess.run(["date", "+%H%M%S"], capture_output=True,
                       text=True).stdout.strip()


def die(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def load_env():
    env = {}
    for ln in open(ENV_FILE):
        ln = ln.strip()
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            env[k.strip()] = v.strip().strip('"')
    return env


def http(method, url, headers=None, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if data:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")[:300]
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw}
    except Exception as e:
        return 0, {"message": str(e)}


def main():
    env = load_env()
    st = env.get("TG_SERVICE_TOKEN") or os.environ.get("TG_SERVICE_TOKEN")
    if not st:
        die("TG_SERVICE_TOKEN not in /etc/treegent/env")

    # ---- rosa's actor id (mongo, stdlib) ----
    import pymongo  # noqa: present in the central venv
    mongo_url = env.get("TG_MONGO_URL", "mongodb://127.0.0.1:27017")
    if mongo_url.startswith("mongodb+srv://"):
        die("SRV mongo URL — set TG_MONGO_URL to the direct host form")
    client = pymongo.MongoClient(mongo_url)
    rosa = client.treegent.actors.find_one({"username": "agent-1"})
    if not rosa:
        die("no actor with username agent-1")
    aid = str(rosa["_id"])
    print(f"rosa actor: {aid} ({rosa.get('display_name')})")

    # ---- 1. a valid GitHub token ----
    gh = os.environ.get("GH", "")
    if not gh:
        hdr = {"X-Service-Token": st, "X-Actor-Id": aid}
        code, secs = http("GET", SECRETS + "/secrets?q=github",
                          headers=hdr)
        if code != 200:
            die(f"secrets list {code}: {secs}")
        for s in secs if isinstance(secs, list) else []:
            c2, val = http("GET",
                           SECRETS + f"/secrets/{s['id']}/value",
                           headers=hdr)
            tok = (val or {}).get("value", "")
            if not tok:
                continue
            c3, _ = http("GET", "https://api.github.com/rate_limit",
                         headers={"Authorization": f"Bearer {tok}",
                                  "User-Agent": "TreeGent-migrate"})
            if c3 == 200:
                gh = tok
                print(f"github token: from secret '{s['name']}' — VALID")
                break
            print(f"github token: secret '{s['name']}' invalid ({c3})")
    if not gh:
        die("no valid GitHub token in Rosa's secrets. Create a classic PAT "
            "(no scopes, github.com/settings/tokens) and re-run: "
            "GH=ghp_xxx .venv/bin/python3 scripts/unblock-rosa.py")
    c, _ = http("GET", "https://api.github.com/rate_limit",
                headers={"Authorization": f"Bearer {gh}",
                         "User-Agent": "TreeGent-migrate"})
    if c != 200:
        die(f"provided GH token invalid ({c})")

    # ---- 2. mint forge tokens via container CLI ----
    def mint(user, tname, scopes):
        out = subprocess.run(
            ["docker", "compose", "exec", "-u", "git", "-T", "server",
             "forgejo", "admin", "user", "generate-access-token",
             "-u", user, "-t", tname, "--scopes", scopes],
            cwd=COMPOSE_DIR, capture_output=True, text=True)
        m = re.search(r"[0-9a-f]{40}", out.stdout + out.stderr)
        if out.returncode != 0 or not m:
            die(f"mint {user}/{tname}: {out.stdout} {out.stderr}")
        return m.group(0)

    mig = mint("treegent-admin", f"mig-{STAMP}",
               "write:repository,read:user")
    rt = mint("rosa", f"rosa-{STAMP}",
              "write:repository,write:issue,read:user")
    print(f"minted: migrate=…{mig[-6:]} rosa=…{rt[-6:]}")

    # ---- 3. pre-clean + migrate ----
    ah = {"Authorization": f"token {mig}"}
    code, body = http("DELETE",
                      f"{FORGE_API}/repos/treegent-admin/TreeGent",
                      headers=ah)
    print(f"pre-clean: {code} (404 = nothing there)")
    payload = {
        "clone_addr": "https://github.com/nstern-digital-solution/TreeGent.git",
        "auth_token": gh,
        "mirror": False,
        "repo_owner": "treegent-admin",
        "repo_name": "TreeGent",
        "private": True,
        "issues": True, "pull_requests": True, "labels": True,
        "milestones": True, "releases": True, "comments": True,
        "wiki": False, "lfs": False, "service": "github",
    }
    print("migrating (clone + issues + PRs — takes a minute)…")
    code, body = http("POST", f"{FORGE_API}/repos/migrate", headers=ah,
                      body=payload, timeout=1800)
    if code != 201:
        die(f"migrate {code}: {body}")
    print(f"migrated: {body.get('full_name')} private={body.get('private')}")

    # ---- 4. rosa as write collaborator ----
    code, body = http(
        "PUT", f"{FORGE_API}/repos/treegent-admin/TreeGent/collaborators/rosa",
        headers=ah, body={"permission": "write"})
    if code not in (200, 201, 204):
        die(f"collaborator grant {code}: {body}")
    print("collaborator grant: OK")

    # ---- 5. forge/token into rosa's secrets ----
    hdr = {"X-Service-Token": st, "X-Actor-Id": aid}
    code, body = http(
        "POST", SECRETS + "/secrets", headers=hdr,
        body={"name": "forge/token", "value": rt,
              "url": FORGE_PUBLIC, "username": "rosa",
              "notes": f"forge credential {STAMP}"})
    if code not in (200, 201):
        die(f"secret write {code}: {body}")
    print("secret forge/token for rosa: written")

    # ---- 6. prove it: clone through the public URL ----
    subprocess.run(["rm", "-rf", "/tmp/rosa-check"])
    clone = subprocess.run(
        ["git", "clone", "-q",
         f"{FORGE_PUBLIC.replace('://', '://rosa:' + rt + '@')}"
         "/treegent-admin/TreeGent.git",
         "/tmp/rosa-check"], capture_output=True, text=True)
    if clone.returncode != 0:
        die(f"clone check failed: {clone.stderr[:300]}")
    head = subprocess.run(["git", "-C", "/tmp/rosa-check", "log",
                           "--oneline", "-1"], capture_output=True, text=True)
    subprocess.run(["rm", "-rf", "/tmp/rosa-check"])
    print(f"ROSA-CLONE-OK: {head.stdout.strip()[:70]}")
    print("\n== done — tell Rosa: your forge credential is in your secrets "
          "scope (forge/token). Clone, push the #74 branch, open the PR. ==")


if __name__ == "__main__":
    main()
