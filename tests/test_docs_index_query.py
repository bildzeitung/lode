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


@pytest.mark.parametrize("title", [True, False], ids=["h1-title", "no-heading"])
def test_split_entry_markers_points_every_piece_of_a_split_entry_at_its_marker(
    tmp_path: Path, title: bool
) -> None:
    """An Update marker at the end of an entry over MAX_UNIT_BYTES lands in a
    different unit from the stale claim (lode-funp); every chunk of that entry
    must still point at it, and no unit outside it may."""
    doc, text, marker_line = _write_split_entry_doc(tmp_path, title=title)
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


def test_split_entry_markers_for_a_split_entry_after_a_section(tmp_path: Path) -> None:
    """An entry appended after a ``##`` section cannot be told from the
    section's own bullets, so from the heading unit on every unit lists the
    markers below it (lode-8cp63): the pieces of the split entry above its
    marker still point at it, the heading unit and the big entry's pieces also
    list the small entry's marker, and no unit lists a marker above it."""
    doc, text, marker_line = _write_split_entry_doc(
        tmp_path, title=True, h3_section=True
    )
    small_marker = next(
        i + 1 for i, line in enumerate(text.splitlines()) if "lode-other" in line
    )
    units = _query_module._build._chunker.chunk_file(doc, text)
    assert [u.first_line for u in units[:3]] == [
        "# Title",
        "## Section",
        "- **Big entry.** stale claim zorblax",
    ]
    assert sum(u.line_hi < marker_line for u in units[2:]) > 1
    assert _query_module.split_entry_markers(str(doc), 1, units[0].line_hi) == []
    for u in units[1:]:
        assert _query_module.split_entry_markers(str(doc), u.line_lo, u.line_hi) == [
            m for m in (marker_line, small_marker) if m > u.line_hi
        ]


def test_split_entry_markers_in_a_section_cut_at_its_h3_headings(
    tmp_path: Path,
) -> None:
    """A ``##`` section over MAX_UNIT_BYTES is cut at its ``###`` headings
    (lode-vt7x), and the section is one entry (lode-pe38): an Update under
    ``### B`` is pointed at by every unit of the section. ``### A`` is itself
    oversized and holds the section's own list, so its bullets are units that
    open like an appended entry; the entry after the section is not one of
    them and lists nothing. Its Update is listed by every unit of the section
    all the same (lode-ineef): a bullet after the section's own text may be
    the section's own."""
    paragraph = "word " * 400
    own_bullets = [f"- own bullet {i} " + "x" * 3000 for i in range(8)]
    marker = "**Update (lode-test, 2026-01-01)** -- corrected."
    other = "  **Update (lode-other)** -- the entry's."
    text = (
        "# Title\n\n## Section\n\nintro\n\n### A\n\n"
        + "\n".join(own_bullets)
        + "\n\n### B\n\n"
        + "\n\n".join([paragraph] * 10)
        + f"\n\n{marker}\n"
        "- **Entry after.** e\n"
        f"{other}\n"
    )
    doc = tmp_path / "decisions.md"
    doc.write_text(text, encoding="utf-8")
    lines = text.splitlines()
    marker_line, other_line = lines.index(marker) + 1, lines.index(other) + 1
    units = _query_module._build._chunker.chunk_file(doc, text)
    firsts = [u.first_line for u in units]
    assert firsts[:3] == ["# Title", "## Section", "### A"]
    assert firsts[3:11] == own_bullets
    assert firsts[11] == "### B"
    assert firsts[-2:] == [marker, "- **Entry after.** e"]
    for u in units:
        assert _query_module.split_entry_markers(str(doc), u.line_lo, u.line_hi) == (
            [marker_line, other_line] if u in units[1:-1] else []
        )


