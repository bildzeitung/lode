"""Tests for scripts/docs_index_query.py -- the docs/ lookup index query CLI
(lode-t6o1.3).

Covers the acceptance criteria: ranked pointers with first-line + snippet
(never a whole unit, never synthesized prose), the --class filter, and the
FTS5 input-escaping regression set (a bd issue id, a slash-prefixed
question, and a hyphenated term must each return clean results -- never a
sqlite3 error).
"""

import functools
import sqlite3
from pathlib import Path

import pytest
from conftest import load_module_from_path
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ isn't an installed package -- same load-by-path pattern as
# tests/test_docs_index_build.py. Registered under a name private to this
# test module so it can never collide with docs_index_query.py's own
# private load of docs_index_build.py, or with that other test module's load.
_query_module = load_module_from_path(
    "docs_index_query", REPO_ROOT / "scripts" / "docs_index_query.py"
)
_escape_query = _query_module._escape_query
query = _query_module.query
app = _query_module.app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _private_index_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the index cache into this test's tmp_path.

    query() builds through docs_index_build.cache_db_path(), which resolves
    $XDG_CACHE_HOME at call time. Without this, every test in this file would
    unlink and rebuild the developer's real ~/.cache/lode/docs-index.sqlite3
    -- and under the suite's default `pytest -n 8` several workers would do
    that to the SAME file concurrently. Added at technical review.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))


def test_escape_query_wraps_every_token_as_a_quoted_phrase() -> None:
    assert _escape_query("lode-nt98") == '"lode-nt98"'
    assert _escape_query("push-vs-pull") == '"push-vs-pull"'
    assert (
        _escape_query("what did we decide about /land?")
        == '"what" "did" "we" "decide" "about" "/land?"'
    )


def test_escape_query_escapes_embedded_double_quotes() -> None:
    # FTS5's own quoting rule: an embedded `"` inside a phrase doubles up.
    expected = '"say" "' + '""' + "hi" + '""' + '"'
    assert _escape_query('say "hi"') == expected


def test_escape_query_empty_input_yields_empty_string() -> None:
    assert _escape_query("") == ""
    assert _escape_query("   ") == ""


def test_escape_query_or_joiner_combines_the_same_quoted_terms() -> None:
    """The fallback mode differs from the primary mode in the joiner only --
    the quoting rule has exactly one implementation (lode-qcp0)."""
    assert _escape_query("lode-3v1p guard", " OR ") == '"lode-3v1p" OR "guard"'
    assert _escape_query("lode-3v1p", " OR ") == '"lode-3v1p"'
    assert _escape_query("", " OR ") == ""


