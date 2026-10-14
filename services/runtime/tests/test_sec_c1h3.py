"""Security C1+H3 (audit #64) regression pins.

C1 — exec environment inheritance + fail-open default (CRITICAL):
  * the exec child env is WHITELISTED (PATH/HOME/LANG/LC_ALL/TMPDIR) on
    BOTH exec paths (foreground create_subprocess_shell, background
    Popen) — no TG_*, no tokens, no Mongo URLs can cross the boundary;
  * exec_enabled fails CLOSED (config default False): t_exec returns a
    clear ERROR unless the host explicitly opts in.

H3 — the runtime refuses to boot on the env.example placeholder token
(and the dev default / empty) unless TG_DEV=1, and _token_ok denies
change-me-random alongside dev-service-token.

Compat (non-negotiable): hosts provisioned WITH exec keep it after
upgrade — hostprovision writes the explicit flag and update_script
backfills it (plus a per-host daemon token for legacy runtime.env files
so the new boot guard cannot brick them). The deploy scripts write the
explicit off for the central host and run-central refuses exec there
outside TG_DEV=1.
"""
import asyncio, os, subprocess, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")
os.environ.setdefault("TG_PUBLIC_URL", "https://central.example")

from app import tools as T      # noqa: E402
from app import config as C     # noqa: E402

REPO = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))


def run(coro):
    return asyncio.run(coro)


def _ctx(workspace):
    return T.ToolContext("agt_sec", workspace, T.Services("stub-key"),
                         username="alice")


def _assert_clean_env(env, workspace):
    """The C1 boundary contract: whitelist keys only, no TG_/TOKEN/MONGO
    anywhere — neither as key nor smuggled in a value."""
    assert env is not None, "exec child must not inherit the daemon env"
    assert set(env) == {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}, \
        f"env must be the exact whitelist, got {sorted(env)}"
    assert env["PATH"], "PATH must cross the boundary"
    assert env["LANG"] == "C.UTF-8" and env["LC_ALL"] == "C.UTF-8"
    assert env["HOME"], "HOME must be set (exec user's home or /tmp)"
    assert env["TMPDIR"].startswith(os.path.join(workspace, ".tg-exec")), \
        f"TMPDIR must be workspace-local, got {env['TMPDIR']}"
    for k, v in env.items():
        ku = k.upper()
        assert not ku.startswith("TG_"), f"TG_ key leaked: {k}"
        assert "TOKEN" not in ku, f"token-bearing key leaked: {k}"
        assert "MONGO" not in ku, f"mongo-bearing key leaked: {k}"
        assert "sentinel" not in (v or ""), f"daemon env value leaked via {k}"


# ---------------- C1: env isolation (both exec paths) ----------------

def test_c1_foreground_exec_env_is_whitelisted(monkeypatch):
    """Foreground path: asyncio.create_subprocess_shell gets ONLY the
    whitelisted env — the daemon's tokens/Mongo URL never cross."""
    ws = tempfile.mkdtemp()
    captured = {}

    async def fake_shell(cmd, **kw):
        captured["cmd"] = cmd
        captured.update(kw)

        class P:
            returncode = 0

            async def communicate(self):
                return b"", b""

            def kill(self):
                pass
        return P()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    monkeypatch.setattr(T.settings, "exec_enabled", True)
    # poison the daemon env: none of this may reach the child
    monkeypatch.setenv("TG_SERVICE_TOKEN", "sentinel-token-123")
    monkeypatch.setenv("TG_MONGO_URL", "mongodb://sentinel-mongo:27017")
    monkeypatch.setenv("RESEND_API_KEY", "sentinel-mail-key")
    out = run(T.t_exec(_ctx(ws), {"cmd": "echo hi"}))
    assert out.startswith("[exit 0]"), out
    assert captured["cwd"] == ws
    _assert_clean_env(captured.get("env"), ws)


