"""Tests for lode.hashing — framed content-address ids (lode-s2f.2).

Asserts the acceptance criteria: framing is unambiguous (``H('a','bc') !=
H('ab','c')``), ``version_id`` is reproducible for identical inputs, and the
``blake2b-128`` stdlib fallback path is exercised.
"""

import hashlib

import pytest

from lode.config import load_settings
from lode.hashing import (
    NO_PARENT,
    content_snapshot_id,
    content_version_id,
)

_HEX128 = 32  # a 128-bit digest is 32 lowercase hex chars

_BLAKE = load_settings(content_hash="blake2b-128")


def test_framing_is_unambiguous_across_field_boundary():
    """The classic aliasing case: shifting the boundary must change the id.

    With bare concatenation ``"a"+"bc" == "ab"+"c"`` would collide; length-
    prefixing every field keeps them distinct. Checked on both hashers.
    """
    # note_id / parent split, identical trailing body.
    assert content_version_id("a", "bc", "body") != content_version_id(
        "ab", "c", "body"
    )
    # parent / body split, identical leading note_id.
    assert content_version_id("n", "a", "bc") != content_version_id("n", "ab", "c")
    # Same property must hold for the fallback hasher.
    assert content_version_id("a", "bc", "body", _BLAKE) != content_version_id(
        "ab", "c", "body", _BLAKE
    )


def test_snapshot_framing_is_unambiguous():
    """``snapshot_id`` frames its two fields, so its boundary is unambiguous too."""
    assert content_snapshot_id("a", "bc") != content_snapshot_id("ab", "c")


def test_version_id_is_reproducible_and_well_formed():
    """Identical inputs hash to the identical lowercase-hex 128-bit digest."""
    first = content_version_id("note-1", NO_PARENT, "hello")
    second = content_version_id("note-1", NO_PARENT, "hello")
    assert first == second
    assert len(first) == _HEX128
    assert first == first.lower()
    int(first, 16)  # pure hex


def test_distinct_inputs_diverge():
    """Each field participates: changing any one changes the id."""
    base = content_version_id("note-1", NO_PARENT, "hello")
    assert base != content_version_id("note-2", NO_PARENT, "hello")  # note_id
    assert base != content_version_id("note-1", "v0", "hello")  # parent
    assert base != content_version_id("note-1", NO_PARENT, "world")  # body


def test_no_parent_is_the_empty_string():
    """A root ``create`` uses the empty-string parent; the constant must match it."""
    assert NO_PARENT == ""
    assert content_version_id("n", NO_PARENT, "b") == content_version_id("n", "", "b")


def test_blake2b_fallback_path_matches_manual_framing():
    """Exercise the stdlib fallback and verify it frames exactly as documented.

    ``framed(field) = len(utf8) as 8-byte BE || utf8``; the fallback is
    ``blake2b`` truncated to 16 bytes. Recomputing the framing by hand and
    hashing it must reproduce the module's output.
    """

    def framed(field: str) -> bytes:
        b = field.encode("utf-8")
        return len(b).to_bytes(8, "big") + b

    note_id, parent, body = "note-1", "", "hello world"
    expected = hashlib.blake2b(
        framed(note_id) + framed(parent) + framed(body), digest_size=16
    ).hexdigest()
    assert content_version_id(note_id, parent, body, _BLAKE) == expected
    assert len(expected) == _HEX128


def test_hashers_differ_for_same_input():
    """xxh3-128 and blake2b-128 are different functions over the same framed bytes."""
    default = content_version_id("note-1", NO_PARENT, "hello")
    fallback = content_version_id("note-1", NO_PARENT, "hello", _BLAKE)
    assert default != fallback


def test_unknown_hash_name_fails_loudly():
    """A build-constant typo must raise, not silently mis-key ids."""
    bad = load_settings(content_hash="md5-128")
    with pytest.raises(ValueError, match="unsupported content_hash"):
        content_version_id("n", NO_PARENT, "b", bad)