@pytest.mark.parametrize("joiner", [" ", " OR "])
@pytest.mark.parametrize(
    "raw",
    [
        '"',
        'a"b',
        "NEAR",
        "NEAR(a b)",
        "a OR b",
        "*",
        "a*",
        "^caret",
        "-",
        "---",
        "(",
        "()",
        "%",
        "café ünïcode",
        ";DROP TABLE units;",
        "x" * 2000,
        " ".join(f"w{i}" for i in range(500)),
        "nul\x00byte",
    ],
)
def test_escaped_query_never_errors_against_fts5(raw: str, joiner: str) -> None:
    """No user string may reach MATCH as an operator, and none may raise --
    probed beyond the three cited regressions at technical review. FTS5
    operators, unbalanced quotes, unicode, NUL, and very long input all have
    to parse as literal phrases. Both combining modes are probed: the default
    implicit AND and the ``" OR "`` fallback mode query() retries with, since
    an escaping hole in either one reaches MATCH the same way.

    Run against a throwaway in-memory table using the build module's OWN
    schema, not a full corpus rebuild: the property under test is the FTS5
    query grammar's reaction to the escaped string, and 19 real rebuilds
    would cost ~2s for no extra coverage.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(_query_module._build._SCHEMA)
        conn.execute(
            "INSERT INTO units VALUES ('a.md', 1, 9, 'hi', 'hello NEAR OR', 'x')"
        )
        match = _escape_query(raw, joiner)
        if not match:
            return
        conn.execute("SELECT path FROM units WHERE units MATCH ?", [match]).fetchall()
    finally:
        conn.close()


def test_escape_query_drops_nul_bytes() -> None:
    # sqlite3 binds str as a C string: a NUL anywhere in the MATCH argument
    # truncates it mid-token and FTS5 raises "unterminated string".
    assert _escape_query("nul\x00byte") == '"nulbyte"'
    assert _escape_query("\x00") == ""


def test_query_regression_bd_issue_id_returns_the_cited_unit() -> None:
    """The epic's headline failure case: 'lode-nt98' must not error, and must
    surface agents-workflow.md's Recycled-worktree guard unit among the top
    hits (acceptance criterion)."""
    results = query("lode-nt98", limit=5)
    assert any("agents-workflow.md" in path for path, *_ in results)


def test_query_regression_slash_prefixed_question_is_clean() -> None:
    """Must return results or a clean empty result -- never a sqlite3 error."""
    results = query("what did we decide about /land?", limit=5)
    assert isinstance(results, list)


def test_query_regression_hyphenated_term_is_clean() -> None:
    results = query("push-vs-pull", limit=5)
    assert isinstance(results, list)


def test_query_never_returns_a_whole_unit_body() -> None:
    """The results tuple carries a bounded snippet, never the raw unit body
    -- structurally enforced by the shape query() returns, not by content
    inspection (a short unit's whole body legitimately fits in a snippet)."""
    results = query("lode-nt98", limit=5)
    assert results
    for path, line_lo, line_hi, first_line, snippet, used_fallback in results:
        assert isinstance(path, str)
        assert line_lo <= line_hi
        assert first_line
        assert len(snippet) <= _query_module._SNIPPET_CHARS + len("...")
        assert used_fallback is False  # a real bd id hits on the AND pass


def test_query_falls_back_to_or_when_and_query_has_zero_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for lode-qcp0: a multi-term query whose terms are all
    present in the corpus, but never together in one chunk, must still
    return pointers -- via an OR retry -- instead of the AND-only zero-hit
    result the implicit-AND MATCH semantics would otherwise produce."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.md").write_text("# Alpha section\n\nzeeqx term appears here.\n")
    (docs_dir / "b.md").write_text("# Beta section\n\nwyvox term appears here.\n")

    monkeypatch.setattr(
        _query_module._build,
        "build_index",
        functools.partial(_query_module._build.build_index, docs_dir=docs_dir),
    )

    # The flag is set only on the path taken after a zero-hit AND pass, so
    # asserting it also asserts that "zeeqx wyvox" matched no single unit.
    results = query("zeeqx wyvox", limit=5)
    assert results
    assert all(used_fallback for *_rest, used_fallback in results)


def test_cli_logs_fallback_fired_true_only_when_the_or_fallback_actually_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lode-i5ww: the CLI's one append_invocation call site must pass the
    REAL per-invocation fallback flag, not a hardcoded False -- so a query
    that trips the AND->OR fallback logs fallback_fired=true, and one that
    doesn't logs false. The autouse _private_index_cache fixture already
    points XDG_CACHE_HOME at tmp_path, which is also where the invocation
    log lands (docs_index_log.log_path() follows the same XDG rule)."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.md").write_text("# Alpha section\n\nzeeqx term appears here.\n")
    (docs_dir / "b.md").write_text("# Beta section\n\nwyvox term appears here.\n")

    monkeypatch.setattr(
        _query_module._build,
        "build_index",
        functools.partial(_query_module._build.build_index, docs_dir=docs_dir),
    )

    # Zero-hit AND pass, so the query only succeeds via the OR retry.
    result = runner.invoke(app, ["zeeqx wyvox"])
    assert result.exit_code == 0, result.output

    # Both terms live in the same unit, so the AND pass hits directly and
    # the fallback is never reached. Kept multi-term deliberately: a
    # single-term query can't fall back at all (the OR rewrite is identical
    # to the AND one), which would assert False for the wrong reason.
    result = runner.invoke(app, ["zeeqx term"])
    assert result.exit_code == 0, result.output

    # The CLI's own accessor, not a second load-by-path: _load_sibling caches
    # on sys.modules, so this is the exact module object the CLI logged through.
    # Read-only here -- calling append_invocation would bypass the thing tested.
    log_module = _query_module._load_log()
    entries = log_module.read_log(log_module.log_path())
    assert [e["fallback_fired"] for e in entries] == [True, False]


