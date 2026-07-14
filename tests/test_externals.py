"""Tests for lode.externals — the mirrored-snapshot write path (lode-w0h.2).

Covers the acceptance criteria: a fetched page becomes one ``externals`` row +
one ``snapshots`` row; an identical refetch adds no row; a changed body adds a
new snapshot and moves ``head_snapshot_id``; a fetch failure writes a
tombstone snapshot (not scaffolding); an ``embed`` job (never ``enrich``) is
enqueued on every non-deduped ``ok`` ingest; and a ``tombstone`` ingest
enqueues no ``embed`` job at all (decision, bd lode-w0h.2, 2026-07-08 — a
failed fetch must not become a retrievable/citable vector).

Also covers the read-enablement extension (lode-c5l, rebuild of the bounced
lode-w0h.8): a non-deduped ``ok`` ingest synchronously drives the FTS leg
(``passages`` + ``passages_fts``), chunking ``redact_before_index(body)`` —
never the raw body — so a secret in a fetched page never lands in the local
index; a ``tombstone`` gets neither the FTS write nor the ``embed`` enqueue.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lode.config import load_settings
from lode.embedding import embed
from lode.hashing import content_snapshot_id
from lode.externals import (
    IngestResult,
    gate_reenrich,
    head_snapshot_info,
    ingest_fetch_result,
    ingest_snapshot,
    set_no_egress,
    tombstone_body,
)
from lode.storage import init_db
from lode.vectorstore import VectorStore
from lode.webfetch import FetchResult, FetchStatus

_EXTERNAL_ID = "https://example.com/article"


@pytest.fixture
def conn(tmp_path: Path):
    c = init_db(tmp_path / "lode.db")
    try:
        yield c
    finally:
        c.close()


def _external_row(conn, external_id: str):
    return conn.execute(
        "SELECT source_type, head_snapshot_id FROM externals WHERE external_id = ?",
        (external_id,),
    ).fetchone()


def _count_snapshots(conn, external_id: str) -> int:
    (n,) = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE external_id = ?", (external_id,)
    ).fetchone()
    return n


def _jobs_for(conn, target_version: str):
    return conn.execute(
        "SELECT type, status FROM jobs WHERE target_version = ? ORDER BY type",
        (target_version,),
    ).fetchall()


# --- fresh ingest -------------------------------------------------------------


def test_fresh_ingest_writes_one_external_and_one_snapshot(conn) -> None:
    result = ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")

    assert not result.deduped
    assert result.status == "ok"
    assert result.snapshot_id == content_snapshot_id(_EXTERNAL_ID, "hello world")
    assert _external_row(conn, _EXTERNAL_ID) == ("web", result.snapshot_id)
    assert _count_snapshots(conn, _EXTERNAL_ID) == 1
    (body, raw_payload, status) = conn.execute(
        "SELECT body, raw_payload, status FROM snapshots WHERE snapshot_id = ?",
        (result.snapshot_id,),
    ).fetchone()
    assert (body, raw_payload, status) == ("hello world", None, "ok")


def test_fresh_ingest_enqueues_embed_only_not_enrich(conn) -> None:
    result = ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")

    assert _jobs_for(conn, result.snapshot_id) == [("embed", "pending")]


def test_raw_payload_is_stored_alongside_body(conn) -> None:
    result = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", "hello world", raw_payload="<html>hello world</html>"
    )

    (raw_payload,) = conn.execute(
        "SELECT raw_payload FROM snapshots WHERE snapshot_id = ?", (result.snapshot_id,)
    ).fetchone()
    assert raw_payload == "<html>hello world</html>"


# --- dedup on identical refetch -----------------------------------------------


def test_identical_refetch_adds_no_row_and_no_new_job(conn) -> None:
    first = ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")
    second = ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")

    assert second.deduped
    assert second.snapshot_id == first.snapshot_id
    assert _count_snapshots(conn, _EXTERNAL_ID) == 1
    # No second embed job was enqueued for the (unchanged) snapshot id.
    assert _jobs_for(conn, first.snapshot_id) == [("embed", "pending")]


def test_dedup_does_not_touch_existing_externals_row(conn) -> None:
    ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")
    (n_before,) = conn.execute("SELECT COUNT(*) FROM externals").fetchone()

    ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")

    (n_after,) = conn.execute("SELECT COUNT(*) FROM externals").fetchone()
    assert n_before == n_after == 1


# --- changed body moves the head ----------------------------------------------


def test_changed_body_adds_a_new_snapshot_and_moves_head(conn) -> None:
    first = ingest_snapshot(conn, _EXTERNAL_ID, "web", "version one")
    second = ingest_snapshot(conn, _EXTERNAL_ID, "web", "version two")

    assert not second.deduped
    assert second.snapshot_id != first.snapshot_id
    assert _count_snapshots(conn, _EXTERNAL_ID) == 2
    assert _external_row(conn, _EXTERNAL_ID) == ("web", second.snapshot_id)
    # Both snapshots enqueued their own embed job.
    assert _jobs_for(conn, first.snapshot_id) == [("embed", "pending")]
    assert _jobs_for(conn, second.snapshot_id) == [("embed", "pending")]


def test_revert_to_a_prior_body_reproduces_the_prior_snapshot_id(conn) -> None:
    """Content addressing, not history: A -> B -> A re-heads onto A's original id."""
    first = ingest_snapshot(conn, _EXTERNAL_ID, "web", "version A")
    ingest_snapshot(conn, _EXTERNAL_ID, "web", "version B")
    third = ingest_snapshot(conn, _EXTERNAL_ID, "web", "version A")

    assert third.snapshot_id == first.snapshot_id
    assert not third.deduped  # head had moved to B, so this IS a head-move
    assert _count_snapshots(conn, _EXTERNAL_ID) == 2  # no 3rd row for a repeat body
    assert _external_row(conn, _EXTERNAL_ID) == ("web", first.snapshot_id)


