"""Path model for the hierarchical file system (issue #51 B).

One normalization rule, used everywhere a path enters the system:

- absolute only ("/…"), trailing "/" stripped, "//" collapsed
- "." segments collapse away (canonicalized like "//" — review finding on #52:
  "/home/a/./x" and "/home/a/x" must be the same path, not two)
- ".." segments rejected (no traversal)
- depth <= 16 segments, total length <= 512
- per segment: [A-Za-z0-9._- ] + at least one char (no empty segments)

normalize_path returns the canonical string, or None on ANY violation —
callers translate None into a 400 and never interpret the raw input.

Paths are opaque strings in the tree model (no filesystem semantics):
"/home/alice/a.txt" lives in folder "/home/alice" and a share at prefix
"/home" covers it. The root "/" names the top of the tree (listable, but
never a folder doc / share target).
"""
import re

SEGMENT_RE = re.compile(r"^[A-Za-z0-9._\- ]+$")
MAX_DEPTH = 16
MAX_LEN = 512
ROOT = "/"


def normalize_path(p) -> str | None:
    """Canonical absolute path, or None when the input violates the rules."""
    if not isinstance(p, str) or not p.startswith("/"):
        return None
    if len(p) > MAX_LEN:
        return None
    segs: list[str] = []
    for seg in p.split("/")[1:]:
        if seg == "":
            continue                      # collapse //
        if seg == ".":
            continue                      # canonicalize away (review finding)
        if seg == ".." or not SEGMENT_RE.fullmatch(seg):
            return None                   # traversal + charset violations
        segs.append(seg)
    if len(segs) > MAX_DEPTH:
        return None
    return ROOT if not segs else "/" + "/".join(segs)


def basename(path: str) -> str:
    return path.rsplit("/", 1)[1] or ""


def dirname(path: str) -> str:
    d = path.rsplit("/", 1)[0]
    return d if d else ROOT


def ancestors(path: str) -> list[str]:
    """Every directory strictly above path, root first: "/a/b/c" ->
    ["/", "/a", "/a/b"]. The root "/" is included (it is an ancestor dir
    of every path) — a "/" share prefix covers the whole tree."""
    out = [ROOT]
    segs = [s for s in path.split("/") if s]
    cur = ""
    for seg in segs[:-1]:
        cur += "/" + seg
        out.append(cur)
    return out


def covers(prefix: str, path: str) -> bool:
    """A share at prefix covers path when prefix == path or prefix is an
    ancestor DIRECTORY of path ("/a" covers "/a/b/c.txt", never "/ab")."""
    if prefix == ROOT:
        return True
    return path == prefix or path.startswith(prefix + "/")