def test_split_entry_markers_in_an_oversized_section_with_no_heading(
    tmp_path: Path,
) -> None:
    """A ``##`` section over MAX_UNIT_BYTES with no heading inside it is cut at
    its own bullets, and is one entry (lode-tb0j): an Update in its closing
    paragraph is pointed at by every unit of the section, not by the entry
    after it. So is the Update one own bullet carries: every other bullet's
    unit lists it too, the cost the lode-tb0j Update in docs/decisions.md
    accepts. Every unit of the section also lists the Update of the entry
    after it (lode-ineef): a bullet after the section's own text may be the
    section's own."""
    own_marker = "  **Update (lode-own)** -- corrects own bullet 2."
    own_bullets = [f"- own bullet {i} " + "x" * 3000 for i in range(8)]
    own_bullets[2] += "\n" + own_marker
    marker = "**Update (lode-test, 2026-01-01)** -- corrected."
    other = "  **Update (lode-other)** -- the entry's."
    text = (
        "# Title\n\n## Section\n\nintro\n\n"
        + "\n".join(own_bullets)
        + f"\n\n{marker}\n"
        "- **Entry after.** e\n"
        f"{other}\n"
    )
    doc = tmp_path / "decisions.md"
    doc.write_text(text, encoding="utf-8")
    lines = text.splitlines()
    marker_lines = [lines.index(own_marker) + 1, lines.index(marker) + 1]
    other_line = lines.index(other) + 1
    units = _query_module._build._chunker.chunk_file(doc, text)
    assert [u.first_line for u in units[:2]] == ["# Title", "## Section"]
    assert len(units) > 4
    for u in units:
        assert _query_module.split_entry_markers(str(doc), u.line_lo, u.line_hi) == (
            [*marker_lines, other_line] if u in units[1:-1] else []
        )


def test_split_entry_markers_agrees_with_the_chunker_on_the_real_decisions_md() -> None:
    """The lookup must fire on the file it exists for, whose shape a fixture
    can get wrong: the real file opens with an H1 title, so its entries come
    from the heading unit's hard-split and not from the no-headings rule.

    Entry boundaries are taken from the chunker's own units rather than from a
    second line scan: a unit that does not open on a structural line continues
    the entry of the unit before it, and so does a unit inside a section the
    chunker reports as oversized, which also lists the markers after the
    section's own text, up to the next ``##`` (lode-ineef). Any other unit at
    or after the first ``##`` lists the markers below it, up to the next ``##``
    (lode-8cp63); where each ``##`` section starts is read off the units too,
    not off ``heading_sections``, which the lookup itself calls.
    """
    path = REPO_ROOT / "docs" / "decisions.md"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    flags = _query_module.fence_flags(lines)
    chunker = _query_module._build._chunker

    def markers(lo: int, hi: int) -> list[int]:
        return [
            n
            for n in range(lo, hi + 1)
            if not flags[n - 1] and _query_module._UPDATE_MARKER in lines[n - 1]
        ]

    oversized = chunker.oversized_sections(path, text)
    units = chunker.chunk_file(path, text)
    h2_starts = [u.line_lo for u in units if u.first_line.startswith("## ")]
    assert h2_starts == [lo for lo, _ in chunker.heading_sections(path, text)]
    entries: list[list] = []
    for unit in units:
        # Strictly after the section's first line: the unit on that line opens
        # the entry.
        in_section = any(lo < unit.line_lo <= hi for lo, hi in oversized)
        if entries and (in_section or not chunker.is_structural_line(unit.first_line)):
            entries[-1].append(unit)
        else:
            entries.append([unit])

    noted = 0
    for entry in entries:
        whole = markers(entry[0].line_lo, entry[-1].line_hi) if len(entry) > 1 else []
        noted += bool(whole)
        for unit in entry:
            next_h2 = next((s for s in h2_starts if s > unit.line_lo), len(lines) + 1)
            if any(lo <= unit.line_lo <= hi for lo, hi in oversized):
                expected = whole + markers(entry[-1].line_hi + 1, next_h2 - 1)
            elif unit.line_lo >= h2_starts[0]:
                expected = markers(unit.line_hi + 1, next_h2 - 1)
            else:
                expected = whole
            assert (
                _query_module.split_entry_markers(str(path), unit.line_lo, unit.line_hi)
                == expected
            )
    assert noted, "no split entry of docs/decisions.md carries an Update marker"


