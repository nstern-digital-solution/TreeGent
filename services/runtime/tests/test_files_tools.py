"""files.ls / files.pull / files.push tool tests (issue #51 D) with a
stubbed Services client — the contract under test is the tool layer:
listing format, pull writes into the workspace (safe path, no silent
overwrite), push is restricted to the agent's OWN subtree and enforces
the 100 MB guard. Transport/auth are the files service's tests."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import tools as T  # noqa: E402


class StubServices:
    def __init__(self, listing=None, blob=b""):
        self.calls = []
        self.listing = listing if listing is not None else \
            {"folders": [], "files": []}
        self.blob = blob

    async def _call(self, base, path, method="GET", body=None, params=None,
                    timeout=60.0):
        self.calls.append(("call", path, dict(params or {})))
        return self.listing

    async def _call_bytes(self, base, path, params=None, timeout=120.0):
        self.calls.append(("bytes", path, dict(params or {})))
        return self.blob

    async def _upload(self, base, path, filename, data,
                      content_type="application/octet-stream", params=None,
                      timeout=300.0):
        self.calls.append(("upload", path, filename, data, dict(params or {})))
        return {"id": "fil_stub", "name": filename}


def ctx_with(svcs, workspace, username="alice"):
    return T.ToolContext("agt_alice", workspace, svcs, username=username)


def run(coro):
    return asyncio.run(coro)


# ---------------- files.ls ----------------

def test_ls_formats_folders_and_files():
    svcs = StubServices(listing={
        "folders": [{"name": "docs", "path": "/home/alice/docs"}],
        "files": [{"name": "a.txt", "size": 5}, {"name": "b.bin", "size": 0}]})
    out = run(T.t_files_ls(ctx_with(svcs, tempfile.mkdtemp()), {"path": "/home/alice"}))
    assert out == ("[folder] docs\n"
                   "[file] a.txt (5 bytes)\n"
                   "[file] b.bin (0 bytes)")
    # default path is the tree root
    run(T.t_files_ls(ctx_with(svcs, tempfile.mkdtemp()), {}))
    assert svcs.calls[-1] == ("call", "/files", {"path": "/"})


def test_ls_empty_directory():
    out = run(T.t_files_ls(ctx_with(StubServices(), tempfile.mkdtemp()), {}))
    assert out == "(empty)"


# ---------------- files.pull ----------------

def test_pull_writes_file_into_workspace():
    ws = tempfile.mkdtemp()
    svcs = StubServices(
        listing={"folders": [], "files": [{"id": "fil_1", "name": "a.txt"}]},
        blob=b"hello")
    out = run(T.t_files_pull(ctx_with(svcs, ws),
                             {"path": "/home/alice/a.txt"}))
    assert "pulled" in out and "5 bytes" in out
    with open(os.path.join(ws, "a.txt"), "rb") as f:
        assert f.read() == b"hello"


def test_pull_to_custom_workspace_path():
    ws = tempfile.mkdtemp()
    svcs = StubServices(
        listing={"folders": [], "files": [{"id": "fil_1", "name": "a.txt"}]},
        blob=b"xy")
    run(T.t_files_pull(ctx_with(svcs, ws),
                       {"path": "/home/alice/a.txt", "to": "sub/out.txt"}))
    with open(os.path.join(ws, "sub", "out.txt"), "rb") as f:
        assert f.read() == b"xy"


def test_pull_refuses_overwrite_unless_asked():
    ws = tempfile.mkdtemp()
    with open(os.path.join(ws, "a.txt"), "w") as f:
        f.write("old")
    svcs = StubServices(
        listing={"folders": [], "files": [{"id": "fil_1", "name": "a.txt"}]},
        blob=b"new")
    out = run(T.t_files_pull(ctx_with(svcs, ws), {"path": "/home/alice/a.txt"}))
    assert out.startswith("ERROR") and "overwrite" in out
    with open(os.path.join(ws, "a.txt")) as f:
        assert f.read() == "old"          # untouched
    out = run(T.t_files_pull(ctx_with(svcs, ws),
                             {"path": "/home/alice/a.txt", "overwrite": True}))
    assert not out.startswith("ERROR")
    with open(os.path.join(ws, "a.txt"), "rb") as f:
        assert f.read() == b"new"          # replaced


def test_pull_invisible_file_and_workspace_escape():
    ws = tempfile.mkdtemp()
    svcs = StubServices(listing={"folders": [], "files": []})
    out = run(T.t_files_pull(ctx_with(svcs, ws), {"path": "/home/alice/nope.txt"}))
    assert out.startswith("ERROR") and "no visible file" in out
    svcs2 = StubServices(
        listing={"folders": [], "files": [{"id": "fil_1", "name": "a.txt"}]},
        blob=b"x")
    out = run(T.t_files_pull(ctx_with(svcs2, ws),
                             {"path": "/home/alice/a.txt", "to": "../evil"}))
    assert out.startswith("ERROR") and "escapes workspace" in out
    assert not os.path.exists(os.path.join(os.path.dirname(ws), "evil"))


# ---------------- files.push ----------------

def test_push_rejects_non_owned_path():
    ws = tempfile.mkdtemp()
    with open(os.path.join(ws, "notes.md"), "w") as f:
        f.write("hi")
    for bad in ("/home/bob/notes.md", "/projects/notes.md",
                "/home/alice/../bob/x.md", "/home/alicent/x.md"):
        svcs = StubServices()
        out = run(T.t_files_push(ctx_with(svcs, ws),
                                 {"from": "notes.md", "to_path": bad}))
        assert out.startswith("ERROR") and "own subtree" in out, (bad, out)
        assert not any(c[0] == "upload" for c in svcs.calls), bad


def test_push_requires_username():
    # fail closed: without the agent's username no destination can be
    # proven to be its own subtree
    ws = tempfile.mkdtemp()
    with open(os.path.join(ws, "n.md"), "w") as f:
        f.write("x")
    svcs = StubServices()
    out = run(T.t_files_push(ctx_with(svcs, ws, username=""),
                             {"from": "n.md", "to_path": "/home/alice/n.md"}))
    assert out.startswith("ERROR") and "own subtree" in out
    assert not any(c[0] == "upload" for c in svcs.calls)


def test_push_uploads_own_subtree():
    ws = tempfile.mkdtemp()
    with open(os.path.join(ws, "notes.md"), "w") as f:
        f.write("payload")
    svcs = StubServices()
    out = run(T.t_files_push(ctx_with(svcs, ws),
                             {"from": "notes.md",
                              "to_path": "/home/alice/sub/notes.md"}))
    assert "pushed" in out and not out.startswith("ERROR"), out
    up = [c for c in svcs.calls if c[0] == "upload"]
    assert len(up) == 1
    assert up[0][2] == "notes.md"                     # basename
    assert up[0][3] == b"payload"                     # bytes
    assert up[0][4] == {"path": "/home/alice/sub"}    # folder, not name


def test_push_missing_source_and_size_guard():
    ws = tempfile.mkdtemp()
    svcs = StubServices()
    out = run(T.t_files_push(ctx_with(svcs, ws),
                             {"from": "ghost.md", "to_path": "/home/a/x.md"}))
    assert out.startswith("ERROR") and "no workspace file" in out
    big = os.path.join(ws, "big.bin")
    with open(big, "wb") as f:
        pass
    os.truncate(big, 100 * 1024 * 1024 + 1)            # sparse, cheap
    out = run(T.t_files_push(ctx_with(svcs, ws),
                             {"from": "big.bin", "to_path": "/home/alice/big.bin"}))
    assert out.startswith("ERROR") and "too large" in out
    assert not any(c[0] == "upload" for c in svcs.calls)


# ---------------- registry ----------------

def test_tools_and_schemas_registered():
    for name in ("files.ls", "files.pull", "files.push"):
        assert name in T.TOOLS, name
    names = [s["name"] for s in T.TOOL_SCHEMAS]
    for name in ("files.ls", "files.pull", "files.push"):
        assert name in names, name
