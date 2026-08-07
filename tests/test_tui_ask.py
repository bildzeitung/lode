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
    AskResult,
    CitationIdentity,
    _resolve_as_of,
    _resolve_identities,
    render_ask_result,
    run_ask,
)
from lode.versions import save


def test_render_ask_result_abstains_with_the_honest_line() -> None:
    result = AskResult(answer=CitedAnswer(claims=(), withheld_citations=()))
    assert render_ask_result(result) == ABSTAIN_LINE


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

    rendered = render_ask_result(result)

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

    rendered = render_ask_result(result)

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

    assert "as of unknown" in render_ask_result(result)


def test_render_ask_result_surfaces_withheld_markers_alongside_abstention() -> None:
    answer = CitedAnswer(
        claims=(),
        withheld_citations=(WithheldCitation(target_id="v9"),),
    )
    result = AskResult(answer=answer)

    rendered = render_ask_result(result)

    assert ABSTAIN_LINE in rendered
    assert "[withheld] v9" in rendered
    assert "withheld from cloud synthesis" in rendered.lower()


def test_resolve_as_of_reads_version_created_from_store(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "n1", "hello world")
        (created,) = conn.execute(
            "SELECT created FROM versions WHERE version_id = ?", (result.version_id,)
        ).fetchone()

        as_of = _resolve_as_of(
            conn, Support(version_id=result.version_id, quoted_span="hello")
        )
    finally:
        conn.close()

    assert as_of == created


def test_resolve_as_of_reads_snapshot_fetched_at_from_store(tmp_path: Path) -> None:
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

        as_of = _resolve_as_of(
            conn, Support(snapshot_id="s1", quoted_span="status: open")
        )
    finally:
        conn.close()

    assert as_of == "2026-06-01T00:00:00.000Z"


def test_resolve_as_of_returns_none_for_an_unresolvable_target(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        as_of = _resolve_as_of(conn, Support(version_id="nonexistent", quoted_span="x"))
    finally:
        conn.close()

    assert as_of is None


def test_resolve_identities_resolves_head_note_version(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        result = save(conn, "n1", "First line of the note.\nmore body")

        identities = _resolve_identities(
            conn, [Support(version_id=result.version_id, quoted_span="First line")]
        )
    finally:
        conn.close()

    assert identities[result.version_id] == CitationIdentity(
        note_id="n1", title="First line of the note.", is_head=True
    )


def test_resolve_identities_marks_a_superseded_version_not_head(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        v1 = save(conn, "n1", "Original body.")
        save(
            conn, "n1", "Updated body.", parent=v1.version_id
        )  # new head; v1 superseded

        identities = _resolve_identities(
            conn, [Support(version_id=v1.version_id, quoted_span="Original")]
        )
    finally:
        conn.close()

    assert identities[v1.version_id].note_id == "n1"
    assert identities[v1.version_id].title == "Original body."
    assert identities[v1.version_id].is_head is False


def test_resolve_identities_resolves_head_snapshot(tmp_path: Path) -> None:
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

        identities = _resolve_identities(
            conn, [Support(snapshot_id="s1", quoted_span="body")]
        )
    finally:
        conn.close()

    assert identities["s1"] == CitationIdentity(
        external_id="e1", title="Ticket title", is_head=True
    )


def test_resolve_identities_batches_one_query_per_kind(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        v1 = save(conn, "n1", "one")
        v2 = save(conn, "n2", "two")

        executed: list[str] = []
        conn.set_trace_callback(executed.append)

        identities = _resolve_identities(
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


def test_resolve_identities_skips_an_unresolvable_target(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    conn = init_db(db_path)
    try:
        identities = _resolve_identities(
            conn, [Support(version_id="nonexistent", quoted_span="x")]
        )
    finally:
        conn.close()

    assert identities == {}


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
