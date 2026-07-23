"""Phase-A exit gate (lode-6w1.1 / lode-x6r.5 / lode-xyb): add → (work) → ask → cited claim / abstain → eval green.

Verification gate for the walking skeleton end-to-end. Three sub-gates together
close Phase A and unblock the deepening tasks:

1. ``lode add`` saves a note and enqueues the ``embed`` derive job; ``lode work``
   drains that job (chunk+embed+FTS via the async worker, lode-x6r.5) so the
   note is vector- and keyword-findable; ``lode ask`` retrieves it, sends the
   context to the Q&A step, and the faithfulness gate verifies the claimed
   ``quoted_span`` is verbatim in the cited version's stored body — the cited
   claim survives and renders. No in-test derive-job stand-in: the real CLI
   ``work`` command performs the embed.

2. An out-of-corpus question against the same corpus ends in honest abstention
   when the Q&A step asserts nothing: the gate finds no surviving claim and the
   abstention line is printed.

3. The deterministic offline eval scorer (``score_golden_set`` over the golden
   fixture, stub embedder + oracle answerer) reports perfect recall@k,
   faithfulness, and abstention — the offline analog of the live ``nox -s eval``
   integration test.

All three run offline and reproducibly. The two non-deterministic seams are
replaced:

- **Embedder**: ``_ConstantEmbedder`` — a deterministic, constant-direction stub
  that produces dim-768 vectors (the LanceDB table width the embed job creates
  under the default ``Settings``).  No fastembed model download.  Injected via
  ``monkeypatch`` into ``lode.embedding.FastEmbedEmbedder`` so it is used by
  both ``lode work`` (the async embed handler) and ``lode ask`` (query embedding).
- **Q&A client**: ``_FakeClient`` — returns known claims for the in-corpus question
  and no claims for the out-of-corpus question.  No Anthropic API call.

The faithfulness gate is REAL and runs inline: every ``quoted_span`` that survives
must be verbatim in the retrieved version body.  The span check in the assertion
below is *the same predicate the gate uses* — so if the assertion passes, the gate
would have passed too (it already did, else the CLI would have abstained).

**Fast/slow split (lode-pql).** Only the embedder is stubbed above; ``lode ask``
still reranks with the real, un-mocked ``FastEmbedCrossEncoder`` (its model-load
cost dominates: ``pytest --durations`` measured gate1/gate2/gate4 — the three
sub-gates whose bodies call ``ask`` — at multiple seconds each, vs. sub-second for
gate3 and gate5, which never call ``ask``). Those three are ``@pytest.mark.slow``:
excluded from the fast inner-loop (``nox -s unit``), still run every time in the
full landing gate (``nox -s tests``). See ``docs/onboarding.md`` for the tiers.
"""

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from lode import cli
from lode.answer import Claim, Support
from lode.cli import app
from lode.config import Settings, load_settings
from lode.eval.golden import golden_set
from lode.eval.harness import score_golden_set
from lode.faithfulness import span_occurs
from lode.llm_provider import AnthropicProvider
from lode.storage import init_db

#: FTS5 keyword that appears in _NOTE_BODY — used in Gate 4 to verify
#: the lexical leg can find the note by keyword without any async work.
_FTS_KEYWORD = "exponential"

runner = CliRunner()

# ---------------------------------------------------------------------------
# In-corpus note: the skeleton is exercised on a known, controlled body.
# The quoted_span must be a verbatim substring of the body — it is what the
# fake Q&A returns and what the faithfulness gate verifies.
# ---------------------------------------------------------------------------
_NOTE_BODY = (
    "We settled on exponential backoff for retry logic in the API client, "
    "doubling the delay on each attempt up to a 30-second ceiling."
)
_QUOTED_SPAN = "exponential backoff for retry logic in the API client"
_IN_CORPUS_QUESTION = "What retry strategy did we settle on for the API client?"
_OUT_OF_CORPUS_QUESTION = "How does quantum computing leverage superposition?"


# ---------------------------------------------------------------------------
# Stubs: a deterministic embedder (no model download) and a fake Q&A client
# ---------------------------------------------------------------------------


