"""Agent-host provisioning over SSH (R53).

The central box owns an ed25519 keypair dedicated to agent-host provisioning
(TG_SSH_KEY, generated at install). The operator adds a host in the web UI
(address + port + ssh user) and runs ONE command on the agent box to
authorize the central box's PUBLIC key. The central service then SSHes in
and provisions: tgexec user (R46), repo, venv, systemd unit running the
runtime daemon with exec ENABLED, agents claimed per host.

Secret handling: the private key and mongo URL never enter Mongo or the UI.
They live in the central box env and are injected at provision time only.
"""
from __future__ import annotations

import asyncio
import base64
import os
import secrets
import subprocess

from treegent_common.hostauth import new_host_key, hash_host_key
import importlib.util
import sys

from .config import db, settings


def _host_keypair(host_id: str) -> tuple[str, str]:
    """R53 (per-host keys): ONE ed25519 keypair PER agent host, named by
    host id. Private halves live ONLY on the central box filesystem —
    never in Mongo, never in the UI. Deleting a host revokes its key
    (the file is removed); a leaked key exposes exactly one machine."""
    keydir = os.environ.get("TG_SSH_KEYDIR") or os.path.expanduser(
        "~/.treegent/agenthosts")
    os.makedirs(keydir, mode=0o700, exist_ok=True)
    priv = os.path.join(keydir, f"{host_id}_ed25519")
    pub = priv + ".pub"
    if not os.path.exists(priv):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                        "-C", f"treegent-{host_id}", "-f", priv], check=True)
        os.chmod(priv, 0o600)
    with open(pub) as f:
        pub_text = f.read().strip()
    return priv, pub_text


def _delete_host_key(host_id: str) -> None:
    """Revoke: remove this host's keypair from the central box."""
    keydir = os.environ.get("TG_SSH_KEYDIR") or os.path.expanduser(
        "~/.treegent/agenthosts")
    for p in (os.path.join(keydir, f"{host_id}_ed25519"),
              os.path.join(keydir, f"{host_id}_ed25519.pub")):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def ssh_run(host_id: str, host: str, port: int, user: str, script: str,
            timeout: int = 900) -> dict:
    """Run a shell script on the agent host via ssh. Returns rc/stdout/stderr."""
    priv, _ = _host_keypair(host_id)
    cmd = ["ssh", "-i", priv,
           "-p", str(port),
           "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ConnectTimeout=15",
           f"{user}@{host}",
           "bash -s"]
    p = subprocess.run(cmd, input=script.encode(), capture_output=True,
                       timeout=timeout)
    return {"rc": p.returncode, "stdout": p.stdout.decode()[-4000:],
            "stderr": p.stderr.decode()[-2000:]}