# --- tombstone on fetch failure ------------------------------------------------


def test_fetch_failure_writes_a_tombstone_snapshot_not_garbage(conn) -> None:
    result = ingest_snapshot(
        conn,
        _EXTERNAL_ID,
        "web",
        tombstone_body("http_403"),
        status="tombstone",
    )

    assert result.status == "tombstone"
    (body, status) = conn.execute(
        "SELECT body, status FROM snapshots WHERE snapshot_id = ?",
        (result.snapshot_id,),
    ).fetchone()
    assert body == "[tombstone: http_403]"
    assert status == "tombstone"


def test_repeated_identical_tombstone_reason_dedups(conn) -> None:
    """A persistently-dead source doesn't spam a new row on every retry."""
    first = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", tombstone_body("http_404"), status="tombstone"
    )
    second = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", tombstone_body("http_404"), status="tombstone"
    )

    assert second.deduped
    assert second.snapshot_id == first.snapshot_id
    assert _count_snapshots(conn, _EXTERNAL_ID) == 1


def test_tombstone_after_ok_moves_head_but_enqueues_no_embed(conn) -> None:
    ok_result = ingest_snapshot(conn, _EXTERNAL_ID, "web", "real content")
    tomb_result = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", tombstone_body("http_410"), status="tombstone"
    )

    assert tomb_result.snapshot_id != ok_result.snapshot_id
    assert _external_row(conn, _EXTERNAL_ID) == ("web", tomb_result.snapshot_id)
    # The head moved to the tombstone row, but a tombstone must never become a
    # retrievable/citable vector — no embed job for it (bd lode-w0h.2 decision).
    assert _jobs_for(conn, tomb_result.snapshot_id) == []


def test_tombstone_ingest_enqueues_no_embed_job(conn) -> None:
    """A status='tombstone' snapshot enqueues NO embed job — fail closed.

    (An 'ok' snapshot still enqueues exactly one embed job — already covered
    by test_fresh_ingest_enqueues_embed_only_not_enrich above.)
    """
    result = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", tombstone_body("http_403"), status="tombstone"
    )

    assert _jobs_for(conn, result.snapshot_id) == []


# --- head_snapshot_info (lode-uda1) --------------------------------------


def test_head_snapshot_info_none_when_no_externals_row(conn) -> None:
    assert head_snapshot_info(conn, _EXTERNAL_ID) is None


