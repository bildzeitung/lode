"""Tests for lode.tui.services.ask -- the TUI ask screen's pipeline wiring (lode-mkc.2).

Pins the ticket's acceptance criterion directly at the logic layer (no Textual
app needed, mirroring ``tests/test_tui_capture.py``'s split): the rendered
ask result shows cited claims with their as-of/version provenance, withheld
markers surface rather than vanish, and an ungrounded question renders the
honest abstention line. :func:`run_ask` is proven to wire the exact same
seams ``lode ask`` drives (``lode.retrieval._retrieve`` + ``lode.cited_answer.ask``,
mocked here the same way ``tests/test_cli.py``'s ``ask`` tests keep the gate
offline) rather than re-implementing retrieval, and to resolve each surviving
citation's as-of timestamp from the store.
"""

from pathlib import Path

import pytest

from lode.answer import Claim, Support
from lode.citations_read import CitationIdentity
from lode.cited_answer import CitedAnswer
from lode.config import Settings
from lode.egress import WithheldCitation
from lode.retrieval import pinned_note_context
from lode.storage import init_db
from lode.tui.services.ask import (
    ABSTAIN_LINE,
    STAGE_GATE,
    STAGE_RETRIEVING,
    STAGE_SYNTHESIZING,
    AskResult,
    citation_targets,
    render_ask_result,
    run_ask,
)
from lode.versions import save


def test_render_ask_result_abstains_with_the_honest_line() -> None:
    result = AskResult(answer=CitedAnswer(claims=(), withheld_citations=()))
    assert render_ask_result(result, context_chars=80) == ABSTAIN_LINE


def test_render_ask_result_shows_cited_claim_with_as_of_provenance() -> None:
    answer = CitedAnswer(
        claims=(
            Claim(
                text="We chose OAuth for service auth.",
                support=[Support(version_id="v1", quoted_span="use OAuth")],
            ),
        ),
        withheld_citations=(),
    )
    result = AskResult(answer=answer, as_of={"v1": "2026-06-18T00:00:00.000Z"})

    rendered = render_ask_result(result, context_chars=80)

    assert "We chose OAuth for service auth." in rendered
    assert "version v1" in rendered
    assert "as of 2026-06-18T00:00:00.000Z" in rendered
    assert '"use OAuth"' in rendered


def test_render_ask_result_shows_snapshot_citation_provenance() -> None:
    answer = CitedAnswer(
        claims=(
            Claim(
                text="The ticket was open.",
                support=[Support(snapshot_id="s1", quoted_span="status: open")],
            ),
        ),
        withheld_citations=(),
    )
    result = AskResult(answer=answer, as_of={"s1": "2026-06-01T00:00:00.000Z"})

    rendered = render_ask_result(result, context_chars=80)

    assert "snapshot s1" in rendered
    assert "as of 2026-06-01T00:00:00.000Z" in rendered


def test_render_ask_result_marks_a_citation_with_unresolved_provenance() -> None:
    """Practically unreachable (the gate already verified the body exists), but handled."""
    answer = CitedAnswer(
        claims=(
            Claim(
                text="claim", support=[Support(version_id="missing", quoted_span="x")]
            ),
        ),
        withheld_citations=(),
    )
    result = AskResult(answer=answer, as_of={})

    assert "as of unknown" in render_ask_result(result, context_chars=80)


def test_render_ask_result_surfaces_withheld_markers_alongside_abstention() -> None:
    answer = CitedAnswer(
        claims=(),
        withheld_citations=(WithheldCitation(target_id="v9"),),
    )
    result = AskResult(answer=answer)

    rendered = render_ask_result(result, context_chars=80)

    assert ABSTAIN_LINE in rendered
    assert "[withheld] v9" in rendered
    assert "withheld from cloud synthesis" in rendered.lower()