def _agenthost_script(host_id: str) -> tuple[str, str]:
    """The provisioning script executed on the agent host.

    Returns (script, host_key). Env comes from the central box at provision
    time and is NEVER stored in Mongo:
      TG_RUNTIME_HOST_KEY   per-host secret (hash persists in agent_hosts)
      TG_PUBLIC_URL-derived service URLs over TLS
    R56: no database credential is written — hosted runtimes are HTTPS
    clients of the central services.
    """
    public = os.environ.get("TG_PUBLIC_URL", "")
    if not public or "127.0.0.1" in public or "localhost" in public:
        raise RuntimeError(
            "TG_PUBLIC_URL missing or loopback — refusing to provision: "
            "agent hosts need the real public URL (e.g. https://tree.nstern.de). "
            "Set it in /etc/treegent/env and restart treegent.")
    # R56: hosts authenticate to chat with a per-host key; NO Mongo URL
    # ever leaves the central box again. (hash stored in agent_hosts by
    # the provision endpoint; the secret itself only in runtime.env)
    host_key = new_host_key()
    # per-host random runtime token: the daemon's own /internal API must not
    # validate against any well-known value (agents get exec on this box)
    rt_token = "rt_" + secrets.token_hex(24)
    agent_keyfile = "/etc/treegent/agent-keyfile"
    script = f"""set -euo pipefail
echo "[provision] start on $(hostname) for host {host_id}"

# 1) user + dirs
id -u treegent >/dev/null 2>&1 || useradd -m -s /bin/bash treegent
id -u tgexec >/dev/null 2>&1 || useradd -m -s /bin/bash tgexec
mkdir -p /opt/TreeGent /var/log/treegent

# 2) dependencies (fresh Debian/Ubuntu boxes lack python3-venv)
export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq >/dev/null 2>&1 || true
  apt-get install -y -qq python3-venv python3-pip git curl >/dev/null 2>&1 \
    || echo "[provision] WARN: apt install failed — trying anyway"
fi

# 3) repo + deps via uv (same flow as the central box; there is no
#    requirements.txt — the workspace installs from pyproject.toml/uv.lock)
if [ ! -d /opt/TreeGent/.git ]; then
  git clone -q https://github.com/nstern-digital-solution/TreeGent.git /opt/TreeGent
else
  # pre-R68 checkouts track egg-info that uv sync regenerates in place —
  # remove it so the pull cannot fail on regenerated build metadata.
  rm -rf /opt/TreeGent/packages/treegent-common/treegent_common.egg-info
  git -c safe.directory=/opt/TreeGent -C /opt/TreeGent pull -q \
    || echo "[provision] WARN: git pull failed — host keeps its current code; run Update after provisioning"
fi
chown -R treegent:treegent /opt/TreeGent /var/log/treegent
cd /opt/TreeGent
rm -rf .venv   # partial venv from earlier attempts would linger otherwise
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 \
    && install -m 755 "$(command -v uv)" /usr/local/bin/uv || true
fi
uv sync -q
chown -R treegent:treegent /opt/TreeGent

# 3b) headless Chromium for the agent browser tool. Installed once into a
#     shared path (the service runs as treegent, provisioning as root) and
#     surfaced to the daemon via PLAYWRIGHT_BROWSERS_PATH in runtime.env.
#     --with-deps pulls the apt libraries Chromium needs. Best-effort: a
#     missing browser degrades the tool to a clear error, never a dead
#     provision or a crashed agent turn.
install -d -m 755 /var/lib/treegent/pw-browsers
PLAYWRIGHT_BROWSERS_PATH=/var/lib/treegent/pw-browsers \\
  uv run playwright install --with-deps chromium >/dev/null 2>&1 \\
  || echo "[provision] WARN: chromium install failed — the browser tool will report it as unavailable"

# 4) runtime-only env file (NEVER the service token; the runtime
#    authenticates as agents with agent keys, not as the central web)
install -d -m 755 /etc/treegent
# Issue #23: a re-provision must NOT rotate the host key the box already
# holds (rotation on every retry is what churned keys before) — reuse the
# key when this runtime.env belongs to THIS host, else take the freshly
# generated one.
HOST_KEY='{host_key}'
if [ -f /etc/treegent/runtime.env ]; then
  OLD_ID=$(grep -m1 '^TG_RUNTIME_HOST_ID=' /etc/treegent/runtime.env | cut -d= -f2- || true)
  OLD_KEY=$(grep -m1 '^TG_RUNTIME_HOST_KEY=' /etc/treegent/runtime.env | cut -d= -f2- || true)
  if [ "$OLD_ID" = '{host_id}' ] && [ -n "$OLD_KEY" ]; then
    HOST_KEY="$OLD_KEY"
  fi
fi
cat > /etc/treegent/runtime.env <<ENVEOF
TG_RUNTIME_HOST_ID={host_id}
TG_RUNTIME_HOST_KEY=$HOST_KEY
TG_RUNTIME_EXEC_ENABLED=true
TG_RUNTIME_EXEC_USER=tgexec
TG_RUNTIME_SERVICE_TOKEN={rt_token}
TG_CHAT_URL={public}/chat
TG_PROXY_URL={public}/proxy
TG_MAIL_URL={public}/mail
TG_SECRETS_URL={public}/secrets
TG_FILES_URL={public}/files
TG_RUNTIME_BIND=127.0.0.1
TG_RUNTIME_PORT=8010
PLAYWRIGHT_BROWSERS_PATH=/var/lib/treegent/pw-browsers
ENVEOF
chmod 600 /etc/treegent/runtime.env
# Issue #23: env-write completion signal. Central commits host_key_hash
# ONLY off this marker (hash = sha256 of the key the file actually holds,
# i.e. the stored form itself, safe for logs) — a script that dies BEFORE
# this line leaves the box on its OLD key, and the OLD hash must survive
# central-side or every host-key auth on the box is rejected forever.
echo "[provision] env-written keyhash=$(printf '%s' "$HOST_KEY" | sha256sum | cut -d' ' -f1)"

# 5) systemd unit: runtime daemon as treegent, exec as tgexec (R46)
cat > /etc/systemd/system/treegent-agent.service <<UNITEOF
[Unit]
Description=TreeGent agent runtime ({host_id})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=treegent
EnvironmentFile=/etc/treegent/runtime.env
ExecStart=/opt/TreeGent/.venv/bin/uvicorn services.runtime.app.__main__:app --host 127.0.0.1 --port 8010
WorkingDirectory=/opt/TreeGent
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF
# R46: exec must drop to the unprivileged tgexec user via passwordless sudo
echo 'treegent ALL=(tgexec) NOPASSWD: ALL' > /etc/sudoers.d/treegent-tgexec
chmod 440 /etc/sudoers.d/treegent-tgexec

systemctl daemon-reload
systemctl enable treegent-agent.service
systemctl restart treegent-agent.service   # enable --now is a NO-OP on a running unit — re-provision must actually restart it

echo "[provision] done — treegent-agent.service active"
"""
    return script, host_key


