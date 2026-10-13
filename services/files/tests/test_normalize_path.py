"""normalize_path guard table (issue #51 B) — the traversal/charset/depth
guard is the load-bearing piece of the whole path model, so it gets its
own table-driven suite. Pure unit tests; no server, no Mongo."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", ".."))

from services.files.app import paths  # noqa: E402


def test_ok_cases():
    ok = [
        ("/home/alice/a.txt", "/home/alice/a.txt"),
        ("/home/alice", "/home/alice"),
        ("/home/alice/", "/home/alice"),           # trailing / stripped
        ("/a//b", "/a/b"),                          # // collapsed
        ("//", "/"),                                # collapses to root
        ("/", "/"),                                 # root lists
        ("/home/alice/my file.txt", "/home/alice/my file.txt"),  # space
        ("/home/alice/.hidden", "/home/alice/.hidden"),
        ("/a/b_c.d-e", "/a/b_c.d-e"),
        ("/projects/future/x", "/projects/future/x"),
        ("/" + "/".join(f"s{i}" for i in range(16)), None),  # depth 16 ok
    ]
    for raw, want in ok:
        got = paths.normalize_path(raw)
        if want is None:
            assert got is not None and got.count("/") == 16, (raw, got)
        else:
            assert got == want, (raw, got)


def test_reject_table():
    bad = [
        "",                          # empty
        "home/alice",                # not absolute
        "/home/alice/../bob",        # traversal
        "/..",                       # traversal at root
        "/../etc/passwd",            # traversal
        "/a/./../../b",              # traversal inside
        "/home/alice/é",             # charset
        "/a/b!",                     # charset
        "/a/b@c",                    # charset
        "/a/ /b!",                   # charset (space ok, "!" not)
        None,                        # not a string
        42,
    ]
    for raw in bad:
        assert paths.normalize_path(raw) is None, raw


def test_depth_and_length_limits():
    deep_ok = "/" + "/".join(f"s{i}" for i in range(16))
    assert paths.normalize_path(deep_ok) == deep_ok
    deep_bad = "/" + "/".join(f"s{i}" for i in range(17))
    assert paths.normalize_path(deep_bad) is None      # depth > 16
    assert paths.normalize_path("/" + "a" * 511) == "/" + "a" * 511  # 512
    assert paths.normalize_path("/" + "a" * 512) is None            # 513


def test_empty_segment_only_collapses():
    # "//" inside a path collapses; it is NOT a rejection — but a path
    # that collapses to nothing but the root is just the root
    assert paths.normalize_path("/a///b/") == "/a/b"


def test_helpers():
    assert paths.basename("/home/alice/a.txt") == "a.txt"
    assert paths.dirname("/home/alice/a.txt") == "/home/alice"
    assert paths.dirname("/a.txt") == "/"
    assert paths.ancestors("/a/b/c") == ["/", "/a", "/a/b"]
    assert paths.ancestors("/a") == ["/"]


def test_share_coverage_rule():
    # a share at prefix covers the path itself and everything below it,
    # never a sibling with a shared name stem ("/a" must not cover "/ab")
    assert paths.covers("/a", "/a/b/c.txt")
    assert paths.covers("/a", "/a")
    assert paths.covers("/", "/anything/at/all")
    assert not paths.covers("/a", "/ab/c.txt")
    assert not paths.covers("/a/b", "/a")
