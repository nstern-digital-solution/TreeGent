"""Security H2 (audit issue #64) — issue #55 defense in depth at the
source: adapters.wake_detail() embeds the RAW From header into a wake
detail, so it sanitizes that fragment itself (CR/LF stripped, whitespace
collapsed, capped so the whole detail fits the 160-char injection
budget) — and the ruled '(N attachments)' suffix (issue #60) is appended
AFTER the cap so it always survives intact.

The injection-side contract is pinned in
services/runtime/tests/test_issue55_notification_only.py.
"""
# pyright: reportAttributeAccessIssue=false
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import adapters  # noqa: E402


def test_from_header_flattened_suffix_intact():
    d = adapters.wake_detail("evil@x.invalid\r\n  hdr  junk", [object()])
    assert d == "new mail from evil@x.invalid hdr junk (1 attachments)", d
    assert "\n" not in d and "\r" not in d


def test_suffix_survives_a_hostile_long_address():
    """The fragment is capped so the detail stays within the 160-char
    injection budget; the suffix is appended after the cap."""
    d = adapters.wake_detail("A" * 500, [object(), object()])
    assert d.endswith("(2 attachments)"), d
    assert len(d) <= 160, len(d)
    assert "\n" not in d and "\r" not in d


def test_ruled_shape_unchanged_for_normal_headers():
    assert adapters.wake_detail("sender@example.com", []) == \
        "new mail from sender@example.com"
    assert adapters.wake_detail("sender@example.com", [object(), object()]) \
        == "new mail from sender@example.com (2 attachments)"