# Issue #23: the provision script signals the exact moment the NEW env is
# on the box by echoing this marker + the sha256 of the host key it wrote
# (sha256 is the stored form — the same value kept in agent_hosts, safe
# for logs). Central commits host_key_hash ONLY from this marker.
ENV_MARKER = "[provision] env-written keyhash="


def parse_env_marker(output: str | None) -> str | None:
    """The hash of the host key the box demonstrably holds, or None.

    None means the script never got as far as writing
    /etc/treegent/runtime.env (or the marker was garbled) — the box is
    still on its OLD key (if it ever had one)."""
    for line in (output or "").splitlines():
        line = line.strip()
        if line.startswith(ENV_MARKER):
            h = line[len(ENV_MARKER):].strip().lower()
            if len(h) == 64 and all(c in "0123456789abcdef" for c in h):
                return h
    return None


def provision_hash_decision(env_hash: str | None, ok: bool) -> str | None:
    """Issue #23: which host_key_hash survives a provision attempt.

    The hash commits ONLY when the box demonstrably holds the key (the
    env-written marker) — never merely because an attempt started. The
    old code committed the fresh hash BEFORE ssh_run, so any failure left
    the box on its old runtime.env while central validated a hash no box
    held: verify_host_key rejected forever ("host credentials rejected",
    every agent on the box dead) while the dashboard said 'active'.
    Restoring the old hash on failure (the obvious fix) is ALSO wrong:
    the script writes the env mid-script, so a post-env-write failure
    leaves the box on the NEW key and breaks auth the same way.

    (env-written x rc) decision table:
      marker,    rc == 0  -> commit the box-signaled hash
      marker,    rc != 0  -> STILL commit the box-signaled hash (the env
                             landed mid-script; the box now holds that
                             key — keeping the old hash re-bricks auth)
      no marker, rc != 0  -> None = keep the recorded hash (pre-env-write
                             failure: the box's OLD key is still live)
      no marker, rc == 0  -> None = keep the recorded hash (defensive:
                             never record a hash for a key we cannot
                             prove landed)

    Returns the hash to commit, or None to leave the recorded hash
    untouched."""
    # rc is a decision-table dimension only: the commit couples to the
    # env-write outcome exactly so rc != 0 cannot re-brick auth.
    _ = ok
    return env_hash