def test_head_snapshot_info_reflects_current_head(conn) -> None:
    result = ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")

    info = head_snapshot_info(conn, _EXTERNAL_ID)
    assert info is not None
    status, fetched_at = info
    assert status == "ok"
    (expected_fetched_at,) = conn.execute(
        "SELECT fetched_at FROM snapshots WHERE snapshot_id = ?",
        (result.snapshot_id,),
    ).fetchone()
    assert fetched_at == expected_fetched_at


def test_head_snapshot_info_reflects_tombstone_head(conn) -> None:
    ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")
    ingest_snapshot(
        conn, _EXTERNAL_ID, "web", tombstone_body("http_410"), status="tombstone"
    )

    status, _fetched_at = head_snapshot_info(conn, _EXTERNAL_ID)
    assert status == "tombstone"


# --- skip_if_head_at_or_after guard (lode-elc8) ------------------------------
#
# ingest_snapshot's atomic replacement for the old, separate
# head_snapshot_info-then-ingest_snapshot read-then-write (lode-uda1's
# original guard shape, which docs/storage.md records as narrowed but not
# closed). These are single-connection, synchronous tests of the guard's
# boolean logic; the genuinely-concurrent proof that the check is atomic
# with the write lives in tests/test_worker.py
# (test_reclaim_dead_letter_hook_guard_is_atomic_under_genuine_concurrency).


def _iso_plus(ts: str, seconds: float) -> str:
    """``ts`` (the schema's millisecond ISO-8601 format) shifted by ``seconds``."""
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    return (dt + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def test_skip_if_head_at_or_after_skips_when_head_at_the_boundary(conn) -> None:
    """The guard is inclusive (">="): a head fetched EXACTLY at the guard
    timestamp still counts as "a real fetch beat the verdict" and the whole
    write -- no snapshot row, no head move -- is skipped.
    """
    ok_result = ingest_snapshot(conn, _EXTERNAL_ID, "web", "the real content")
    (head_fetched_at,) = conn.execute(
        "SELECT fetched_at FROM snapshots WHERE snapshot_id = ?",
        (ok_result.snapshot_id,),
    ).fetchone()

    guarded = ingest_snapshot(
        conn,
        _EXTERNAL_ID,
        "web",
        tombstone_body("dead: timeout"),
        status="tombstone",
        skip_if_head_at_or_after=head_fetched_at,
    )

    assert guarded is None
    assert _external_row(conn, _EXTERNAL_ID) == ("web", ok_result.snapshot_id)
    assert _count_snapshots(conn, _EXTERNAL_ID) == 1


def test_skip_if_head_at_or_after_skips_when_head_strictly_after(conn) -> None:
    """The intended case: the head was fetched some time AFTER the guard
    timestamp (a real, later fetch already landed) -- still skipped.
    """
    ok_result = ingest_snapshot(conn, _EXTERNAL_ID, "web", "the real content")
    (head_fetched_at,) = conn.execute(
        "SELECT fetched_at FROM snapshots WHERE snapshot_id = ?",
        (ok_result.snapshot_id,),
    ).fetchone()
    earlier_claim = _iso_plus(head_fetched_at, -60)

    guarded = ingest_snapshot(
        conn,
        _EXTERNAL_ID,
        "web",
        tombstone_body("dead: timeout"),
        status="tombstone",
        skip_if_head_at_or_after=earlier_claim,
    )

    assert guarded is None
    assert _external_row(conn, _EXTERNAL_ID) == ("web", ok_result.snapshot_id)
    assert _count_snapshots(conn, _EXTERNAL_ID) == 1


def test_skip_if_head_at_or_after_does_not_block_when_head_predates_guard(conn) -> None:
    """The pre-existing, intentional case the guard must NOT affect: a LATER
    dead-letter (claimed_at after the head's own fetch) still tombstones
    even though the external already has OLDER 'ok' content -- unaffected
    by lode-uda1/lode-elc8 (docs/externals.md "Fetch-outcome taxonomy").
    """
    ok_result = ingest_snapshot(conn, _EXTERNAL_ID, "web", "the old, still-live body")
    (head_fetched_at,) = conn.execute(
        "SELECT fetched_at FROM snapshots WHERE snapshot_id = ?",
        (ok_result.snapshot_id,),
    ).fetchone()
    later_claim = _iso_plus(head_fetched_at, 3600)

    tombstoned = ingest_snapshot(
        conn,
        _EXTERNAL_ID,
        "web",
        tombstone_body("dead: timeout"),
        status="tombstone",
        skip_if_head_at_or_after=later_claim,
    )

    assert tombstoned is not None
    assert tombstoned.status == "tombstone"
    assert _external_row(conn, _EXTERNAL_ID) == ("web", tombstoned.snapshot_id)
    assert _count_snapshots(conn, _EXTERNAL_ID) == 2


def test_skip_if_head_at_or_after_does_not_block_a_tombstone_head(conn) -> None:
    """A tombstone head never satisfies the guard (only a non-'tombstone'
    head fetched at-or-after the claim does) -- a second dead-letter must
    still write over an existing tombstone.
    """
    first = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", tombstone_body("dead: first"), status="tombstone"
    )

    second = ingest_snapshot(
        conn,
        _EXTERNAL_ID,
        "web",
        tombstone_body("dead: second"),
        status="tombstone",
        skip_if_head_at_or_after="1970-01-01T00:00:00.000Z",
    )

    assert second is not None
    assert second.snapshot_id != first.snapshot_id
    assert _external_row(conn, _EXTERNAL_ID) == ("web", second.snapshot_id)


