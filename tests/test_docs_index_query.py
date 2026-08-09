"""Tests for scripts/docs_index_query.py -- the docs/ lookup index query CLI
(lode-t6o1.3).

Covers the acceptance criteria: ranked pointers with first-line + snippet
(never a whole unit, never synthesized prose), the --class filter, and the
FTS5 input-escaping regression set (a bd issue id, a slash-prefixed
question, and a hyphenated term must each return clean results -- never a
sqlite3 error).
"""

from pathlib import Path

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
    for path, line_lo, line_hi, first_line, snippet in results:
        assert isinstance(path, str)
        assert line_lo <= line_hi
        assert first_line
        assert len(snippet) <= _query_module._SNIPPET_CHARS + len("...")


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
    # single result line must stay short.
    for line in result.stdout.splitlines():
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