async def provision_host(host_doc: dict) -> dict:
    """SSH in and provision. Updates the host row with status + log."""
    hid = host_doc["_id"]
    script, host_key = _agenthost_script(hid)
    # Issue #23: do NOT touch host_key_hash up-front — the box keeps its
    # OLD runtime.env unless the script gets as far as writing the new one.
    # The hash is committed (or kept) after the attempt, per
    # provision_hash_decision.
    await db.agent_hosts.update_one(
        {"_id": hid}, {"$set": {"status": "provisioning",
                                "provision_started_at": _now()}})
    try:
        r = await asyncio.to_thread(
            ssh_run, hid, host_doc["address"], host_doc.get("port", 22),
            host_doc.get("ssh_user", "root"), script)
        ok = r["rc"] == 0
        fields = {"status": "active" if ok else "failed",
                  "last_log": (r["stdout"] + "\\n" + r["stderr"])[-2000:],
                  "provisioned_at": _now() if ok else None}
        commit = provision_hash_decision(parse_env_marker(r["stdout"]), ok)
        if commit:
            fields["host_key_hash"] = commit
        await db.agent_hosts.update_one({"_id": hid}, {"$set": fields})
        return {"ok": ok, "log": r["stdout"][-1500:],
                "stderr": r["stderr"][-500:] if not ok else ""}
    except Exception as e:  # noqa: BLE001
        # TimeoutExpired carries the partial captured output — the marker
        # may be in it even though the attempt failed (ssh killed late).
        partial = getattr(e, "stdout", None)
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        fields = {"status": "failed",
                  "last_log": f"ssh error: {e}"[:2000]}
        commit = provision_hash_decision(parse_env_marker(partial), False)
        if commit:
            fields["host_key_hash"] = commit
        await db.agent_hosts.update_one({"_id": hid}, {"$set": fields})
        return {"ok": False, "log": f"ssh error: {e}"}


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


# ---------------- healthcheck / version / update (R54) ----------------

CHECK_SCRIPT = """set -uo pipefail
SVC=$(systemctl is-active treegent-agent.service 2>/dev/null || echo unknown)
if [ -d /opt/TreeGent/.git ]; then
  VER=$(git -c safe.directory=/opt/TreeGent -C /opt/TreeGent rev-parse HEAD 2>/dev/null || echo none)
else
  VER=none
fi
UPT=$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0)
LOAD=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null || echo 0)
# Issue #23: auth-consistency probe. systemctl says nothing about whether
# the box's host key still matches the hash central validates against —
# ask the real auth path (chat /internal/host) with the key recorded ON
# THE BOX and report only the HTTP status. 401 = key/hash divergence:
# the host is auth-failed and needs re-provision. The key itself never
# leaves the box.
AUTH=skip
if [ -f /etc/treegent/runtime.env ]; then
  HI=$(grep -m1 '^TG_RUNTIME_HOST_ID=' /etc/treegent/runtime.env | cut -d= -f2- || true)
  HK=$(grep -m1 '^TG_RUNTIME_HOST_KEY=' /etc/treegent/runtime.env | cut -d= -f2- || true)
  CU=$(grep -m1 '^TG_CHAT_URL=' /etc/treegent/runtime.env | cut -d= -f2- || true)
  if [ -n "${HI:-}" ] && [ -n "${HK:-}" ] && [ -n "${CU:-}" ]; then
    AUTH=$(curl -s -o /dev/null -m 10 -w '%{http_code}' \
      -H "X-Host-Id: $HI" -H "X-Host-Key: $HK" \
      "$CU/internal/host/agents" || true)
    AUTH=${AUTH:-err}
  fi
fi
echo "svc=$SVC ver=$VER uptime=$UPT load=$LOAD auth=$AUTH"
"""


def _central_commit(full: bool = False) -> str:
    """The central box's own repo commit — the reference version hosts
    are compared against (update = make host match central)."""
    here = os.path.abspath(__file__)
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(here))))   # services/runtime/app/x.py -> repo root
    try:
        out = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True, timeout=10)
        if out.returncode != 0:  # e.g. dubious ownership when run as root
            out = subprocess.run(
                ["git", "-c", f"safe.directory={repo}", "-C", repo,
                 "rev-parse", "HEAD"],
                capture_output=True, timeout=10)
        sha = out.stdout.decode().strip()
        return sha if (out.returncode == 0 and sha) else "?"
    except Exception:
        return "?"