def test_skip_if_head_at_or_after_none_disables_guard_on_no_head_yet(conn) -> None:
    """No externals row yet -- the guard has nothing to compare against, so
    it must not skip; the first-ever ingest for an external_id proceeds
    normally even when a guard timestamp is passed.
    """
    result = ingest_snapshot(
        conn,
        _EXTERNAL_ID,
        "web",
        tombstone_body("dead: first ever"),
        status="tombstone",
        skip_if_head_at_or_after="2020-01-01T00:00:00.000Z",
    )

    assert result is not None
    assert _external_row(conn, _EXTERNAL_ID) == ("web", result.snapshot_id)


# --- ingest_fetch_result adapter (webfetch.FetchResult -> ingest_snapshot) ---


def test_ingest_fetch_result_ok_uses_clean_text_and_raw_html(conn) -> None:
    fetch = FetchResult(
        status=FetchStatus.OK,
        final_url=_EXTERNAL_ID,
        clean_text="A Real Article body.",
        raw_html="<html>A Real Article body.</html>",
        http_status=200,
        tombstone_reason=None,
    )

    result = ingest_fetch_result(conn, _EXTERNAL_ID, "web", fetch)

    assert result.status == "ok"
    (body, raw_payload) = conn.execute(
        "SELECT body, raw_payload FROM snapshots WHERE snapshot_id = ?",
        (result.snapshot_id,),
    ).fetchone()
    assert body == "A Real Article body."
    assert raw_payload == "<html>A Real Article body.</html>"


def test_ingest_fetch_result_tombstone_uses_tombstone_reason(conn) -> None:
    fetch = FetchResult(
        status=FetchStatus.TOMBSTONE,
        final_url=_EXTERNAL_ID,
        clean_text=None,
        raw_html="<html>nope</html>",
        http_status=403,
        tombstone_reason="http_403",
    )

    result = ingest_fetch_result(conn, _EXTERNAL_ID, "web", fetch)

    assert result.status == "tombstone"
    (body, raw_payload) = conn.execute(
        "SELECT body, raw_payload FROM snapshots WHERE snapshot_id = ?",
        (result.snapshot_id,),
    ).fetchone()
    assert body == "[tombstone: http_403]"
    assert raw_payload == "<html>nope</html>"


def test_ingest_fetch_result_redirect_cap_tombstone_has_no_raw_payload(conn) -> None:
    """A too-many-redirects tombstone carries neither clean_text nor raw_html."""
    fetch = FetchResult(
        status=FetchStatus.TOMBSTONE,
        final_url=_EXTERNAL_ID,
        clean_text=None,
        raw_html=None,
        http_status=None,
        tombstone_reason="too_many_redirects",
    )

    result = ingest_fetch_result(conn, _EXTERNAL_ID, "web", fetch)

    (body, raw_payload) = conn.execute(
        "SELECT body, raw_payload FROM snapshots WHERE snapshot_id = ?",
        (result.snapshot_id,),
    ).fetchone()
    assert body == "[tombstone: too_many_redirects]"
    assert raw_payload is None


# --- synchronous FTS on ingest (lode-c5l) ---------------------------------------