def test_split_entry_markers_points_the_accepted_limit_bullet_at_its_update() -> None:
    """The case lode-8cp63 was filed for, on the real file: the lode-21sxk
    section ends in its own bullet list, and the lode-v62sn Update that
    corrects its "Accepted limit" bullet sits under the last bullet."""
    path = REPO_ROOT / "docs" / "decisions.md"
    text = path.read_text(encoding="utf-8")
    (bullet,) = [
        u
        for u in _query_module._build._chunker.chunk_file(path, text)
        if u.first_line.startswith("- **Accepted limit.** While a land pass holds")
    ]
    (update,) = [
        n
        for n, line in enumerate(text.splitlines(), start=1)
        if "**Update (lode-v62sn, 2026-11-25):**" in line
    ]
    assert update > bullet.line_hi
    assert update in _query_module.split_entry_markers(
        str(path), bullet.line_lo, bullet.line_hi
    )


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


def test_split_entry_markers_in_a_section_under_the_limit_with_own_bullets(
    tmp_path: Path,
) -> None:
    """A ``##`` section under MAX_UNIT_BYTES that ends in its own bullet list
    has its bullets cut into units, and its Update sits under the last
    (lode-8cp63). A hit on the heading unit or an earlier bullet lists it; the
    bullet it sits under shows it and gets no NOTE. The lookup stops at the
    next ``##`` and never looks above the hit: the bullet before ``## Section``
    lists nothing, and no unit of ``## Section`` lists that bullet's marker."""
    lone_marker = "  **Update (lode-lone)** -- the lone bullet's."
    marker = "  **Update (lode-test, 2026-01-01)** -- corrects bullet one."
    text = (
        f"# Title\n\n## Before\n\nprose\n\n- lone bullet\n{lone_marker}\n\n"
        "## Section\n\nintro\n\n"
        "- **Bullet one.** stale zorblax\n"
        "- **Bullet two.** other\n"
        f"{marker}\n"
    )
    doc = tmp_path / "decisions.md"
    doc.write_text(text, encoding="utf-8")
    lines = text.splitlines()
    lone_line, marker_line = lines.index(lone_marker) + 1, lines.index(marker) + 1
    units = _query_module._build._chunker.chunk_file(doc, text)
    expected = {
        "# Title": [],
        "## Before": [lone_line],
        "- lone bullet": [],
        "## Section": [marker_line],
        "- **Bullet one.** stale zorblax": [marker_line],
        "- **Bullet two.** other": [],
    }
    assert [u.first_line for u in units] == list(expected)
    for u in units:
        assert (
            _query_module.split_entry_markers(str(doc), u.line_lo, u.line_hi)
            == expected[u.first_line]
        )


def test_split_entry_markers_in_an_oversized_section_with_closing_bullets(
    tmp_path: Path,
) -> None:
    """A hit in an oversized section's own text lists the Update under a
    top-level bullet after that text, before the next ``##`` (lode-ineef), and
    nothing from the next section. The bullets after the own text are read as
    they are after a small section (lode-8cp63): the earlier bullet lists the
    Update under the later."""
    marker = "  **Update (lode-test, 2026-01-01)** -- corrects bullet one."
    next_marker = "  **Update (lode-next)** -- the next section's."
    text = (
        "# Title\n\n## Section\n\n"
        + "\n\n".join(["word " * 400] * 10)
        + "\n\n- **Bullet one.** stale\n- **Bullet two.** other\n"
        f"{marker}\n\n## Next\n\n- next bullet\n{next_marker}\n"
    )
    doc = tmp_path / "decisions.md"
    doc.write_text(text, encoding="utf-8")
    lines = text.splitlines()
    marker_line, next_line = lines.index(marker) + 1, lines.index(next_marker) + 1
    chunker = _query_module._build._chunker
    assert chunker.oversized_sections(doc, text)
    units = chunker.chunk_file(doc, text)
    outside = {
        "# Title": [],
        "- **Bullet one.** stale": [marker_line],
        "- **Bullet two.** other": [],
        "## Next": [next_line],
        "- next bullet": [],
    }
    assert sum(u.first_line not in outside for u in units) > 2
    for u in units:
        assert _query_module.split_entry_markers(
            str(doc), u.line_lo, u.line_hi
        ) == outside.get(u.first_line, [marker_line])


