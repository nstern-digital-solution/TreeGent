"""Issue #23 regression: the host-key hash may be committed only when the
box demonstrably holds the key.

provision_host used to write host_key_hash BEFORE ssh_run — any failed
provision (timeout, DNS, non-zero rc mid-script) left the box on its OLD
runtime.env while central validated ONLY the fresh hash, so every host-key
auth on the box was rejected forever ("host credentials rejected", all box
agents dead) while the dashboard said 'active'. The obvious fix (restore
the old hash on failure) is wrong too: the script writes
/etc/treegent/runtime.env MID-script, so a post-env-write failure leaves
the box on the NEW key. The hash commit must couple to the env-write
marker the script echoes (provision_hash_decision's 2x2 table).

Also pins the second half: the health probe must never report 'active' for
a host whose recorded key the central hash rejects (401 = auth-failed).
"""
import asyncio, os, subprocess, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")
os.environ.setdefault("TG_PUBLIC_URL", "https://central.example")

from app import hostprovision as hp  # noqa: E402

SIG_SHA = "a" * 64     # hash of the key the fake box reports holding
OLD_SHA = "b" * 64     # what central had recorded before the attempt


class FakeCollection:
    def __init__(self):
        self.updates = []

    async def update_one(self, filt, update):
        self.updates.append(update.get("$set", {}))


class FakeDB:
    def __init__(self):
        self.agent_hosts = FakeCollection()


def _run_provision(monkeypatch, fake_ssh):
    fake = FakeDB()
    monkeypatch.setattr(hp, "db", fake)
    monkeypatch.setattr(hp, "ssh_run", fake_ssh)
    r = asyncio.run(hp.provision_host(
        {"_id": "host_deadbeef", "address": "box.example",
         "port": 22, "ssh_user": "root"}))
    return r, fake.agent_hosts.updates


# ---------------- script contract ----------------

def test_script_signals_env_write_and_preserves_key():
    """The script must (a) emit the env-write completion marker, (b) hash
    the key it actually wrote, (c) reuse the key a re-provisioned box
    already holds instead of rotating on every retry, and (d) still be
    valid bash."""
    script, _ = hp._agenthost_script("host_deadbeef")
    assert hp.ENV_MARKER in script, "script must signal env-write completion"
    assert "sha256sum" in script, "marker must carry the stored key hash"
    assert "^TG_RUNTIME_HOST_KEY=" in script, \
        "re-provision must reuse the box's existing host key"
    p = subprocess.run(["bash", "-n"], input=script, capture_output=True,
                       text=True)
    assert p.returncode == 0, f"script does not parse: {p.stderr}"


def test_check_script_probes_auth_with_box_key():
    """The health probe exercises the REAL auth path (chat /internal/host)
    with the key recorded on the box and reports only the HTTP code."""
    assert "X-Host-Id" in hp.CHECK_SCRIPT
    assert "X-Host-Key" in hp.CHECK_SCRIPT
    assert "auth=" in hp.CHECK_SCRIPT
    p = subprocess.run(["bash", "-n"], input=hp.CHECK_SCRIPT,
                       capture_output=True, text=True)
    assert p.returncode == 0, f"check script does not parse: {p.stderr}"


# ---------------- env-marker parsing ----------------

def test_parse_env_marker_reads_hash():
    out = ("[provision] start\n"
           f"{hp.ENV_MARKER}{SIG_SHA}\n"
           "[provision] done\n")
    assert hp.parse_env_marker(out) == SIG_SHA


def test_parse_env_marker_absent_or_garbled():
    assert hp.parse_env_marker(None) is None
    assert hp.parse_env_marker("") is None
    assert hp.parse_env_marker("died before the env write\n") is None
    assert hp.parse_env_marker(f"{hp.ENV_MARKER}xyz\n") is None
    assert hp.parse_env_marker(f"{hp.ENV_MARKER}\n") is None
    assert hp.parse_env_marker(f"{hp.ENV_MARKER}{SIG_SHA[:32]}\n") is None


# ---------------- decision table (env-written x rc) ----------------

def test_decision_table_env_write_decides():
    assert hp.provision_hash_decision(SIG_SHA, True) == SIG_SHA
    assert hp.provision_hash_decision(SIG_SHA, False) == SIG_SHA
    assert hp.provision_hash_decision(None, False) is None
    assert hp.provision_hash_decision(None, True) is None