def _fts_hits(conn, target_version: str):
    return conn.execute(
        "SELECT passage_id FROM passages_fts WHERE target_version = ?",
        (target_version,),
    ).fetchall()


def test_ok_ingest_synchronously_populates_passages_and_fts(conn) -> None:
    result = ingest_snapshot(conn, _EXTERNAL_ID, "web", "alpha content here")

    (passage_count,) = conn.execute(
        "SELECT COUNT(*) FROM passages WHERE target_version = ?",
        (result.snapshot_id,),
    ).fetchone()
    assert passage_count >= 1
    assert len(_fts_hits(conn, result.snapshot_id)) == passage_count
    # Directly keyword-findable via FTS5, no embed worker involved.
    rows = conn.execute(
        "SELECT passage_id FROM passages_fts WHERE passages_fts MATCH 'alpha'"
    ).fetchall()
    assert rows


def test_changed_body_reindexes_fts_for_the_new_head_only(conn) -> None:
    first = ingest_snapshot(conn, _EXTERNAL_ID, "web", "alpha")
    second = ingest_snapshot(conn, _EXTERNAL_ID, "web", "beta")

    # Both snapshots keep their own passage rows (soft history, like note heads)...
    assert _fts_hits(conn, first.snapshot_id)
    assert _fts_hits(conn, second.snapshot_id)
    # ...but each snapshot's FTS rows carry only its own body's terms.
    rows = conn.execute(
        "SELECT passage_id FROM passages_fts WHERE passages_fts MATCH 'alpha'"
    ).fetchall()
    assert {r[0] for r in rows} == {p[0] for p in _fts_hits(conn, first.snapshot_id)}


def test_tombstone_ingest_writes_no_passages_or_fts_rows(conn) -> None:
    result = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", tombstone_body("http_403"), status="tombstone"
    )

    (passage_count,) = conn.execute(
        "SELECT COUNT(*) FROM passages WHERE target_version = ?",
        (result.snapshot_id,),
    ).fetchone()
    assert passage_count == 0
    assert _fts_hits(conn, result.snapshot_id) == []


def test_deduped_identical_refetch_does_not_rewrite_fts(conn) -> None:
    first = ingest_snapshot(conn, _EXTERNAL_ID, "web", "alpha content")
    (before,) = conn.execute(
        "SELECT COUNT(*) FROM passages_fts WHERE target_version = ?",
        (first.snapshot_id,),
    ).fetchone()

    ingest_snapshot(conn, _EXTERNAL_ID, "web", "alpha content")  # identical refetch

    (after,) = conn.execute(
        "SELECT COUNT(*) FROM passages_fts WHERE target_version = ?",
        (first.snapshot_id,),
    ).fetchone()
    assert after == before


# --- redact-before-index on the FTS leg (lode-c5l bounce fix) -------------------
#
# THE BOUNCE: the predecessor branch (land/lode-w0h.8 @ 53caca8) chunked the
# RAW body on the FTS leg while the vector leg (embed) independently redacted
# it — a split-brain that made a pasted secret directly keyword-retrievable,
# violating the redact-before-index invariant (lode-n60) docs/externals.md
# spells out. These are the regression tests the bounce ticket required.


def test_ok_ingest_redacts_a_secret_before_fts_index(conn) -> None:
    """(a)+(b)+(d) of the acceptance criteria: FTS/passages hold the redacted
    form, never the secret; snapshots.body still holds the original."""
    secret = "AKIAIOSFODNN7EXAMPLE"  # seeded AWS-access-key-id pattern
    body = f"mirrored page contents\ncreds: {secret} keep private\n"

    result = ingest_snapshot(conn, _EXTERNAL_ID, "web", body)

    fts_rows = conn.execute(
        "SELECT text FROM passages_fts WHERE target_version = ?",
        (result.snapshot_id,),
    ).fetchall()
    assert fts_rows, "sanity: the body chunked to at least one passage"
    assert not any(secret in text for (text,) in fts_rows)
    passage_rows = conn.execute(
        "SELECT text FROM passages WHERE target_version = ?",
        (result.snapshot_id,),
    ).fetchall()
    assert not any(secret in text for (text,) in passage_rows)
    # snapshots.body (the irreplaceable mirrored copy) still carries the raw
    # secret — only the text handed to chunk() is redacted, exactly as
    # versions.body is never touched by the note-side redaction either.
    (stored_body,) = conn.execute(
        "SELECT body FROM snapshots WHERE snapshot_id = ?", (result.snapshot_id,)
    ).fetchone()
    assert secret in stored_body


