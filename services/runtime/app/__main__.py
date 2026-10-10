import asyncio
import os
import secrets
import sys

from fastapi import FastAPI, Header, HTTPException

# supervisor prints must reach journald (systemd pipes stdout; python buffers)
for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(line_buffering=True)

from .config import client, db, settings
from .loop import supervise

app = FastAPI(title="TreeGent runtime", version="0.1.0")


def _token_ok(t: str) -> bool:
    """Fail-closed: the shipped default token never authorizes /internal
    unless TG_DEV=1 explicitly marks a development machine. Agent hosts
    run WITHOUT TG_SERVICE_TOKEN (R45), so their runtime API must refuse
    the well-known default instead of accepting it."""
    if not t or not settings.service_token:
        return False                       # empty header/config never authorizes
    if t != settings.service_token:
        return False
    if t in ("dev-service-token",) and os.environ.get("TG_DEV") != "1":
        return False
    return True

# live registry the supervisor refreshes (for the /internal endpoint)
AGENTS_STATE: dict = {}


async def _host_health_loop() -> None:
    """R54: probe all hosts every 60s so status stays truthful.
    Hosted runtimes have no Mongo handle — this loop is central-only."""
    if settings.host_key:
        return   # R56: an agent host does not monitor the fleet
    from .hostprovision import check_host_sync
    while True:
        try:
            async for h in db.agent_hosts.find():
                try:
                    update = await asyncio.to_thread(check_host_sync, h)
                    # Issue #23: auth-consistency probe (rider in
                    # check_host_sync's SSH script — the box curls the chat
                    # host tier with ITS recorded key): on 401 the box's key
                    # and the recorded hash diverged and the host is
                    # auth-failed / needs re-provision — never 'active'.
                    if update.get("auth_probe") == "401":
                        update["status"] = "auth-failed"
                    await db.agent_hosts.update_one({"_id": h["_id"]}, {"$set": update})
                except Exception as e:  # noqa: BLE001
                    print(f"[host-health] host {h.get('_id')}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"[host-health] error: {e}")
        await asyncio.sleep(60)


@app.on_event("startup")
async def _startup() -> None:
    import asyncio
    app.state.supervisor = asyncio.create_task(supervise(AGENTS_STATE))
    app.state.host_health = asyncio.create_task(_host_health_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    app.state.supervisor.cancel()
    if getattr(app.state, "host_health", None):
        app.state.host_health.cancel()


@app.get("/health")
async def health():
    # issue #29: agent hosts have NO Mongo by design (R56) — the ping
    # below raised ServerSelectionTimeoutError on EVERY call forever, so
    # the one endpoint a human reaches for first always 500'd on a box
    # that was working perfectly. Same tier predicate as _host_health_loop
    # (settings.host_key set -> hosted runtime, see ~line 38).
    if settings.host_key:
        return {"ok": True, "service": "runtime", "tier": "hosted"}
    await client.admin.command("ping")
    return {"ok": True, "service": "runtime", "tier": "central"}


@app.get("/internal/agents")
async def agents_overview(x_service_token: str = Header(default="")):
    if not _token_ok(x_service_token):
        raise HTTPException(401, "bad service token")
    return {"agents": [{"id": aid, "busy": st.get("busy", False),
                        "name": st.get("name")}
                       for aid, st in AGENTS_STATE.items()]}


@app.get("/internal/history/{agent_id}")
async def agent_history(agent_id: str, x_service_token: str = Header(default=""),
                        limit: int = 50):
    """R47: turns + tool calls for the web viewer. Full transcripts only
    with limit=0 (viewer fetches on demand)."""
    if not _token_ok(x_service_token):
        raise HTTPException(401, "bad service token")
    if limit == 0:
        doc = await db.agent_sessions.find_one({"_id": agent_id})
        return {"messages": (doc or {}).get("messages", [])}
    turns = []
    async for t in db.runtime_turns.find({"agent_id": agent_id})             .sort("started", -1).limit(limit):
        turns.append({
            "id": t["_id"],
            "trigger": t.get("trigger"),
            "steps": t.get("steps", 0),
            "final": (t.get("final") or "")[:2000],
            "injections": [i[:500] for i in (t.get("injections") or [])],
            "started": t.get("started"),
            "ended": t.get("ended"),
        })
    return {"turns": turns}


# ---------------- agent hosts (R53) ----------------

from pydantic import BaseModel


class HostIn(BaseModel):
    name: str
    address: str
    port: int = 22
    ssh_user: str = "root"


@app.get("/internal/hosts/{host_id}/pubkey")
async def host_pubkey(host_id: str, x_service_token: str = Header(default="")):
    """The PUBLIC half of THIS host's provisioning key (per-host keypairs,
    R53). The UI shows it in the one-liner the operator runs on that box."""
    if not _token_ok(x_service_token):
        raise HTTPException(401, "bad service token")
    # R69 fix: host_id reached the keypath unvalidated (path traversal);
    # require a registered host with a canonical id first
    import re as _re
    if not _re.fullmatch(r"host_[0-9a-f]+", host_id) or \
            not await db.agent_hosts.find_one({"_id": host_id}, {"_id": 1}):
        raise HTTPException(404, "no such host")
    from .hostprovision import _host_keypair
    _, pub = _host_keypair(host_id)
    return {"pubkey": pub}


@app.get("/internal/hosts")
async def list_hosts(x_service_token: str = Header(default="")):
    if not _token_ok(x_service_token):
        raise HTTPException(401, "bad service token")
    out = []
    async for h in db.agent_hosts.find():
        out.append({"id": h["_id"], "name": h.get("name"),
                    "address": h.get("address"), "port": h.get("port", 22),
                    "ssh_user": h.get("ssh_user", "root"),
                    "status": h.get("status", "unknown"),
                    "host_version_short": h.get("host_version_short", ""),
                    "uptime": h.get("uptime_s"), "load1": h.get("load1"),
                    "last_seen": h.get("last_seen"),
                    "last_log": (h.get("last_log") or "")[-800:],
                    "provisioned_at": h.get("provisioned_at")})
    from .hostprovision import _central_commit, _central_origin_ahead
    return {"hosts": out, "central_version": _central_commit()[:7],
            "central_behind": _central_origin_ahead()}


@app.post("/internal/hosts", status_code=201)
async def add_host(body: HostIn, x_service_token: str = Header(default="")):
    if not _token_ok(x_service_token):
        raise HTTPException(401, "bad service token")
    import secrets
    hid = f"host_{secrets.token_hex(6)}"
    await db.agent_hosts.insert_one(
        {"_id": hid, "name": body.name, "address": body.address,
         "port": body.port, "ssh_user": body.ssh_user,
         "status": "new", "created_at": _now_iso()})
    return {"id": hid}


@app.delete("/internal/hosts/{host_id}")
async def delete_host(host_id: str, x_service_token: str = Header(default=""),
                      x_actor_id: str = Header(default="")):
    """Remove a host: revoke its keypair (central side) and best-effort
    remove the authorized_keys line + stop the service on the box."""
    if not _token_ok(x_service_token):
        raise HTTPException(401, "bad service token")
    # R68: host removal requires the ROOT human's actor id too
    if not await _root_caller(x_actor_id):
        raise HTTPException(403, "host management requires the root human")
    h = await db.agent_hosts.find_one({"_id": host_id})
    if not h:
        raise HTTPException(404, "no such host")
    from .hostprovision import ssh_run, _delete_host_key, _host_keypair
    _, pub = _host_keypair(host_id)
    cleanup = f"""systemctl stop treegent-agent.service 2>/dev/null || true
systemctl disable treegent-agent.service 2>/dev/null || true
sed -i '/{pub.split()[1]}/d' ~{h.get('ssh_user', 'root')}/.ssh/authorized_keys 2>/dev/null || true
echo cleaned"""
    try:
        await asyncio.to_thread(
            ssh_run, host_id, h["address"], h.get("port", 22),
            h.get("ssh_user", "root"), cleanup, timeout=60)
    except Exception:
        pass   # box unreachable — key revocation below still holds
    _delete_host_key(host_id)
    await db.agent_hosts.delete_one({"_id": host_id})
    return {"removed": host_id}


@app.post("/internal/hosts/{host_id}/provision")
async def provision(host_id: str, x_service_token: str = Header(default="")):
    if not _token_ok(x_service_token):
        raise HTTPException(401, "bad service token")
    h = await db.agent_hosts.find_one({"_id": host_id})
    if not h:
        raise HTTPException(404, "no such host")
    from .hostprovision import provision_host
    r = await provision_host(h)
    return r


@app.post("/internal/hosts/{host_id}/check")
async def check_host(host_id: str, x_service_token: str = Header(default="")):
    """R54: probe one host (service state, version, uptime, load)."""
    if not _token_ok(x_service_token):
        raise HTTPException(401, "bad service token")
    h = await db.agent_hosts.find_one({"_id": host_id})
    if not h:
        raise HTTPException(404, "no such host")
    from .hostprovision import check_host_sync
    update = await asyncio.to_thread(check_host_sync, h)
    await db.agent_hosts.update_one({"_id": host_id}, {"$set": update})
    return update


async def _root_caller(actor_id: str) -> bool:
    """R68: host mutations need the root human's actor id beside the token."""
    if not actor_id:
        return False
    from treegent_common.perms import is_root_actor
    a = await db.actors.find_one({"_id": actor_id})
    return bool(a and is_root_actor(a))


@app.post("/internal/hosts/{host_id}/update")
async def update_host(host_id: str, x_service_token: str = Header(default=""),
                      x_actor_id: str = Header(default="")):
    """R54: pull the host to the central box's current commit. R68: root
    human actor id required alongside the token."""
    if not _token_ok(x_service_token):
        raise HTTPException(401, "bad service token")
    if not await _root_caller(x_actor_id):
        raise HTTPException(403, "host management requires the root human")
    h = await db.agent_hosts.find_one({"_id": host_id})
    if not h:
        raise HTTPException(404, "no such host")
    from .hostprovision import _central_commit, update_script, ssh_run
    sha = _central_commit()
    if sha == "?":
        raise HTTPException(503, "central repo version unavailable")
    r = await asyncio.to_thread(
        ssh_run, host_id, h["address"], h.get("port", 22),
        h.get("ssh_user", "root"), update_script(sha), timeout=600)
    ok = r["rc"] == 0
    # Issue #23: an update never touches the host key — a host marked
    # auth-failed (key/hash divergence) stays auth-failed until it is
    # actually re-provisioned, never flipped back to 'active' here.
    ok_status = "active" if h.get("status") != "auth-failed" else "auth-failed"
    await db.agent_hosts.update_one(
        {"_id": host_id},
        {"$set": {"host_version": sha if ok else (h.get("host_version") or ""),
                  "host_version_short": sha[:7] if ok else (h.get("host_version_short") or ""),
                  "status": (ok_status if ok else h.get("status")),
                  "last_update": _now_iso() if ok else None,
                  "last_update_log": (r["stdout"] + r["stderr"])[-1000:]}})
    return {"ok": ok, "log": r["stdout"][-600:], "stderr": r["stderr"][-300:] if not ok else ""}


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