class _ConstantEmbedder:
    """A deterministic, constant-direction stub: every text maps to [1, 0, 0, ...].

    Dim matches whatever ``settings.embedding_vector_dim`` says (768 by default
    when the CLI uses ``Settings()``), so the query vector and the indexed passage
    vectors live in the same space — the cosine ANN will find them.  No semantic
    signal, but the dense leg can surface the one indexed note.
    """

    def __init__(self, settings: Settings) -> None:
        self._dim = settings.embedding_vector_dim

    def _vector(self) -> list[float]:
        return [1.0] + [0.0] * (self._dim - 1)

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._vector() for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector()


class _HashEmbedder:
    """Offline eval stub: same text → same fixed-dim vector (no semantic meaning).

    Used for the golden-set eval (Gate 3) where the scoring harness exercises
    the metric plumbing, not retrieval quality.  Recall is carried by the
    model-free FTS5 leg.  Both methods hash into the same space so query and
    passage vectors are directly comparable under the cosine ANN.
    """

    DIM = 8

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            vectors.append([digest[i] / 255.0 for i in range(self.DIM)])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [digest[i] / 255.0 for i in range(self.DIM)]


class _FakeMessages:
    """Records every parse() call and returns ``claimed`` claims."""

    def __init__(self, claimed: list[Claim]) -> None:
        self._claimed = claimed
        self.calls: list[dict] = []

    def parse(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(parsed_output=SimpleNamespace(claims=self._claimed))


class _FakeClient:
    """Mock Anthropic client — returns ``claimed`` for every call, no network."""

    def __init__(self, claimed: list[Claim]) -> None:
        self.messages = _FakeMessages(claimed)


def _fake_enrich_version(conn, version_id, settings, *, client=None) -> None:
    """No-op stand-in for ``lode.enrich.enrich_version`` (lode-7mq).

    ``lode add`` calls ``enrich_version`` immediately on the capture path
    (lode-npx.2); left unstubbed it constructs a real Anthropic client and
    makes a live, billed API call whenever ``ANTHROPIC_API_KEY`` happens to be
    ambient — every other add-invoking test in ``test_cli.py`` already stubs
    this the same way. This gate only exercises the embed/retrieval/Q&A path,
    not enrichment, so a no-op is the correct fake.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_head_version_id(db_path: Path, note_id: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT head_version_id FROM notes WHERE note_id = ?", (note_id,)
        ).fetchone()
        assert row is not None, f"note {note_id!r} not found in DB"
        return row[0]
    finally:
        conn.close()


def _oracle_answerer(question, context):
    """Oracle for the eval harness: returns known-good citations for the golden set."""
    from lode.cited_answer import CitedAnswer

    by_question = {item.question: item for item in golden_set()}
    item = by_question[question]
    claims = tuple(
        Claim(
            text=c.quoted_span,
            support=[Support(version_id=c.version_id, quoted_span=c.quoted_span)],
        )
        for c in item.citations
    )
    return CitedAnswer(claims=claims, withheld_citations=())


# ---------------------------------------------------------------------------
# Gate 1: lode add → lode work → lode ask → verbatim-grounded cited claim
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_gate1_add_ask_yields_cited_claim_with_verbatim_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode add + lode work (async embed) + lode ask → cited claim; span is verbatim.

    The acceptance criterion (lode-6w1.1 / lode-x6r.5): a scripted ``lode add``
    of a known note, followed by ``lode work`` to drain the async embed job,
    followed by ``lode ask``, returns a cited claim whose ``quoted_span`` occurs
    verbatim in the cited version's stored body. No in-test derive-job stand-in
    — the real ``work`` command performs the embed via the async handler.

    Step by step:
    1. Stub ``lode.embedding.FastEmbedEmbedder`` with ``_ConstantEmbedder`` —
       deterministic, no model download, same dim-768 space for both document and
       query vectors.  The stub is used by ``lode work`` (the async embed handler)
       and by ``lode ask`` (query-side embedding).
    2. ``lode add`` saves the note, enqueues the ``embed`` derive job, and calls
       Haiku immediately for enrichment (lode-npx.2) — no embedding happens here.
    3. ``lode work`` drains the ``embed`` job: chunk+embed+FTS runs via the worker
       (lode-x6r.5), using the stubbed embedder — no real model download.
    4. ``lode ask`` retrieves the indexed note (both legs), sends the context to the
       Q&A step (fake client returning a known claim), the faithfulness gate verifies
       the span, and the surviving cited claim is printed.
    5. We assert the span is present in the output AND verbatim in the body using
       the same ``span_occurs`` predicate the gate uses.
    """
    db_path = tmp_path / "lode.db"

    # Stub the embedder before the worker runs — the async embed handler
    # (lode.worker._embed_handler) calls lode.embedding.embed which defaults to
    # FastEmbedEmbedder; patching it here makes the worker use the stub instead
    # of downloading the real model.  The same stub is used by lode ask's
    # query-embedding step.
    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _ConstantEmbedder)

    # Stub enrich_version — this gate exercises embed/retrieval/Q&A, not
    # enrichment; unstubbed, `add` would make a real Haiku call whenever
    # ANTHROPIC_API_KEY is ambient (lode-7mq).
    import lode.enrich as enrich_mod

    monkeypatch.setattr(enrich_mod, "enrich_version", _fake_enrich_version)

    # Step 1: add the note; embed job is enqueued, nothing embedded yet.
    add_result = runner.invoke(app, ["add", _NOTE_BODY, "--db", str(db_path)])
    assert add_result.exit_code == 0, add_result.output
    note_id = add_result.stdout.strip()
    assert note_id, "lode add should print the note_id"

    # Step 2: drain the embed job via lode work (async, lode-x6r.5).
    work_result = runner.invoke(app, ["work", "--db", str(db_path)])
    assert work_result.exit_code == 0, work_result.output

    # Step 3: get the version_id for the fake client's citation support.
    version_id = _get_head_version_id(db_path, note_id)

    # Step 4: ask via the CLI with the fake Q&A client.
    # The fake client returns a claim whose quoted_span is a verbatim substring of
    # the body — so the faithfulness gate will verify it and let it through.
    fake_client = _FakeClient(
        [
            Claim(
                text="Use exponential backoff.",
                support=[Support(version_id=version_id, quoted_span=_QUOTED_SPAN)],
            )
        ]
    )
    monkeypatch.setattr(
        "lode.qa.build_provider", lambda settings: AnthropicProvider(fake_client)
    )

    ask_result = runner.invoke(app, ["ask", _IN_CORPUS_QUESTION, "--db", str(db_path)])
    assert ask_result.exit_code == 0, ask_result.output

    # Step 5: the claim survived the faithfulness gate and was rendered.
    output = ask_result.stdout
    assert cli._ABSTAIN_LINE not in output, "expected a cited claim, not abstention"
    assert _QUOTED_SPAN in output, f"quoted_span not in ask output: {output!r}"
    assert "version_id" in output, "expected a version_id citation in ask output"

    # The quoted_span is verbatim in the note body (the same check the gate ran).
    assert span_occurs(_QUOTED_SPAN, _NOTE_BODY), (
        f"span {_QUOTED_SPAN!r} is not verbatim in body {_NOTE_BODY!r} — "
        "gate would have abstained"
    )