def test_leg_parity_fts_and_embed_chunk_identical_redacted_text(
    conn, tmp_path: Path
) -> None:
    """No orphaned trailing passages row after the embed worker drains.

    LexicalCacheBackend.index documents that "the embed worker re-writes the
    same rows later (same deterministic passage_ids)" — that assumption only
    holds if the FTS leg and the vector leg chunk IDENTICAL text. Before the
    lode-c5l fix, FTS chunked the raw body and embed chunked the redacted
    body: if redaction shortened the body enough to drop a trailing chunk,
    embed's INSERT OR REPLACE would overwrite ords 0..n-1 and leave a
    trailing raw-text passages row orphaned (which expand_parents would then
    build Q&A context from). Asserting the two legs converge to the exact
    same passages rows (ids, ords, and text) closes that gap.
    """

    class _StubEmbedder:
        def embed_passages(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] for _ in texts]

    secret = "AKIAIOSFODNN7EXAMPLE"
    body = (
        f"mirrored page contents\ncreds: {secret} keep private\n"
        "## More section\nAdditional prose so the body chunks into more than "
        "one passage, exercising the trailing-chunk orphan case.\n"
    )
    # Small overridden vector dim so the stub embedder's 1-wide vectors match
    # the LanceDB table's fixed schema width (mirrors tests/test_embedding.py).
    settings = load_settings(embedding_vector_dim=1)

    result = ingest_snapshot(conn, _EXTERNAL_ID, "web", body, settings=settings)
    fts_leg_rows = conn.execute(
        "SELECT passage_id, ord, char_range, text FROM passages "
        "WHERE target_version = ? ORDER BY ord",
        (result.snapshot_id,),
    ).fetchall()

    embed(
        conn,
        result.snapshot_id,
        lance_dir=tmp_path / "vectors",
        embedder=_StubEmbedder(),
        settings=settings,
    )
    after_embed_rows = conn.execute(
        "SELECT passage_id, ord, char_range, text FROM passages "
        "WHERE target_version = ? ORDER BY ord",
        (result.snapshot_id,),
    ).fetchall()

    assert fts_leg_rows, "sanity: the body chunked to at least one passage"
    assert after_embed_rows == fts_leg_rows  # no orphaned/diverging rows
    assert not any(secret in row[3] for row in after_embed_rows)


# --- IngestResult shape --------------------------------------------------------


def test_ingest_result_is_frozen_dataclass_shape() -> None:
    result = IngestResult("ext-1", "snap-1", "ok", deduped=True)
    assert result.external_id == "ext-1"
    assert result.snapshot_id == "snap-1"
    assert result.status == "ok"
    assert result.deduped is True


# --- set_no_egress: the no-egress control surface (lode-w0h.7) ----------------


def test_set_no_egress_flips_the_flag_on_an_existing_external(conn) -> None:
    ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")

    existed = set_no_egress(conn, _EXTERNAL_ID)

    assert existed is True
    (no_egress,) = conn.execute(
        "SELECT no_egress FROM externals WHERE external_id = ?", (_EXTERNAL_ID,)
    ).fetchone()
    assert no_egress == 1


def test_set_no_egress_clear_flips_it_back(conn) -> None:
    ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")
    set_no_egress(conn, _EXTERNAL_ID)

    existed = set_no_egress(conn, _EXTERNAL_ID, no_egress=False)

    assert existed is True
    (no_egress,) = conn.execute(
        "SELECT no_egress FROM externals WHERE external_id = ?", (_EXTERNAL_ID,)
    ).fetchone()
    assert no_egress == 0


def test_set_no_egress_unknown_external_returns_false_and_writes_nothing(
    conn,
) -> None:
    existed = set_no_egress(conn, "https://never-ingested.example/page")

    assert existed is False
    assert conn.execute("SELECT COUNT(*) FROM externals").fetchone()[0] == 0


