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
  git -c safe.directory=/opt/TreeGent -C /opt/TreeGent pull -q || true
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

# 4) runtime-only env file (NEVER the service token; the runtime
#    authenticates as agents with agent keys, not as the central web)
install -d -m 755 /etc/treegent
cat > /etc/treegent/runtime.env <<'ENVEOF'
TG_RUNTIME_HOST_ID={host_id}
TG_RUNTIME_HOST_KEY={host_key}
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
ENVEOF
chmod 600 /etc/treegent/runtime.env

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


async def provision_host(host_doc: dict) -> dict:
    """SSH in and provision. Updates the host row with status + log."""
    hid = host_doc["_id"]
    script, host_key = _agenthost_script(hid)
    await db.agent_hosts.update_one(
        {"_id": hid}, {"$set": {"status": "provisioning",
                                "host_key_hash": hash_host_key(host_key),
                                "provision_started_at": _now()}})
    try:
        r = await asyncio.to_thread(
            ssh_run, hid, host_doc["address"], host_doc.get("port", 22),
            host_doc.get("ssh_user", "root"), script)
        ok = r["rc"] == 0
        await db.agent_hosts.update_one(
            {"_id": hid},
            {"$set": {"status": "active" if ok else "failed",
                      "last_log": (r["stdout"] + "\\n" + r["stderr"])[-2000:],
                      "provisioned_at": _now() if ok else None}})
        return {"ok": ok, "log": r["stdout"][-1500:],
                "stderr": r["stderr"][-500:] if not ok else ""}
    except Exception as e:  # noqa: BLE001
        await db.agent_hosts.update_one(
            {"_id": hid},
            {"$set": {"status": "failed",
                      "last_log": f"ssh error: {e}"[:2000]}})
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
echo "svc=$SVC ver=$VER uptime=$UPT load=$LOAD"
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


def _central_commit_short() -> str:
    sha = _central_commit(full=True)
    return sha[:7] if sha not in ("?", "") else "?"


def check_host_sync(host_doc: dict) -> dict:
    """SSH probe: service state, repo version, uptime, load. Updates the
    host row (status active|stopped|unreachable, last_seen, version...)."""
    hid = host_doc["_id"]
    try:
        r = ssh_run(hid, host_doc["address"], host_doc.get("port", 22),
                    host_doc.get("ssh_user", "root"), CHECK_SCRIPT, timeout=30)
    except Exception as e:  # noqa: BLE001
        r = {"rc": -1, "stdout": "", "stderr": str(e)}
    fields = {"svc": "unknown", "ver": "none", "uptime": "0", "load": "0"}
    if r["rc"] == 0:
        for kv in r["stdout"].strip().splitlines():
            for part in kv.split():
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k in fields:
                        fields[k] = v
        status = "active" if fields["svc"] == "active" else "stopped"
    else:
        status = "unreachable"
    ver = fields["ver"]
    ver_short = ver[:7] if ver not in ("none", "") else "none"
    update = {"status": status, "svc": fields["svc"],
              "host_version": ver, "host_version_short": ver_short,
              "uptime_s": int(fields["uptime"] or 0),
              "load1": float(fields["load"] or 0),
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
systemctl restart treegent-agent.service
echo "updated to {target_sha[:7]}"
"""