def test_split_entry_markers_on_an_oversized_section_that_is_one_unit(
    tmp_path: Path,
) -> None:
    """An oversized section with nowhere to cut is one unit. A hit on it shows
    the marker in its own text and gets no NOTE for that one, only for the
    marker under the bullet after it."""
    text = (
        "# Title\n\n## Section\n"
        + "x" * 20000
        + " **Update (lode-in)** -- in the own text.\n"
        "- **Bullet.** b\n  **Update (lode-after)** -- under the bullet.\n"
    )
    doc = tmp_path / "decisions.md"
    doc.write_text(text, encoding="utf-8")
    chunker = _query_module._build._chunker
    assert chunker.oversized_sections(doc, text) == [(3, 4)]
    _title, section, bullet = chunker.chunk_file(doc, text)
    assert (section.line_lo, section.line_hi) == (3, 4)
    assert _query_module.split_entry_markers(str(doc), 3, 4) == [6]
    assert (
        _query_module.split_entry_markers(str(doc), bullet.line_lo, bullet.line_hi)
        == []
    )


def test_split_entry_markers_in_an_oversized_h1_section_of_a_log_with_no_h2(
    tmp_path: Path,
) -> None:
    """A log with no ``##`` has no ``##`` section to bound the lookup after an
    oversized H1 section's own text, so a hit there lists that text's markers
    only: not the one under the bullet after it, and not a later H1 section's."""
    own_marker = "**Update (lode-own)** -- in the own text."
    text = (
        "# One\n\n" + "\n\n".join(["word " * 400] * 10) + f"\n\n{own_marker}\n"
        "- entry\n  **Update (lode-entry)** -- the entry's.\n\n"
        "# Two\n\nprose\n\n- entry\n  **Update (lode-two)** -- the next section's.\n"
    )
    doc = tmp_path / "decisions.md"
    doc.write_text(text, encoding="utf-8")
    own_line = text.splitlines().index(own_marker) + 1
    chunker = _query_module._build._chunker
    assert chunker.oversized_sections(doc, text) == [(1, own_line)]
    units = chunker.chunk_file(doc, text)
    assert sum(u.line_hi <= own_line for u in units) > 2
    for u in units:
        assert _query_module.split_entry_markers(str(doc), u.line_lo, u.line_hi) == (
            [own_line] if u.line_hi <= own_line else []
        )


def _fake_run(
    monkeypatch: pytest.MonkeyPatch, grep: tuple[int, str], bd: tuple[int, str] | None
) -> None:
    """Stub the id-check subprocesses; ``bd=None`` simulates bd unavailable."""
    import subprocess

    def fake(args: list[str]) -> subprocess.CompletedProcess[str] | None:
        rc, out = grep if args[0] == "git" else (bd if bd else (None, ""))
        if rc is None:
            return None
        return subprocess.CompletedProcess(args, rc, out, "")

    monkeypatch.setattr(_query_module, "_run", fake)


def test_zero_hit_bare_id_exists_in_bd(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, (1, ""), (0, "[{}]"))
    result = runner.invoke(app, ["lode-zzzzz"])
    assert "No results." in result.stdout
    assert "exists in bd but no docs/ entry" in result.stdout


def test_zero_hit_bare_id_missing_from_bd(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, (1, ""), (1, '{"error": "no issues found matching"}'))
    result = runner.invoke(app, ["lode-zzzzz"])
    assert "does not exist in bd" in result.stdout


def test_zero_hit_bare_id_on_origin_trunk(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, (0, "origin/trunk:docs/decisions.md\n"), (0, "[{}]"))
    result = runner.invoke(app, ["lode-zzzzz"])
    assert "origin/trunk (docs/decisions.md)" in result.stdout
    assert "exists in bd" not in result.stdout


def test_zero_hit_bare_id_falls_back_when_bd_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_run(monkeypatch, (1, ""), None)
    result = runner.invoke(app, ["lode-zzzzz"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "No results."


def test_zero_hit_non_id_query_gets_no_note(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, (1, ""), (0, "[{}]"))
    result = runner.invoke(app, ["zzz not an id zzz"])
    assert result.stdout.strip() == "No results."


def test_run_returns_none_when_the_subprocess_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    def hang(*_a: object, **kw: object) -> None:
        assert kw["timeout"] == _query_module._ID_CHECK_TIMEOUT_S
        raise subprocess.TimeoutExpired("bd", 5)

    monkeypatch.setattr(subprocess, "run", hang)
    assert _query_module._run(["bd", "show", "x"]) is None
