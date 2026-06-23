"""Tests for the deterministic eval seed corpus (lode-5y8.4).

Acceptance (lode-5y8.4): a committed set of input note bodies loads
deterministically into reproducible ``version_id``s the golden citations
reference. The load-bearing test is the **pinned snapshot** below: if a body, the
hash framing, or ``H`` ever changes, these literal ids change and the test fails
loudly -- which is exactly the early warning that a golden citation just went
stale.

The content-hash framing itself (boundary ambiguity, field folding, the
``blake2b`` fallback) is owned and tested by ``lode.hashing`` (lode-s2f.2); this
fixture only imports :func:`lode.hashing.content_version_id`, so those properties
are not re-asserted here.
"""

from lode.eval.seed import SeedNote, seed_notes
from lode.hashing import NO_PARENT, content_version_id

# Pinned snapshot: note_id -> expected version_id. Regenerated only when the
# corpus is *intentionally* changed (each change invalidates the golden citations
# that reference the old id, so this failing is a feature). See the module
# docstring of lode.eval.seed for how the id is derived.
EXPECTED_VERSION_IDS = {
    "01-postgres-autovacuum": "82edfc260c8861c33860ef9b06ab516b",
    "02-python-gil": "9dd5a58fa3aa80590bd375f423511f69",
    "03-incident-checkout-latency": "c2b334662a1291d919a36b670b53ee34",
    "04-k8s-probes": "620f8f31db0221fc8d779acc18f10b41",
    "05-git-bisect": "0f7b058cd5b8942c24b9508cdbe6dc72",
    "06-tls-cert-renewal": "d73ec8ec29300393c9700b4500fb6e3e",
    "07-redis-eviction": "d3452e0eb06cf614dac25da10f4d4445",
    "08-http-idempotency": "1fb5111f51b3924980e1d2333be7bbc2",
    "09-oncall-escalation": "dbbfa437066e0204ce0119e475b5eb47",
    "10-feature-flag-cleanup": "fc4c39e3dd5f9467552dfdace94bd53d",
}


def test_version_ids_match_pinned_snapshot() -> None:
    # The reproducibility guarantee: the committed bodies hash to these exact ids.
    got = {note.note_id: note.version_id for note in seed_notes()}
    assert got == EXPECTED_VERSION_IDS


def test_seed_notes_is_deterministic_across_calls() -> None:
    # Same fixture, byte-for-byte, on every call -- no run-to-run drift.
    assert seed_notes() == seed_notes()


def test_corpus_is_loaded_in_sorted_order() -> None:
    # Stable ordering is part of determinism (and keeps the golden set readable).
    note_ids = [note.note_id for note in seed_notes()]
    assert note_ids == sorted(note_ids)
    assert note_ids == list(EXPECTED_VERSION_IDS)


def test_every_note_has_nonempty_body() -> None:
    # A blank corpus file would silently weaken the eval; guard against it.
    for note in seed_notes():
        assert note.body.strip(), f"empty body for {note.note_id}"


def test_version_ids_are_unique() -> None:
    # Distinct bodies must yield distinct ids -- a collision would alias citations.
    version_ids = [note.version_id for note in seed_notes()]
    assert len(set(version_ids)) == len(version_ids)


def test_seed_note_version_id_matches_recomputation() -> None:
    # The SeedNote.version_id is exactly content_version_id of its own fields
    # (root create -> empty parent), not some separately stored value.
    for note in seed_notes():
        assert note.version_id == content_version_id(note.note_id, NO_PARENT, note.body)


def test_seed_notes_are_frozen() -> None:
    # SeedNote is an immutable record; the fixture must not be mutated in place.
    note = seed_notes()[0]
    assert isinstance(note, SeedNote)
    try:
        note.body = "tampered"  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("SeedNote should be frozen")