# ---------------- provision_host commit ordering ----------------

def test_pre_env_failure_keeps_old_hash(monkeypatch):
    """THE regression: a failure before the env write leaves the box on
    its OLD key — no update may touch host_key_hash (the old code
    committed the fresh hash before ssh_run and bricked auth)."""
    def fake_ssh(*a, **k):
        return {"rc": 2, "stdout": "died at apt\n", "stderr": "boom"}
    r, updates = _run_provision(monkeypatch, fake_ssh)
    assert r["ok"] is False
    assert not any("host_key_hash" in u for u in updates), \
        "pre-env-write failure must leave the recorded hash untouched"
    assert updates[-1]["status"] == "failed"


def test_post_env_failure_still_commits_box_hash(monkeypatch):
    """The mirrored case: the env landed mid-script, then the script died.
    The box NOW holds the new key — the signaled hash must be committed
    even though rc != 0 ('restore old hash on failure' re-bricks here)."""
    def fake_ssh(*a, **k):
        return {"rc": 2,
                "stdout": f"[provision] stuff\n{hp.ENV_MARKER}{SIG_SHA}\n",
                "stderr": "boom"}
    r, updates = _run_provision(monkeypatch, fake_ssh)
    assert r["ok"] is False
    assert updates[-1].get("host_key_hash") == SIG_SHA
    assert updates[-1]["status"] == "failed"


def test_success_commits_box_hash_and_never_commits_early(monkeypatch):
    def fake_ssh(*a, **k):
        return {"rc": 0,
                "stdout": f"{hp.ENV_MARKER}{SIG_SHA}\n[provision] done\n",
                "stderr": ""}
    r, updates = _run_provision(monkeypatch, fake_ssh)
    assert r["ok"] is True
    assert "host_key_hash" not in updates[0], \
        "the pre-SSH update must not carry the hash"
    assert updates[-1].get("host_key_hash") == SIG_SHA
    assert updates[-1]["status"] == "active"


def test_timeout_after_env_write_commits_box_hash(monkeypatch):
    """ssh timeout kills the transport but TimeoutExpired carries the
    partial output — if the marker is in it, the box holds the new key."""
    def fake_ssh(*a, **k):
        raise subprocess.TimeoutExpired(
            "ssh", 900, output=f"{hp.ENV_MARKER}{SIG_SHA}\n".encode())
    r, updates = _run_provision(monkeypatch, fake_ssh)
    assert r["ok"] is False
    assert updates[-1].get("host_key_hash") == SIG_SHA


def test_timeout_before_env_write_keeps_old_hash(monkeypatch):
    def fake_ssh(*a, **k):
        raise subprocess.TimeoutExpired("ssh", 900, output=b"died early\n")
    r, updates = _run_provision(monkeypatch, fake_ssh)
    assert r["ok"] is False
    assert not any("host_key_hash" in u for u in updates)


# ---------------- health probe: 401 -> auth-failed, never active --------

def _sync(monkeypatch, stdout, rc=0):
    def fake_ssh(*a, **k):
        return {"rc": rc, "stdout": stdout, "stderr": ""}
    monkeypatch.setattr(hp, "ssh_run", fake_ssh)
    return hp.check_host_sync({"_id": "host_deadbeef",
                               "address": "box.example"})


def test_auth_probe_401_never_active(monkeypatch):
    u = _sync(monkeypatch, "svc=active ver=abc1234 uptime=10 load=0.1 auth=401\n")
    assert u["status"] == "auth-failed", u
    assert u["needs_reprovision"] is True
    assert u["auth_probe"] == "401"
    # a stopped service plus a rejected key is still auth-failed first
    u = _sync(monkeypatch, "svc=inactive ver=none uptime=10 load=0.1 auth=401\n")
    assert u["status"] == "auth-failed", u


def test_auth_probe_ok_stays_active(monkeypatch):
    u = _sync(monkeypatch, "svc=active ver=abc1234 uptime=10 load=0.1 auth=200\n")
    assert u["status"] == "active"
    assert u["needs_reprovision"] is False
    # never provisioned (no runtime.env): probe skips, plain svc status
    u = _sync(monkeypatch, "svc=active ver=none uptime=1 load=0 auth=skip\n")
    assert u["status"] == "active"
    assert u["needs_reprovision"] is False