def test_set_no_egress_never_touches_indexing_or_retrieval(conn) -> None:
    """no_egress gates egress only -- the flag flip alone changes no other row."""
    result = ingest_snapshot(conn, _EXTERNAL_ID, "web", "hello world")
    before_passages = conn.execute(
        "SELECT COUNT(*) FROM passages WHERE target_version = ?",
        (result.snapshot_id,),
    ).fetchone()[0]
    before_jobs = _jobs_for(conn, result.snapshot_id)

    set_no_egress(conn, _EXTERNAL_ID)

    after_passages = conn.execute(
        "SELECT COUNT(*) FROM passages WHERE target_version = ?",
        (result.snapshot_id,),
    ).fetchone()[0]
    assert after_passages == before_passages
    assert _jobs_for(conn, result.snapshot_id) == before_jobs
    assert _external_row(conn, _EXTERNAL_ID) == ("web", result.snapshot_id)


# --- gate_reenrich: material-change re-enrich gating (lode-w0h.5) --------------
#
# Vectors are written directly via VectorStore (mirrors tests/test_vectorstore.py)
# rather than through the real embed leg -- gate_reenrich only ever reads
# VectorStore.vectors_for, so this isolates the gate's own decision logic from
# fastembed/chunking. A small vector dim keeps the test vectors trivial; the
# production dim is the pinned build constant.

_DIM = 4


def _gate_settings(**overrides):
    return load_settings(embedding_vector_dim=_DIM, **overrides)


def _write_vector(
    lance_dir: Path, target_version: str, vector: list[float], settings
) -> None:
    VectorStore(lance_dir, settings).replace_vectors(
        target_version,
        [
            {
                "passage_id": f"{target_version}:0",
                "target_version": target_version,
                "vector": vector,
                "model": settings.embedding_model,
            }
        ],
    )


def test_gate_reenrich_first_snapshot_is_material(conn, tmp_path: Path) -> None:
    """No predecessor to compare against -- nothing to carry forward either."""
    settings = _gate_settings()
    lance_dir = tmp_path / "vectors"
    result = ingest_snapshot(conn, _EXTERNAL_ID, "web", "first body", settings=settings)
    _write_vector(lance_dir, result.snapshot_id, [1.0, 0.0, 0.0, 0.0], settings)

    outcome = gate_reenrich(
        conn, result.snapshot_id, lance_dir=lance_dir, settings=settings
    )

    assert outcome is not None and "material" in outcome
    assert _jobs_for(conn, result.snapshot_id) == [
        ("embed", "pending"),
        ("enrich", "pending"),
    ]


def test_gate_reenrich_predecessor_never_embedded_is_material(
    conn, tmp_path: Path
) -> None:
    """Predecessor exists (e.g. a tombstone) but has no vectors -- no baseline -> material."""
    settings = _gate_settings()
    lance_dir = tmp_path / "vectors"
    ingest_snapshot(conn, _EXTERNAL_ID, "web", "version one", settings=settings)
    second = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", "version two", settings=settings
    )
    _write_vector(lance_dir, second.snapshot_id, [1.0, 0.0, 0.0, 0.0], settings)

    outcome = gate_reenrich(
        conn, second.snapshot_id, lance_dir=lance_dir, settings=settings
    )

    assert outcome is not None and "material" in outcome
    assert ("enrich", "pending") in _jobs_for(conn, second.snapshot_id)


def test_gate_reenrich_identical_vectors_is_immaterial_enqueues_no_enrich(
    conn, tmp_path: Path
) -> None:
    settings = _gate_settings()
    lance_dir = tmp_path / "vectors"
    first = ingest_snapshot(conn, _EXTERNAL_ID, "web", "version one", settings=settings)
    second = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", "version two", settings=settings
    )
    _write_vector(lance_dir, first.snapshot_id, [1.0, 0.0, 0.0, 0.0], settings)
    _write_vector(
        lance_dir, second.snapshot_id, [1.0, 0.0, 0.0, 0.0], settings
    )  # delta 0.0

    outcome = gate_reenrich(
        conn, second.snapshot_id, lance_dir=lance_dir, settings=settings
    )

    assert outcome is not None and "immaterial" in outcome
    assert _jobs_for(conn, second.snapshot_id) == [
        ("embed", "pending")
    ]  # no enrich enqueued


