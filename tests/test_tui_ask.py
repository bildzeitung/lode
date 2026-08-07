"""Tests for lode.tui.services.ask -- the TUI ask screen's pipeline wiring (lode-mkc.2).

Pins the ticket's acceptance criterion directly at the logic layer (no Textual
app needed, mirroring ``tests/test_tui_capture.py``'s split): the rendered
ask result shows cited claims with their as-of/version provenance, withheld
markers surface rather than vanish, and an ungrounded question renders the
honest abstention line. :func:`run_ask` is proven to wire the exact same
seams ``lode ask`` drives (``lode.cli._retrieve`` + ``lode.cited_answer.ask``,
mocked here the same way ``tests/test_cli.py``'s ``ask`` tests keep the gate
offline) rather than re-implementing retrieval, and to resolve each surviving
citation's as-of timestamp from the store.
"""

from pathlib import Path

import pytest

from lode.answer import Claim, Support
from lode.cited_answer import CitedAnswer
from lode.config import Settings
from lode.egress import WithheldCitation
from lode.storage import init_db
from lode.tui.services.ask import (
    ABSTAIN_LINE,
    STAGE_GATE,
    STAGE_RETRIEVING,
    STAGE_SYNTHESIZING,
    AskResult,
    CitationIdentity,
    _resolve_citations,
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


def test_resolve_citations_reads_version_created_from_store(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "n1", "hello world")
        (created,) = conn.execute(
            "SELECT created FROM versions WHERE version_id = ?", (result.version_id,)
        ).fetchone()

        as_of, _, _ = _resolve_citations(
            conn, [Support(version_id=result.version_id, quoted_span="hello")]
        )
    finally:
        conn.close()

    assert as_of == {result.version_id: created}


def test_resolve_citations_reads_snapshot_fetched_at_from_store(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute(
            "INSERT INTO externals (external_id, source_type) VALUES ('e1', 'jira')"
        )
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, external_id, body, status, fetched_at) "
            "VALUES ('s1', 'e1', 'status: open', 'ok', '2026-06-01T00:00:00.000Z')"
        )
        conn.commit()

        as_of, _, _ = _resolve_citations(
            conn, [Support(snapshot_id="s1", quoted_span="status: open")]
        )
    finally:
        conn.close()

    assert as_of == {"s1": "2026-06-01T00:00:00.000Z"}


def test_resolve_citations_maps_an_unresolvable_target_to_none(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        as_of, identities, _ = _resolve_citations(
            conn, [Support(version_id="nonexistent", quoted_span="x")]
        )
    finally:
        conn.close()

    assert as_of == {"nonexistent": None}
    assert identities == {}


def test_resolve_citations_resolves_head_note_version(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "n1", "First line of the note.\nmore body")

        _, identities, _ = _resolve_citations(
            conn, [Support(version_id=result.version_id, quoted_span="First line")]
        )
    finally:
        conn.close()

    assert identities[result.version_id] == CitationIdentity(
        note_id="n1", title="First line of the note.", is_head=True
    )


def test_resolve_citations_marks_a_superseded_version_not_head(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        v1 = save(conn, "n1", "Original body.")
        save(
            conn, "n1", "Updated body.", parent=v1.version_id
        )  # new head; v1 superseded

        _, identities, _ = _resolve_citations(
            conn, [Support(version_id=v1.version_id, quoted_span="Original")]
        )
    finally:
        conn.close()

    assert identities[v1.version_id] == CitationIdentity(
        note_id="n1", title="Original body.", is_head=False
    )


def test_resolve_citations_resolves_head_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        conn.execute(
            "INSERT INTO externals (external_id, source_type, head_snapshot_id) "
            "VALUES ('e1', 'web', 's1')"
        )
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, external_id, body, status, fetched_at) "
            "VALUES ('s1', 'e1', ?, 'ok', '2026-06-01T00:00:00.000Z')",
            ("Ticket title\nbody",),
        )
        conn.commit()

        _, identities, _ = _resolve_citations(
            conn, [Support(snapshot_id="s1", quoted_span="body")]
        )
    finally:
        conn.close()

    assert identities["s1"] == CitationIdentity(
        external_id="e1", title="Ticket title", is_head=True
    )


def test_resolve_citations_batches_one_query_per_kind(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        v1 = save(conn, "n1", "one")
        v2 = save(conn, "n2", "two")

        executed: list[str] = []
        conn.set_trace_callback(executed.append)

        _, identities, _ = _resolve_citations(
            conn,
            [
                Support(version_id=v1.version_id, quoted_span="one"),
                Support(version_id=v2.version_id, quoted_span="two"),
            ],
        )
        conn.set_trace_callback(None)
    finally:
        conn.close()

    assert len(executed) == 1
    assert len(identities) == 2


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

    monkeypatch.setattr("lode.cli._retrieve", _stub_retrieve)
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
        "lode.cli._retrieve", lambda conn, question, *, lance_dir, settings=None: []
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