def test_render_ask_result_groups_a_note_cited_by_multiple_claims_once(
    tmp_path: Path,
) -> None:
    """lode-35nu.3's core acceptance line: a note cited by N claims renders
    once, with its claims nested under it -- not once per claim."""
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        saved = save(conn, "n1", "We chose OAuth for service auth. It scales well.")
    finally:
        conn.close()

    answer = CitedAnswer(
        claims=(
            Claim(
                text="We chose OAuth.",
                support=[Support(version_id=saved.version_id, quoted_span="OAuth")],
            ),
            Claim(
                text="It scales well.",
                support=[
                    Support(version_id=saved.version_id, quoted_span="scales well")
                ],
            ),
        ),
        withheld_citations=(),
    )
    identities = {
        saved.version_id: CitationIdentity(
            note_id="n1",
            title="We chose OAuth for service auth.",
            is_head=True,
        )
    }
    bodies = {saved.version_id: "We chose OAuth for service auth. It scales well."}
    result = AskResult(answer=answer, identities=identities, bodies=bodies)

    rendered = render_ask_result(result, context_chars=80)

    # The note's title (the group header) appears exactly once, as its own line.
    header_lines = [
        line
        for line in rendered.splitlines()
        if line == "We chose OAuth for service auth."
    ]
    assert len(header_lines) == 1
    assert "  We chose OAuth." in rendered
    assert "  It scales well." in rendered