def test_query_class_filter_restricts_to_the_requested_class() -> None:
    for doc_class in ("decision-record", "reference/process"):
        results = query("the", doc_class=doc_class, limit=20)
        # Re-derive expected doc_class per path via the chunker's own
        # classify(), the single source of truth for the tag -- not a
        # hardcoded filename list here.
        classify = _query_module._build._chunker.classify
        for path, *_rest in results:
            assert classify(path) == doc_class


def test_cli_prints_pointer_first_line_and_snippet_never_full_unit() -> None:
    result = runner.invoke(app, ["lode-nt98"])
    assert result.exit_code == 0
    assert "agents-workflow.md:" in result.stdout
    # A whole unit is well over the snippet cap; the printed output for any
    # single result line must stay short. A NOTE line is exempt: it carries
    # line numbers only, and its length follows the entry's marker count.
    for line in result.stdout.splitlines():
        if line.startswith("    NOTE: "):
            continue
        assert len(line) <= _query_module._SNIPPET_CHARS + 80


def test_cli_no_results_prints_a_clean_message_not_an_error() -> None:
    result = runner.invoke(app, ["zzz_definitely_not_a_real_term_anywhere_in_docs_zzz"])
    assert result.exit_code == 0
    assert "No results" in result.stdout


def test_cli_rejects_an_invalid_class_value() -> None:
    result = runner.invoke(app, ["lode-nt98", "--class", "bogus"])
    assert result.exit_code == 1


def test_cli_slash_prefixed_query_does_not_crash() -> None:
    result = runner.invoke(app, ["what did we decide about /land?"])
    assert result.exit_code == 0


def test_cli_hyphenated_query_does_not_crash() -> None:
    result = runner.invoke(app, ["push-vs-pull"])
    assert result.exit_code == 0


def test_cli_still_prints_results_when_the_invocation_log_cannot_be_written(
    tmp_path: Path,
) -> None:
    """The usage log (lode-dozi) is instrumentation, not a dependency.

    Occupying the log path with a DIRECTORY makes append_invocation's open()
    raise, while leaving the index cache dir itself writable so the query
    still succeeds. Before the guard, this crashed with a traceback AFTER the
    results had already been computed -- on the CLI CLAUDE.md sends every
    agent through first.
    """
    (tmp_path / "lode" / "docs-index-log.jsonl").mkdir(parents=True)
    result = runner.invoke(app, ["lode-nt98"])
    assert result.exit_code == 0, result.output
    assert "docs/" in result.output


def _write_split_entry_doc(
    directory: Path, *, title: bool, h3_section: bool = False
) -> tuple[Path, str, int]:
    """Write a decisions.md-shaped fixture; return its path, text and marker line.

    One entry over MAX_UNIT_BYTES whose only Update marker is its last line,
    then a small entry carrying a marker of its own. ``title`` adds the H1 line
    the real ``docs/decisions.md`` opens with, which changes the rule the
    chunker reaches the entries by. ``h3_section`` puts a ``##`` section with a
    ``###`` sub-heading ahead of the entries, so they are reached as entries
    appended after a section whose unit holds a deeper heading (``lode-kys3``).
    """
    pad = "\n\n".join(f"  filler paragraph {i} " + "x" * 2000 for i in range(10))
    text = (
        ("# Title\n\nPreamble.\n\n" if title else "")
        + ("## Section\n\nprose\n\n### Sub\n\nmore\n" if h3_section else "")
        + "- **Big entry.** stale claim zorblax\n\n"
        + pad
        + "\n\n  **Update (lode-test, 2026-01-01)** -- corrected.\n"
        "- **Small entry.** quuxly\n"
        "  **Update (lode-other)** -- not the big entry's.\n"
    )
    doc = directory / "decisions.md"
    doc.write_text(text, encoding="utf-8")
    marker_line = next(
        i + 1 for i, line in enumerate(text.splitlines()) if "lode-test" in line
    )
    return doc, text, marker_line


