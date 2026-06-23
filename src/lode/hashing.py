"""Framed content-address hashing for lode's version chains (lode-s2f.2).

This module is the SINGLE source of truth for lode's content-address ids
(``docs/storage.md``, "Identity vs version"): :func:`content_version_id` computes
a note version's ``version_id`` and :func:`content_snapshot_id` an external
snapshot's ``snapshot_id`` — both with the one framing pinned in the docs. The
version-save path (``lode-s2f.3``) and the eval seed fixture (``lode-5y8.4``)
both import these; there is no second implementation anywhere, so the ids cannot
drift.

Encoding (frozen in ``docs/storage.md``):

    version_id  = H( framed(note_id) || framed(parent) || framed(body) )
    snapshot_id = H( framed(external_id) || framed(body) )

where ``framed(field)`` prefixes the field's UTF-8 bytes with an 8-byte
big-endian unsigned length. Length-prefixing *every* field (not bare
concatenation) is what gives each one an unambiguous boundary, so
``H("a", "bc") != H("ab", "c")`` — the field-aliasing bug a content-addressed
store must not have. A root ``create`` has no parent, encoded as the empty
string (:data:`NO_PARENT`, an empty framed field). The docs present this
length-prefixed form *and* a hash-of-sub-hashes form, but they hash to different
values; the length-prefixed form pinned here is the one frozen encoding.

``H`` is the build constant ``config.content_hash`` (``docs/configuration.md``):
``xxh3-128`` by default, with stdlib ``blake2b-128`` as the zero-dependency
fallback. Both hashers frame their input identically, so swapping ``H`` re-keys
every id consistently — it never changes the framing.
"""

import hashlib

import xxhash

from lode.config import Settings

#: Width, in bytes, of the big-endian unsigned length prefix on each hashed
#: field. 8 bytes (uint64) cannot overflow on any realistic note body and keeps
#: the framing fixed-width. Part of the pinned encoding (``docs/storage.md``);
#: changing it re-keys every id.
_FRAME_LEN_BYTES = 8

#: The parent of a fresh ``create`` version: there is none, encoded as the empty
#: string (an empty framed field — an 8-byte zero length, then no bytes). Matches
#: ``op: create`` in ``docs/storage.md``.
NO_PARENT = ""


def _framed(field: str) -> bytes:
    """Length-prefix one hash field: ``len(utf8) as uint64 BE`` then the UTF-8 bytes."""
    encoded = field.encode("utf-8")
    return len(encoded).to_bytes(_FRAME_LEN_BYTES, "big") + encoded


def _digest(data: bytes, content_hash: str) -> str:
    """Hash ``data`` with the configured ``H``, returning a lowercase hex digest.

    ``xxh3-128`` is the default; ``blake2b-128`` is the stdlib no-dep fallback
    (``hashlib.blake2b`` truncated to 16 bytes). An unknown name fails loudly:
    ``H`` is a build constant, so a typo must surface rather than silently
    mis-key every id.
    """
    if content_hash == "xxh3-128":
        return xxhash.xxh3_128_hexdigest(data)
    if content_hash == "blake2b-128":
        return hashlib.blake2b(data, digest_size=16).hexdigest()
    raise ValueError(
        f"unsupported content_hash {content_hash!r}; expected 'xxh3-128' or 'blake2b-128'"
    )


def _content_id(fields: tuple[str, ...], settings: Settings | None) -> str:
    """Hash an ordered tuple of framed string fields into a content-address id.

    Each field is independently length-prefixed (:func:`_framed`) and the framed
    fields are concatenated in order, so the boundaries between them are
    unambiguous regardless of the field contents.
    """
    settings = settings or Settings()
    framed = b"".join(_framed(field) for field in fields)
    return _digest(framed, settings.content_hash)


def content_version_id(
    note_id: str,
    parent_version_id: str,
    body: str,
    settings: Settings | None = None,
) -> str:
    """The content-address ``version_id`` for one note version (``docs/storage.md``).

    ``version_id = H(framed(note_id) || framed(parent) || framed(body))`` as a
    lowercase hex digest. Folding in ``note_id`` makes cross-note collisions
    impossible; folding in the parent keeps each chain position distinct even on
    a revert to an earlier body. A root ``create`` passes :data:`NO_PARENT` as
    ``parent_version_id``.
    """
    return _content_id((note_id, parent_version_id, body), settings)


def content_snapshot_id(
    external_id: str,
    body: str,
    settings: Settings | None = None,
) -> str:
    """The content-address ``snapshot_id`` for one external snapshot (``docs/storage.md``).

    ``snapshot_id = H(framed(external_id) || framed(body))`` as a lowercase hex
    digest — the same framing as :func:`content_version_id`, over the snapshot's
    two identity fields. Externals/snapshots are created but unused until step 2;
    the id encoding is pinned here so it cannot drift.
    """
    return _content_id((external_id, body), settings)