def _central_origin_ahead(cache_s: int = 300) -> int:
    """R71: how many commits origin/main is AHEAD of this box (git fetch +
    rev-list count). The shared server itself being behind is exactly the
    state the navbar red dot must catch — hosts are compared against
    CENTRAL, but central is compared against ORIGIN. Cached 5 min so the
    60s health loop doesn't hammer GitHub; -1 = check failed (unknown)."""
    import time as _time
    now = _time.time()
    global _origin_ahead_cache
    try:
        _origin_ahead_cache
    except NameError:
        _origin_ahead_cache = {"ts": 0.0, "val": 0}
    if now - _origin_ahead_cache["ts"] < cache_s:
        return _origin_ahead_cache["val"]
    here = os.path.abspath(__file__)
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(here))))
    val = -1
    try:
        subprocess.run(["git", "-C", repo, "fetch", "origin", "main"],
                       capture_output=True, timeout=30)
        subprocess.run(["git", "-c", f"safe.directory={repo}", "-C", repo,
                        "fetch", "origin", "main"],
                       capture_output=True, timeout=30)
        out = subprocess.run(
            ["git", "-c", f"safe.directory={repo}", "-C", repo, "rev-list",
             "--count", "HEAD..origin/main"],
            capture_output=True, timeout=10)
        if out.returncode == 0:
            val = int(out.stdout.decode().strip() or 0)
    except Exception:  # noqa: BLE001 — never break list_hosts on git issues
        val = -1
    _origin_ahead_cache.update(ts=now, val=val)
    return val


def _central_commit_short() -> str:
    sha = _central_commit(full=True)
    return sha[:7] if sha not in ("?", "") else "?"


def check_host_sync(host_doc: dict) -> dict:
    """SSH probe: service state, repo version, uptime, load, auth
    consistency. Updates the host row (status active|stopped|unreachable|
    auth-failed, last_seen, version...)."""
    hid = host_doc["_id"]
    try:
        r = ssh_run(hid, host_doc["address"], host_doc.get("port", 22),
                    host_doc.get("ssh_user", "root"), CHECK_SCRIPT, timeout=30)
    except Exception as e:  # noqa: BLE001
        r = {"rc": -1, "stdout": "", "stderr": str(e)}
    fields = {"svc": "unknown", "ver": "none", "uptime": "0", "load": "0",
              "auth": "skip"}
    if r["rc"] == 0:
        for kv in r["stdout"].strip().splitlines():
            for part in kv.split():
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k in fields:
                        fields[k] = v
        status = "active" if fields["svc"] == "active" else "stopped"
        if fields["auth"] == "401":
            # Issue #23: the box's host key no longer matches the recorded
            # hash — its agents cannot authenticate (host credentials
            # rejected). This must NEVER be reported 'active'; the host
            # needs re-provision, which re-syncs key and hash together.
            status = "auth-failed"
    else:
        status = "unreachable"
    ver = fields["ver"]
    ver_short = ver[:7] if ver not in ("none", "") else "none"
    update = {"status": status, "svc": fields["svc"],
              "host_version": ver, "host_version_short": ver_short,
              "uptime_s": int(fields["uptime"] or 0),
              "load1": float(fields["load"] or 0),
              "auth_probe": fields["auth"],
              "needs_reprovision": fields["auth"] == "401",
              "last_seen": _now() if status != "unreachable" else None}
    return update


def update_script(target_sha: str) -> str:
    """Make the host run the central box's current commit."""
    return f"""set -euo pipefail
cd /opt/TreeGent
GIT="git -c safe.directory=/opt/TreeGent"
$GIT fetch -q origin
$GIT checkout -q {target_sha} 2>/dev/null || $GIT reset -q --hard {target_sha}
UV="$(command -v uv || echo /usr/local/bin/uv)"
"$UV" sync -q
chown -R treegent:treegent /opt/TreeGent/.venv
# browser tool: make sure headless Chromium exists on updated hosts too
install -d -m 755 /var/lib/treegent/pw-browsers
PLAYWRIGHT_BROWSERS_PATH=/var/lib/treegent/pw-browsers \\
  "$UV" run playwright install --with-deps chromium >/dev/null 2>&1 \\
  || echo "[update] WARN: chromium install failed — the browser tool will report it as unavailable"
grep -q PLAYWRIGHT_BROWSERS_PATH /etc/treegent/runtime.env 2>/dev/null \\
  || echo 'PLAYWRIGHT_BROWSERS_PATH=/var/lib/treegent/pw-browsers' >> /etc/treegent/runtime.env
systemctl restart treegent-agent.service
echo "updated to {target_sha[:7]}"
"""
