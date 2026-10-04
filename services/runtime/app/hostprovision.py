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
import subprocess
import importlib.util
import sys

from .config import db, settings


def _ensure_central_keypair() -> tuple[str, str]:
    """Return (private_path, public_key_text). Generates on first use."""
    priv = os.environ.get("TG_SSH_KEY") or os.path.expanduser(
        "~/.treegent/agenthost_ed25519")
    pub = priv + ".pub"
    if not os.path.exists(priv):
        os.makedirs(os.path.dirname(priv), exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                        "-C", "treegent-central", "-f", priv], check=True)
    with open(pub) as f:
        pub_text = f.read().strip()
    return priv, pub_text


def ssh_run(host: str, port: int, user: str, script: str,
            timeout: int = 900) -> dict:
    """Run a shell script on the agent host via ssh. Returns rc/stdout/stderr."""
    priv, _ = _ensure_central_keypair()
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


def _agenthost_script(host_id: str) -> str:
    """The provisioning script executed on the agent host.

    Env comes from the central box at provision time (never stored in Mongo):
      TG_MONGO_URL      the SAME database the central services use
      TG_PUBLIC_URL     https://central-domain — service routes via Caddy
    """
    mongo = os.environ.get("TG_MONGO_URL", "")
    public = os.environ.get("TG_PUBLIC_URL", "http://127.0.0.1")
    agent_keyfile = "/etc/treegent/agent-keyfile"
    return f"""set -euo pipefail
echo "[provision] start on $(hostname) for host {host_id}"

# 1) user + dirs
id -u treegent >/dev/null 2>&1 || useradd -m -s /bin/bash treegent
id -u tgexec >/dev/null 2>&1 || useradd -m -s /bin/bash tgexec
mkdir -p /opt/TreeGent /var/log/treegent

# 2) repo + venv
if [ ! -d /opt/TreeGent/.git ]; then
  git clone -q https://github.com/nstern-digital-solution/TreeGent.git /opt/TreeGent
else
  git -C /opt/TreeGent pull -q || true
fi
chown -R treegent:treegent /opt/TreeGent /var/log/treegent
cd /opt/TreeGent
python3 -m venv .venv 2>/dev/null || true
.venv/bin/pip install -q -U pip >/dev/null 2>&1 || true
.venv/bin/pip install -q -r requirements.txt

# 3) runtime-only env file (NEVER the service token; the runtime
#    authenticates as agents with agent keys, not as the central web)
install -d -m 755 /etc/treegent
cat > /etc/treegent/runtime.env <<ENVEOF
TG_MONGO_URL={mongo}
TG_RUNTIME_HOST_ID={host_id}
TG_RUNTIME_EXEC_ENABLED=true
TG_CHAT_URL={public}/chat
TG_PROXY_URL={public}/proxy
TG_MAIL_URL={public}/mail
TG_SECRETS_URL={public}/secrets
TG_FILES_URL={public}/files
TG_RUNTIME_BIND=127.0.0.1
TG_RUNTIME_PORT=8010
ENVEOF
chmod 600 /etc/treegent/runtime.env

# 4) systemd unit: runtime daemon as treegent, exec as tgexec (R46)
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
systemctl daemon-reload
systemctl enable --now treegent-agent.service

echo "[provision] done — treegent-agent.service active"
"""


async def provision_host(host_doc: dict) -> dict:
    """SSH in and provision. Updates the host row with status + log."""
    hid = host_doc["_id"]
    script = _agenthost_script(hid)
    await db.agent_hosts.update_one(
        {"_id": hid}, {"$set": {"status": "provisioning",
                                "provision_started_at": _now()}})
    try:
        r = await asyncio.to_thread(
            ssh_run, host_doc["address"], host_doc.get("port", 22),
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