# ---------------------------------------------------------------------------
# Gate 2: out-of-corpus question → abstain
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_gate2_out_of_corpus_question_abstains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An out-of-corpus question against the corpus returns honest abstention.

    The acceptance criterion (lode-6w1.1 / lode-x6r.5): an out-of-corpus
    question abstains.  The embedder stub is injected before ``lode work`` so
    the async embed handler uses it; the fake Q&A asserts nothing (empty claims)
    — which is the correct behaviour when the sources don't support an answer.
    The gate finds no surviving claim and the abstention line is printed.
    """
    db_path = tmp_path / "lode.db"

    # Stub the embedder before lode work runs.
    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _ConstantEmbedder)

    # Stub enrich_version — see gate1's comment (lode-7mq): unstubbed, `add`
    # would make a real Haiku call whenever ANTHROPIC_API_KEY is ambient.
    import lode.enrich as enrich_mod

    monkeypatch.setattr(enrich_mod, "enrich_version", _fake_enrich_version)

    # Add the note; embed job is enqueued but not yet run.
    add_result = runner.invoke(app, ["add", _NOTE_BODY, "--db", str(db_path)])
    assert add_result.exit_code == 0

    # Drain the embed job via the async worker (lode-x6r.5).
    work_result = runner.invoke(app, ["work", "--db", str(db_path)])
    assert work_result.exit_code == 0, work_result.output

    # The fake client returns no claims for the out-of-corpus question — the Q&A
    # step can't assert anything grounded, so the gate abstains.
    monkeypatch.setattr(
        "lode.qa.build_provider",
        lambda settings: AnthropicProvider(_FakeClient([])),
    )

    ask_result = runner.invoke(
        app, ["ask", _OUT_OF_CORPUS_QUESTION, "--db", str(db_path)]
    )
    assert ask_result.exit_code == 0, ask_result.output
    assert cli._ABSTAIN_LINE in ask_result.stdout, (
        f"expected abstention for out-of-corpus question; got: {ask_result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Gate 3: eval scorer — deterministic offline scorer reports green metrics
# ---------------------------------------------------------------------------


def test_gate3_eval_scorer_reports_green_on_golden_fixture(tmp_path: Path) -> None:
    """The eval scorer reports recall@k=1, faithfulness=1, abstention=1.

    The acceptance criterion (lode-6w1.1): the ``nox -s eval`` integration test
    runs green — the scorer reports recall@k / faithfulness / abstention on the
    fixture without error.

    The scorer is driven here directly with stub seams (not via the live
    ``nox -s eval`` integration test, which needs Anthropic credentials for the
    real Q&A leg; that credential-gated session covers the live path). The three metrics are graded against
    the oracle answerer that returns the known-good citations for every golden item
    — so a correct scorer reports perfect scores.
    """
    settings = load_settings(embedding_vector_dim=_HashEmbedder.DIM)
    embedder = _HashEmbedder()

    conn = init_db(":memory:")
    try:
        score = score_golden_set(
            conn,
            lance_dir=tmp_path / "vectors",
            embedder=embedder,
            answerer=_oracle_answerer,
            settings=settings,
        )
    finally:
        conn.close()

    # Every known-good retrieval target is surfaced by the lexical leg.
    assert score.recall_at_k == pytest.approx(1.0), (
        f"recall@{score.k} = {score.recall_at_k:.3f}; expected 1.0"
    )
    # Every golden answer cites the correct version with a verbatim span.
    assert score.faithfulness_accuracy == pytest.approx(1.0), (
        f"faithfulness = {score.faithfulness_accuracy:.3f}; expected 1.0"
    )
    # In-corpus questions answer; out-of-corpus ones abstain — both correct.
    assert score.abstention_accuracy == pytest.approx(1.0), (
        f"abstention = {score.abstention_accuracy:.3f}; expected 1.0"
    )
    assert score.k == settings.retrieval_top_k


# ---------------------------------------------------------------------------
# Gate 4: lode add → lode ask (NO work) → keyword finds the note (lode-xyb)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_gate4_fts_findable_before_lode_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode add + lode ask (no lode work) → keyword finds note via FTS.

    The acceptance criterion (lode-xyb / x6r.4's real AC): the lexical leg
    (FTS5) is synchronous and model-free, so a just-saved note is
    keyword-findable BEFORE any async embedding runs.  This is the regression
    that x6r.5 introduced: it removed ``_embed_inline`` which was the only FTS
    write, and cli.py add was building ``Repository(conn)`` with NullCache, so
    ``cache.index()`` was a no-op and the note was never indexed synchronously.

    After lode-xyb: cli.py add injects
    ``CompositeCache([LexicalCacheBackend(conn)])`` so ``Repository.save``
    calls ``LexicalCacheBackend.index()`` right after the version commits —
    writing ``passages`` + ``passages_fts`` without any model — and ``lode ask``
    can retrieve the note via FTS immediately.

    Step by step:
    1. Stub ``lode.embedding.FastEmbedEmbedder`` with ``_ConstantEmbedder``
       (the dense leg embeds the query, but has no indexed vectors to hit).
    2. ``lode add`` saves the note and synchronously writes passages + FTS5 rows
       via the injected cache.  The embed job is enqueued but NOT drained.
    3. ``lode ask`` with a keyword from the note's body:
       - Lexical leg (FTS5): hits the note ✓ (synchronous, written on save).
       - Dense leg (LanceDB): empty (embed never ran, no vectors).
       - RRF fuses the one lexical hit alone — still surfaces it.
       - ``expand_parents`` reads the ``passages`` row (written synchronously) → context OK.
       - Fake client returns a claim; faithfulness gate verifies the span.
    4. Assert the cited claim is present (not abstained) and the span is in output.
    """
    db_path = tmp_path / "lode.db"

    # Stub the embedder — only the query-embedding side is called here; there
    # are no indexed vectors, so the dense leg returns empty.
    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _ConstantEmbedder)

    # Stub enrich_version — see gate1's comment (lode-7mq): unstubbed, `add`
    # would make a real Haiku call whenever ANTHROPIC_API_KEY is ambient.
    import lode.enrich as enrich_mod

    monkeypatch.setattr(enrich_mod, "enrich_version", _fake_enrich_version)

    # Step 1: add the note.  No lode work runs.
    add_result = runner.invoke(app, ["add", _NOTE_BODY, "--db", str(db_path)])
    assert add_result.exit_code == 0, add_result.output
    note_id = add_result.stdout.strip()
    assert note_id, "lode add should print the note_id"

    # Step 2: get the version_id for the fake client's citation support.
    version_id = _get_head_version_id(db_path, note_id)

    # Step 3: ask via the CLI with the fake Q&A client.  No lode work has run.
    fake_client = _FakeClient(
        [
            Claim(
                text="Use exponential backoff.",
                support=[Support(version_id=version_id, quoted_span=_QUOTED_SPAN)],
            )
        ]
    )
    monkeypatch.setattr(
        "lode.qa.build_provider", lambda settings: AnthropicProvider(fake_client)
    )

    # Ask with a keyword from the note body — this exercises the lexical leg.
    ask_result = runner.invoke(app, ["ask", _FTS_KEYWORD, "--db", str(db_path)])
    assert ask_result.exit_code == 0, ask_result.output

    # Step 4: the claim survived — the note was found via FTS before any work ran.
    output = ask_result.stdout
    assert cli._ABSTAIN_LINE not in output, (
        f"expected cited claim via FTS, but got abstention — "
        f"FTS5 was not indexed synchronously on save (lode-xyb regression).\n"
        f"output: {output!r}"
    )
    assert _QUOTED_SPAN in output, f"quoted_span not in ask output: {output!r}"