def test_gate_reenrich_orthogonal_vectors_is_material_enqueues_enrich(
    conn, tmp_path: Path
) -> None:
    settings = _gate_settings()
    lance_dir = tmp_path / "vectors"
    first = ingest_snapshot(conn, _EXTERNAL_ID, "web", "version one", settings=settings)
    second = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", "version two", settings=settings
    )
    _write_vector(lance_dir, first.snapshot_id, [1.0, 0.0, 0.0, 0.0], settings)
    _write_vector(
        lance_dir, second.snapshot_id, [0.0, 1.0, 0.0, 0.0], settings
    )  # delta 1.0

    outcome = gate_reenrich(
        conn, second.snapshot_id, lance_dir=lance_dir, settings=settings
    )

    assert outcome is not None and "material" in outcome
    assert ("enrich", "pending") in _jobs_for(conn, second.snapshot_id)


def test_gate_reenrich_immaterial_reanchors_matching_annotation_to_new_snapshot(
    conn, tmp_path: Path
) -> None:
    """A verbatim-matching AI annotation carries forward: source_version advances,
    status stays 'fresh' -- exactly Repository.save's re-anchor mechanism, reused
    here rather than duplicated (lode.staleness.reanchor_annotations)."""
    settings = _gate_settings()
    lance_dir = tmp_path / "vectors"
    first = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", "the quick fox jumps", settings=settings
    )
    second = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", "the quick fox jumps over the log", settings=settings
    )
    _write_vector(lance_dir, first.snapshot_id, [1.0, 0.0, 0.0, 0.0], settings)
    _write_vector(
        lance_dir, second.snapshot_id, [1.0, 0.0, 0.0, 0.0], settings
    )  # immaterial

    with conn:
        conn.execute(
            "INSERT INTO annotations "
            "(target, source_version, kind, payload, source, status, quoted_text) "
            "VALUES (?, ?, 'tag', ?, 'ai', 'fresh', ?)",
            (_EXTERNAL_ID, first.snapshot_id, json.dumps("fox"), "quick fox"),
        )

    outcome = gate_reenrich(
        conn, second.snapshot_id, lance_dir=lance_dir, settings=settings
    )

    assert outcome is not None and "immaterial" in outcome
    (source_version, status) = conn.execute(
        "SELECT source_version, status FROM annotations WHERE target = ?",
        (_EXTERNAL_ID,),
    ).fetchone()
    assert source_version == second.snapshot_id
    assert status == "fresh"


def test_gate_reenrich_unknown_snapshot_id_returns_none(conn, tmp_path: Path) -> None:
    assert gate_reenrich(conn, "not-a-real-id", lance_dir=tmp_path / "vectors") is None


def test_gate_reenrich_tombstone_snapshot_returns_none(conn, tmp_path: Path) -> None:
    result = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", tombstone_body("http_403"), status="tombstone"
    )
    assert (
        gate_reenrich(conn, result.snapshot_id, lance_dir=tmp_path / "vectors") is None
    )


def test_gate_reenrich_note_version_target_returns_none(conn, tmp_path: Path) -> None:
    # A note version_id is never a row in `snapshots` -- the gate must be a
    # no-op for it, since _embed_handler calls it unconditionally after every
    # embed job, note or external alike.
    assert (
        gate_reenrich(conn, "some-note-version-id", lance_dir=tmp_path / "vectors")
        is None
    )


def test_gate_reenrich_respects_custom_threshold(conn, tmp_path: Path) -> None:
    """A small delta is material against a very low custom threshold."""
    settings = _gate_settings(reenrichment_materiality_threshold=0.01)
    lance_dir = tmp_path / "vectors"
    first = ingest_snapshot(conn, _EXTERNAL_ID, "web", "version one", settings=settings)
    second = ingest_snapshot(
        conn, _EXTERNAL_ID, "web", "version two", settings=settings
    )
    _write_vector(lance_dir, first.snapshot_id, [1.0, 0.0, 0.0, 0.0], settings)
    _write_vector(lance_dir, second.snapshot_id, [0.99, 0.01, 0.0, 0.0], settings)

    outcome = gate_reenrich(
        conn, second.snapshot_id, lance_dir=lance_dir, settings=settings
    )

    assert outcome is not None and "material" in outcome