@pytest.mark.parametrize(
    ("title", "h3_section"),
    [(True, False), (False, False), (True, True)],
    ids=["h1-title", "no-heading", "after-a-section-with-an-h3"],
)
def test_split_entry_markers_points_every_piece_of_a_split_entry_at_its_marker(
    tmp_path: Path, title: bool, h3_section: bool
) -> None:
    """An Update marker at the end of an entry over MAX_UNIT_BYTES lands in a
    different unit from the stale claim (lode-funp); every chunk of that entry
    must still point at it, and no unit outside it may."""
    doc, text, marker_line = _write_split_entry_doc(
        tmp_path, title=title, h3_section=h3_section
    )
    units = _query_module._build._chunker.chunk_file(doc, text)
    big_lo = next(u.line_lo for u in units if u.first_line.startswith("- **Big"))
    small_lo = next(u.line_lo for u in units if u.first_line.startswith("- **Small"))
    pieces = [u for u in units if big_lo <= u.line_lo < small_lo]
    assert len(pieces) > 1
    assert any(u.line_hi < marker_line for u in pieces)
    for u in units:
        assert _query_module.split_entry_markers(str(doc), u.line_lo, u.line_hi) == (
            [marker_line] if u in pieces else []
        )


def test_split_entry_markers_agrees_with_the_chunker_on_the_real_decisions_md() -> None:
    """The lookup must fire on the file it exists for, whose shape a fixture
    can get wrong: the real file opens with an H1 title, so its entries come
    from the heading unit's hard-split and not from the no-headings rule.

    Entry boundaries are taken from the chunker's own units rather than from a
    second line scan: a unit that does not open on a structural line continues
    the entry of the unit before it.
    """
    path = REPO_ROOT / "docs" / "decisions.md"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    flags = _query_module.fence_flags(lines)
    chunker = _query_module._build._chunker

    entries: list[list] = []
    for unit in chunker.chunk_file(path, text):
        if not entries or chunker.is_structural_line(unit.first_line):
            entries.append([unit])
        else:
            entries[-1].append(unit)

    noted = 0
    for entry in entries:
        expected = []
        if len(entry) > 1:
            expected = [
                n
                for n in range(entry[0].line_lo, entry[-1].line_hi + 1)
                if not flags[n - 1] and _query_module._UPDATE_MARKER in lines[n - 1]
            ]
        noted += bool(expected)
        for unit in entry:
            assert (
                _query_module.split_entry_markers(str(path), unit.line_lo, unit.line_hi)
                == expected
            )
    assert noted, "no split entry of docs/decisions.md carries an Update marker"


def test_cli_prints_note_for_a_hit_in_a_split_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI surfaces split_entry_markers' result under the hit it belongs
    to, and prints nothing extra under a hit that is a whole entry."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    _doc, _text, marker_line = _write_split_entry_doc(docs_dir, title=True)
    monkeypatch.setattr(
        _query_module._build,
        "build_index",
        functools.partial(_query_module._build.build_index, docs_dir=docs_dir),
    )

    result = runner.invoke(app, ["zorblax"])
    assert result.exit_code == 0, result.output
    assert f"Update markers at line {marker_line} --" in result.stdout

    result = runner.invoke(app, ["quuxly"])
    assert result.exit_code == 0, result.output
    assert "decisions.md:" in result.stdout
    assert "NOTE" not in result.stdout