def test_render_ask_result_highlights_the_quoted_span_in_context(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        saved = save(conn, "n1", "before text OAuth after text")
    finally:
        conn.close()

    answer = CitedAnswer(
        claims=(
            Claim(
                text="claim",
                support=[Support(version_id=saved.version_id, quoted_span="OAuth")],
            ),
        ),
        withheld_citations=(),
    )
    identities = {
        saved.version_id: CitationIdentity(note_id="n1", title="title", is_head=True)
    }
    bodies = {saved.version_id: "before text OAuth after text"}
    result = AskResult(answer=answer, identities=identities, bodies=bodies)

    rendered = render_ask_result(result, context_chars=80)

    assert "»OAuth«" in rendered
    assert "before text" in rendered
    assert "after text" in rendered


def test_render_ask_result_context_chars_is_configurable(tmp_path: Path) -> None:
    body = "x" * 200 + "OAuth" + "y" * 200
    answer = CitedAnswer(
        claims=(
            Claim(
                text="claim", support=[Support(version_id="v1", quoted_span="OAuth")]
            ),
        ),
        withheld_citations=(),
    )
    identities = {"v1": CitationIdentity(note_id="n1", title="title", is_head=True)}
    result = AskResult(answer=answer, identities=identities, bodies={"v1": body})

    rendered_narrow = render_ask_result(result, context_chars=5)
    rendered_wide = render_ask_result(result, context_chars=50)

    assert "x" * 5 in rendered_narrow
    assert "x" * 6 not in rendered_narrow
    assert "x" * 50 in rendered_wide


def test_render_ask_result_renders_context_for_a_whitespace_reflowed_span() -> None:
    """The faithfulness gate accepts a span matching only after whitespace
    normalization (``span_occurs``), so the renderer must too -- an exact-only
    search would be stricter than the gate and silently drop context for a
    quote reflowed off a multi-line body, which is the common case."""
    body = "lead in\nthe token\nrotates hourly\ntrailing"
    answer = CitedAnswer(
        claims=(
            Claim(
                text="claim",
                support=[
                    Support(version_id="v1", quoted_span="the token rotates hourly")
                ],
            ),
        ),
        withheld_citations=(),
    )
    identities = {"v1": CitationIdentity(note_id="n1", title="title", is_head=True)}
    result = AskResult(answer=answer, identities=identities, bodies={"v1": body})

    rendered = render_ask_result(result, context_chars=80)

    # Highlighted, with real context on both sides -- not the bare-span fallback.
    assert "»the token rotates hourly«" in rendered
    assert "lead in" in rendered
    assert "trailing" in rendered
    assert '"the token rotates hourly"' not in rendered


def test_render_ask_result_uses_body_offset_to_pick_the_right_occurrence() -> None:
    """A ``quoted_span`` occurring twice in its body is otherwise ambiguous
    (``locate_span`` alone always finds the leftmost) -- ``Support.body_offset``
    (lode-hruz), when stamped, disambiguates which occurrence the context comes
    from."""
    body = "alpha OAuth beta " + ("x" * 100) + " gamma OAuth delta"
    second_offset = body.index("OAuth", body.index("OAuth") + 1)
    answer = CitedAnswer(
        claims=(
            Claim(
                text="claim",
                support=[
                    Support(
                        version_id="v1",
                        quoted_span="OAuth",
                        body_offset=second_offset,
                    )
                ],
            ),
        ),
        withheld_citations=(),
    )
    identities = {"v1": CitationIdentity(note_id="n1", title="title", is_head=True)}
    result = AskResult(answer=answer, identities=identities, bodies={"v1": body})

    rendered = render_ask_result(result, context_chars=10)

    assert "gamma" in rendered
    assert "delta" in rendered
    assert "alpha" not in rendered
    assert "beta" not in rendered


def test_render_ask_result_falls_back_to_flat_rendering_when_unresolved() -> None:
    """A citation whose target didn't resolve to an identity has no body to
    pull context from -- it keeps the old flat, ungrouped rendering."""
    answer = CitedAnswer(
        claims=(
            Claim(
                text="claim", support=[Support(version_id="missing", quoted_span="x")]
            ),
        ),
        withheld_citations=(),
    )
    result = AskResult(answer=answer)

    rendered = render_ask_result(result, context_chars=80)

    assert '"x"' in rendered
    assert "»" not in rendered


def test_run_ask_wires_retrieve_and_gate_then_resolves_as_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    saved = save(conn, "n1", "We decided to use OAuth for service auth.")
    conn.close()

    canned_answer = CitedAnswer(
        claims=(
            Claim(
                text="use OAuth",
                support=[Support(version_id=saved.version_id, quoted_span="use OAuth")],
            ),
        ),
        withheld_citations=(),
    )
    retrieve_calls: list[str] = []

    def _stub_retrieve(conn, question, *, lance_dir, settings=None):
        retrieve_calls.append(question)
        return []

    def _stub_ask(conn, question, context, *, think_harder=False, settings=None):
        assert context == []
        return canned_answer

    monkeypatch.setattr("lode.retrieval._retrieve", _stub_retrieve)
    monkeypatch.setattr("lode.cited_answer.ask", _stub_ask)

    result = run_ask(db_path, "what did we decide about auth?", settings=Settings())

    assert retrieve_calls == ["what did we decide about auth?"]
    assert result.answer is canned_answer
    assert result.as_of[saved.version_id] is not None
    assert result.identities[saved.version_id] == CitationIdentity(
        note_id="n1",
        title="We decided to use OAuth for service auth.",
        is_head=True,
    )


def test_run_ask_reports_stages_in_order_via_on_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode-35nu.5: the in-flight spinner needs a progress callback threaded
    through ``run_ask`` -- proves the callback fires once per stage, in
    pipeline order, without coupling this module to Textual (``on_stage`` is
    a plain callable).
    """
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    saved = save(conn, "n1", "We decided to use OAuth for service auth.")
    conn.close()

    canned_answer = CitedAnswer(
        claims=(
            Claim(
                text="use OAuth",
                support=[Support(version_id=saved.version_id, quoted_span="use OAuth")],
            ),
        ),
        withheld_citations=(),
    )

    monkeypatch.setattr(
        "lode.retrieval._retrieve",
        lambda conn, question, *, lance_dir, settings=None: [],
    )
    monkeypatch.setattr(
        "lode.cited_answer.ask",
        lambda conn, question, context, *, think_harder=False, settings=None: (
            canned_answer
        ),
    )

    stages: list[str] = []
    result = run_ask(
        db_path,
        "what did we decide about auth?",
        settings=Settings(),
        on_stage=stages.append,
    )

    assert stages == [STAGE_RETRIEVING, STAGE_SYNTHESIZING, STAGE_GATE]
    assert result.answer is canned_answer


def test_run_ask_with_no_on_stage_is_unaffected() -> None:
    """The default (``on_stage=None``) is every existing caller/test -- must
    stay a plain, callback-free call.
    """
    import inspect

    assert inspect.signature(run_ask).parameters["on_stage"].default is None


def test_run_ask_pinned_note_id_default_is_none_and_unaffected() -> None:
    """lode-35nu.11.3: the default is every existing caller/test's exact prior
    behaviour -- corpus-wide Ask never passes this.
    """
    import inspect

    assert inspect.signature(run_ask).parameters["pinned_note_id"].default is None


def test_run_ask_with_pinned_note_id_prepends_pinned_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode-35nu.11.3: the pinned note's own passages lead the context handed
    to synthesis, ahead of whatever normal corpus retrieval also found --
    "pinned as primary context rather than competing for retrieval rank."
    Normal retrieval still runs underneath (a per-note ask can still cite
    other notes/externals) -- its hit for a *different* note survives.
    """
    from lode.lexical import LexicalCacheBackend
    from lode.repository import CompositeCache, Repository
    from lode.retrieval import ContextItem, TrustTier

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    repo = Repository(conn, CompositeCache([LexicalCacheBackend(conn)]))
    pinned_version = repo.save("note-pinned", "the pinned note's own body").version_id
    conn.close()

    canned_answer = CitedAnswer(claims=(), withheld_citations=())
    captured: dict[str, object] = {}

    def _stub_retrieve(conn, question, *, lance_dir, settings=None):
        return [
            ContextItem(
                tier=TrustTier.OWNED_NOTE,
                passage_id="other-note-passage",
                target_version="other-note-version",
                char_range="0:5",
                passage_text="other note's text",
                parent_block="other note's text",
                score=1.0,
            )
        ]

    def _stub_ask(conn, question, context, *, think_harder=False, settings=None):
        captured["context"] = context
        return canned_answer

    monkeypatch.setattr("lode.retrieval._retrieve", _stub_retrieve)
    monkeypatch.setattr("lode.cited_answer.ask", _stub_ask)

    run_ask(
        db_path,
        "what does this note say?",
        settings=Settings(),
        pinned_note_id="note-pinned",
    )

    context = captured["context"]
    assert context  # not empty
    # The pinned note's own passage(s) lead, followed by the normal
    # retrieval hit for the other note -- neither dropped, pinned first.
    assert context[0].target_version == pinned_version
    assert context[0].tier is TrustTier.OWNED_NOTE
    assert context[-1].target_version == "other-note-version"


def test_run_ask_with_pinned_note_id_dedupes_against_retrieval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If normal retrieval independently found the same passage the pin would
    add, the pinned copy wins and it is not duplicated in the context.
    """
    from lode.lexical import LexicalCacheBackend
    from lode.repository import CompositeCache, Repository

    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    repo = Repository(conn, CompositeCache([LexicalCacheBackend(conn)]))
    repo.save("note-pinned", "the pinned note's own body")
    pinned = pinned_note_context(conn, "note-pinned")
    assert pinned  # sanity: real chunking produced at least one passage
    conn.close()

    captured: dict[str, object] = {}

    def _stub_retrieve(conn, question, *, lance_dir, settings=None):
        # Normal retrieval also happens to surface the same passage.
        return [pinned[0]]

    def _stub_ask(conn, question, context, *, think_harder=False, settings=None):
        captured["context"] = context
        return CitedAnswer(claims=(), withheld_citations=())

    monkeypatch.setattr("lode.retrieval._retrieve", _stub_retrieve)
    monkeypatch.setattr("lode.cited_answer.ask", _stub_ask)

    run_ask(
        db_path,
        "what does this note say?",
        settings=Settings(),
        pinned_note_id="note-pinned",
    )

    context = captured["context"]
    passage_ids = [item.passage_id for item in context]
    assert passage_ids.count(pinned[0].passage_id) == 1


# ---------------------------------------------------------------------------
# citation_targets (lode-35nu.4) -- the ask screen's navigation order.
# ---------------------------------------------------------------------------


def test_citation_targets_lists_distinct_targets_in_first_cited_order() -> None:
    answer = CitedAnswer(
        claims=(
            Claim(
                text="claim one",
                support=[
                    Support(version_id="v2", quoted_span="a"),
                    Support(version_id="v1", quoted_span="b"),
                ],
            ),
            Claim(
                text="claim two",
                # v1 cited again -- must not appear twice.
                support=[Support(version_id="v1", quoted_span="c")],
            ),
        ),
        withheld_citations=(),
    )
    identities = {
        "v1": CitationIdentity(note_id="n1", title="Note One", is_head=True),
        "v2": CitationIdentity(note_id="n2", title="Note Two", is_head=True),
    }
    result = AskResult(answer=answer, identities=identities)

    assert citation_targets(result) == ["v2", "v1"]


def test_citation_targets_excludes_a_target_with_no_resolved_identity() -> None:
    answer = CitedAnswer(
        claims=(
            Claim(
                text="claim",
                support=[Support(version_id="v1", quoted_span="a")],
            ),
        ),
        withheld_citations=(),
    )
    # No identities at all -- store had nothing to resolve (AskResult's own
    # documented "practically unreachable but handled" case).
    result = AskResult(answer=answer, identities={})

    assert citation_targets(result) == []


def test_citation_targets_empty_for_an_abstained_answer() -> None:
    result = AskResult(answer=CitedAnswer(claims=(), withheld_citations=()))

    assert citation_targets(result) == []


def test_citation_targets_walks_groups_contiguously_like_the_rendered_answer() -> None:
    """Navigation order must be the RENDERED order, not a flat first-cited walk.

    ``n1`` is cited by claims 0 and 2 with an ``n2`` claim in between, so
    ``_render_claims`` emits one contiguous ``n1`` block (v1 then v3) followed
    by ``n2``'s. A flat first-cited walk would yield ``v1, v2, v3`` -- the
    status line's "Citation n/m" would then disagree with what the reader is
    looking at.
    """
    answer = CitedAnswer(
        claims=(
            Claim(text="one", support=[Support(version_id="v1", quoted_span="a")]),
            Claim(text="two", support=[Support(version_id="v2", quoted_span="b")]),
            Claim(text="three", support=[Support(version_id="v3", quoted_span="c")]),
        ),
        withheld_citations=(),
    )
    identities = {
        # v1 and v3 are two different versions of the SAME note.
        "v1": CitationIdentity(note_id="n1", title="Note One", is_head=False),
        "v2": CitationIdentity(note_id="n2", title="Note Two", is_head=True),
        "v3": CitationIdentity(note_id="n1", title="Note One", is_head=True),
    }
    result = AskResult(answer=answer, identities=identities)

    assert citation_targets(result) == ["v1", "v3", "v2"]


def test_citation_targets_excludes_an_identity_with_neither_note_nor_external() -> None:
    """The "exactly one of note_id/external_id" invariant violated.

    ``_render_claims`` drops such a citation into its ungrouped flat fallback
    (nothing to open); ``citation_targets`` must agree, or ``Ctrl+J`` would
    push a ``SnapshotViewerScreen`` at a target that is not an external
    snapshot at all.
    """
    answer = CitedAnswer(
        claims=(
            Claim(text="claim", support=[Support(version_id="v1", quoted_span="a")]),
        ),
        withheld_citations=(),
    )
    result = AskResult(
        answer=answer,
        identities={"v1": CitationIdentity(title="Neither", is_head=True)},
    )

    assert citation_targets(result) == []