def test_c1_background_exec_env_is_whitelisted(monkeypatch):
    """Background path: the detached Popen gets the same whitelist."""
    ws = tempfile.mkdtemp()
    captured = {}

    class FakePopen:
        def __init__(self, *a, **kw):
            captured.update(kw)

    monkeypatch.setattr(T.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(T.settings, "exec_enabled", True)
    monkeypatch.setenv("TG_SERVICE_TOKEN", "sentinel-token-123")
    monkeypatch.setenv("TG_MONGO_URL", "mongodb://sentinel-mongo:27017")
    out = run(T.t_exec(_ctx(ws), {"cmd": "sleep 5", "background": True}))
    assert "background job" in out, out
    assert captured["cwd"] == ws
    _assert_clean_env(captured.get("env"), ws)


def test_c1_exec_env_helpers_create_writable_tmpdir():
    """_exec_env must CREATE the workspace-local TMPDIR (a child handed a
    missing TMPDIR falls back to writing wherever it can)."""
    ws = tempfile.mkdtemp()
    env = T._exec_env(ws)
    assert os.path.isdir(env["TMPDIR"])
    probe = os.path.join(env["TMPDIR"], "probe")
    with open(probe, "w") as f:
        f.write("ok")
    assert open(probe).read() == "ok"


# ---------------- C1: fail-closed default ----------------

def test_c1_exec_disabled_by_default(monkeypatch):
    """The config default is OFF (fail-closed) and t_exec refuses with a
    clear ERROR without ever spawning anything."""
    monkeypatch.delenv("TG_RUNTIME_EXEC_ENABLED", raising=False)
    monkeypatch.chdir(tempfile.mkdtemp())      # no stray .env influence
    assert C.Settings.model_fields["exec_enabled"].default is False
    assert C.Settings().exec_enabled is False

    async def must_not_spawn(*a, **kw):
        raise AssertionError("exec disabled — no subprocess may be spawned")
    monkeypatch.setattr(asyncio, "create_subprocess_shell", must_not_spawn)
    monkeypatch.setattr(T.settings, "exec_enabled", False)
    out = run(T.t_exec(_ctx(tempfile.mkdtemp()), {"cmd": "echo pwned"}))
    assert out.startswith("ERROR: exec is disabled"), out


def test_c1_exec_works_when_explicitly_enabled(monkeypatch):
    """Explicit opt-in still works — and the whitelisted env actually
    reaches the real child (TMPDIR set, TG_* absent)."""
    ws = tempfile.mkdtemp()
    monkeypatch.setattr(T.settings, "exec_enabled", True)
    monkeypatch.setenv("TG_SERVICE_TOKEN", "sentinel-token-123")
    out = run(T.t_exec(_ctx(ws), {
        "cmd": "echo tmp=$TMPDIR; echo tok=[${TG_SERVICE_TOKEN:-}]"}))
    assert out.startswith("[exit 0]"), out
    assert os.path.join(ws, ".tg-exec") in out, out
    assert "tok=[]" in out, f"daemon token must not reach the child: {out}"
    assert "sentinel" not in out


# ---------------- H3: placeholder token ----------------

def _boot_import(extra_env):
    """Import app.config in a CLEAN subprocess — the guard runs at import."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("TG_")}
    env.update(extra_env)
    env["PYTHONPATH"] = os.path.join(REPO, "services", "runtime")
    return subprocess.run([sys.executable, "-c", "import app.config"],
                          env=env, cwd=tempfile.mkdtemp(),
                          capture_output=True, text=True)


def test_h3_placeholder_token_refused_at_boot():
    """The env.example placeholder never boots a runtime (nor the dev
    default or empty); a real token boots; TG_DEV=1 is the escape."""
    for tok in ("change-me-random", "dev-service-token", ""):
        r = _boot_import({"TG_SERVICE_TOKEN": tok})
        assert r.returncode != 0, f"boot must refuse token {tok!r}"
        assert "refusing to start" in r.stderr, r.stderr
    r = _boot_import({"TG_SERVICE_TOKEN": "rt_a-real-per-host-token"})
    assert r.returncode == 0, r.stderr
    r = _boot_import({"TG_SERVICE_TOKEN": "change-me-random", "TG_DEV": "1"})
    assert r.returncode == 0, r.stderr


def test_h3_token_ok_denies_placeholder(monkeypatch):
    from app import __main__ as M
    monkeypatch.delenv("TG_DEV", raising=False)
    monkeypatch.setattr(M.settings, "service_token", "change-me-random")
    assert M._token_ok("change-me-random") is False, \
        "the placeholder must never authorize /internal"
    monkeypatch.setattr(M.settings, "service_token", "dev-service-token")
    assert M._token_ok("dev-service-token") is False
    monkeypatch.setattr(M.settings, "service_token", "rt_abc123")
    assert M._token_ok("rt_abc123") is True
    assert M._token_ok("") is False
    assert M._token_ok("rt_other") is False


# ---------------- compat + deploy contracts ----------------

def test_c1_compat_provisioned_hosts_keep_exec():
    """Provisioned-with-exec hosts keep exec after upgrade: the provision
    script writes the explicit opt-in (it provisions tgexec for exactly
    those), and update_script backfills it for env files that predate the
    flag — while an explicit false stays untouched."""
    from app import hostprovision as hp
    script, _ = hp._agenthost_script("host_deadbeef")
    assert "TG_RUNTIME_EXEC_ENABLED=true" in script
    # H3 compat: legacy runtime.env gets a per-host daemon token backfilled
    # so the new boot guard cannot brick a host on update
    assert "TG_RUNTIME_SERVICE_TOKEN=rt_" in script
    up = hp.update_script("a" * 40)
    assert "grep -q '^TG_RUNTIME_EXEC_ENABLED='" in up
    assert "TG_RUNTIME_EXEC_ENABLED=true" in up
    assert "grep -q '^TG_RUNTIME_SERVICE_TOKEN=rt_'" in up
    for name, s in (("provision", script), ("update", up)):
        p = subprocess.run(["bash", "-n"], input=s, capture_output=True,
                           text=True)
        assert p.returncode == 0, f"{name} script does not parse: {p.stderr}"


def test_c1_deploy_scripts_write_explicit_off():
    """Central deploys write the explicit off; run-central refuses an
    exec-enabled central runtime outside TG_DEV=1."""
    def read(name):
        with open(os.path.join(REPO, "deploy", name)) as f:
            return f.read()
    rc, qs, inst, envex = (read(n) for n in
                           ("run-central.sh", "quickstart.sh", "install.sh",
                            "env.example"))
    assert "TG_RUNTIME_EXEC_ENABLED" in rc and "refusing to start" in rc
    assert 'set_kv TG_RUNTIME_EXEC_ENABLED "false"' in qs
    assert "TG_RUNTIME_EXEC_ENABLED=false" in inst   # central path
    assert "TG_RUNTIME_EXEC_ENABLED=true" in inst    # --agent-host path
    assert "TG_RUNTIME_EXEC_ENABLED=false" in envex
    for name in ("run-central.sh", "quickstart.sh", "install.sh"):
        p = subprocess.run(["bash", "-n", os.path.join(REPO, "deploy", name)],
                           capture_output=True, text=True)
        assert p.returncode == 0, f"{name} does not parse: {p.stderr}"


# ---------------- review #66: hosted empty-token boot exemption ----------

def test_h3_hosted_empty_token_boots(monkeypatch, tmp_path):
    """Rosa's option (a): a hosted runtime (host key set) with NO service
    token must boot — it never sends the header (R56), the guard protects
    nothing there, and refusing turns a pre-R46 env into an outage."""
    import importlib, subprocess, sys, textwrap
    code = textwrap.dedent("""
        import os
        os.environ["TG_RUNTIME_HOST_KEY"] = "hk_test_123"
        for k in ("TG_RUNTIME_SERVICE_TOKEN", "TG_SERVICE_TOKEN", "TG_DEV"):
            os.environ.pop(k, None)
        import services.runtime.app.config  # noqa: F401
        print("BOOT-OK")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=os.path.dirname(
                           os.path.dirname(os.path.dirname(
                               os.path.dirname(os.path.abspath(__file__))))))
    assert "BOOT-OK" in r.stdout, r.stderr[-400:]


def test_h3_hosted_placeholder_token_still_refused():
    """The exemption is EMPTY-only: a hosted runtime carrying the env.example
    placeholder still refuses (a known public header value must never
    authorize /internal, hosted or not)."""
    import subprocess, sys, textwrap
    code = textwrap.dedent("""
        import os
        os.environ["TG_RUNTIME_HOST_KEY"] = "hk_test_123"
        os.environ["TG_SERVICE_TOKEN"] = "change-me-random"
        os.environ.pop("TG_DEV", None)
        import services.runtime.app.config  # noqa: F401
        print("BOOT-OK")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=os.path.dirname(
                           os.path.dirname(os.path.dirname(
                               os.path.dirname(os.path.abspath(__file__))))))
    assert "BOOT-OK" not in r.stdout
    assert "refusing to start" in (r.stderr or r.stdout)