# ---------------------------------------------------------------------------
# Gate 5: lode work does NOT double-index FTS (lode-xyb)
# ---------------------------------------------------------------------------


def test_gate5_worker_does_not_double_index_fts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode work does not write FTS5 rows; the synchronous save already did.

    The acceptance criterion (lode-xyb): the worker's ``_embed_handler`` is
    vector-only after lode-xyb.  Running ``lode work`` after ``lode add`` must
    not change the ``passages_fts`` row count — it should remain exactly what
    the synchronous save wrote.

    Step by step:
    1. ``lode add`` writes passages + passages_fts synchronously.
    2. Count passages_fts rows.
    3. ``lode work`` drains the embed job (vector leg only).
    4. Count passages_fts rows again.
    5. Assert the count is unchanged — no double-index.
    """
    db_path = tmp_path / "lode.db"

    # Stub the embedder for the worker's vector leg.
    monkeypatch.setattr("lode.embedding.FastEmbedEmbedder", _ConstantEmbedder)

    # Stub enrich_version — see gate1's comment (lode-7mq): unstubbed, `add`
    # would make a real Haiku call whenever ANTHROPIC_API_KEY is ambient.
    import lode.enrich as enrich_mod

    monkeypatch.setattr(enrich_mod, "enrich_version", _fake_enrich_version)

    # Add a note — synchronous cache writes passages + passages_fts.
    add_result = runner.invoke(app, ["add", _NOTE_BODY, "--db", str(db_path)])
    assert add_result.exit_code == 0, add_result.output

    # Count FTS rows right after add (before any async work).
    conn = sqlite3.connect(db_path)
    try:
        fts_count_before = conn.execute("SELECT COUNT(*) FROM passages_fts").fetchone()[
            0
        ]
        assert fts_count_before > 0, (
            "passages_fts should have rows after lode add (synchronous FTS write)"
        )
    finally:
        conn.close()

    # Drain the embed job (vector leg only under lode-xyb).
    work_result = runner.invoke(app, ["work", "--db", str(db_path)])
    assert work_result.exit_code == 0, work_result.output

    # Count FTS rows after work — must be unchanged.
    conn = sqlite3.connect(db_path)
    try:
        fts_count_after = conn.execute("SELECT COUNT(*) FROM passages_fts").fetchone()[
            0
        ]
    finally:
        conn.close()

    assert fts_count_after == fts_count_before, (
        f"FTS row count changed after lode work: {fts_count_before} → "
        f"{fts_count_after}.  The worker double-indexed FTS (lode-xyb regression)."
    )
