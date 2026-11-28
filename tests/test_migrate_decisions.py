"""Tests for the decisions.md migration dry run (lode-ebayj.3), on a small fixture."""

import json
import os
import random
import subprocess
from pathlib import Path

import pytest
from conftest import load_module_from_path
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parent.parent
_mig = load_module_from_path(
    "migrate_decisions", REPO_ROOT / "scripts" / "migrate_decisions.py"
)
_gate = load_module_from_path(
    "check_decisions_entries_for_migration",
    REPO_ROOT / "scripts" / "check_decisions_entries.py",
)
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "decisions_small.md"
DECISIONS = REPO_ROOT / "docs" / "decisions.md"
# Read at import: every test runs with the list swapped (the autouse fixture).
REAL_NEW_ENTRY_BULLETS = _mig.NEW_ENTRY_BULLETS
runner = CliRunner()

MOVE = _mig.Override(
    "lode-fff1",
    "**Update (lode-aaa3, 2026-03-05):**",
    "misfiled under the second section",
    target="lode-aaa1",
)
DROP = _mig.Override(
    "lode-fff1", "**Update (lode-fff2, 2026-03-06):**", "obsolete pointer"
)


# The fixture file's one listed bullet. A test on an inline text, which has no
# such bullet, empties the list with ``_no_new_entry_bullets``.
@pytest.fixture(autouse=True)
def _fixture_new_entry_bullets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _mig,
        "NEW_ENTRY_BULLETS",
        (("Update (lode-ggg1", "a correction filed as its own entry"),),
    )


@pytest.fixture
def _no_new_entry_bullets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_mig, "NEW_ENTRY_BULLETS", ())


def _blame(text: str, date: str = "2026-04-01") -> dict[int, tuple[str, str]]:
    """A blame answer giving every line one date, for tests that need no real git."""
    return {n: (date, "a" * 40) for n in range(1, text.count("\n") + 3)}


def _text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _body(path: Path) -> str:
    return _mig._strip_frontmatter(path.read_text(encoding="utf-8"))


def _entry(entry_text: str):
    """The one entry of an inline text; the test empties the new-entry list."""
    return _mig.migrate(
        "# t\n\n" + entry_text, overrides=(), blame=_blame(entry_text)
    ).entries[0]


def _by_title(result, fragment: str):
    (hit,) = [e for e in result.entries if fragment in e.title]
    return hit


def _write(result, target: Path) -> None:
    _mig.write_entries(target, result.entries)


def test_splits_on_column_zero_bullets_and_sections_outside_fences() -> None:
    preamble, entries = _mig.split_entries(_text())
    assert preamble == 5
    assert [e.kind for e in entries] == [
        "bullet",
        "bullet",
        "bullet",
        "bullet",
        "section",
        "section",
        "bullet",
        "section",
    ]
    fenced = next(e for e in entries if "Fenced example" in e.title)
    assert "- **not an entry**" in "".join(fenced.lines)
    assert "## not a section either" in "".join(fenced.lines)


def test_a_sub_point_bullet_joins_its_section_and_a_listed_bullet_does_not() -> None:
    _, entries = _mig.split_entries(_text())
    first = next(e for e in entries if "First section" in e.title)
    assert "A bullet inside the section (lode-eee3)" in "".join(first.lines)
    assert not any("lode-eee3" in e.title for e in entries)
    assert any(e.title.startswith("Update (lode-ggg1") for e in entries)
    assert "lode-ggg1" not in "".join(
        next(e for e in entries if "Second section" in e.title).lines
    )
    assert first.sub_points == [(31, "A bullet inside the section (lode-eee3).")]


def test_a_bullet_after_a_listed_bullet_is_an_entry_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "# t\n\n## S\n\n- **sub.** a\n- **Listed.** b\n- **After.** c\n"
    monkeypatch.setattr(_mig, "NEW_ENTRY_BULLETS", (("Listed", "its own"),))
    _, entries = _mig.split_entries(text)
    assert [e.title for e in entries] == ["S", "Listed.", "After."]
    assert entries[0].sub_points == [(5, "sub.")]


@pytest.mark.parametrize("prefix", ["No such bullet", "Update (lode-", ""])
def test_a_new_entry_row_that_matches_nothing_or_twice_is_an_error(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    text = _text() + "\n- **Update (lode-hhh1, 2026-03-11).** A second one.\n"
    monkeypatch.setattr(_mig, "NEW_ENTRY_BULLETS", ((prefix, "x"),))
    with pytest.raises(ValueError, match="new-entry row"):
        _mig.split_entries(text)


def test_the_real_new_entry_list_gives_a_reason_per_row() -> None:
    assert REAL_NEW_ENTRY_BULLETS
    assert all(prefix and reason for prefix, reason in REAL_NEW_ENTRY_BULLETS)


# Goes when the cutover (lode-ebayj.4) removes docs/decisions.md. If the
# lode-sflg section stops deriving decided, lode-pqhhx Q4 says to escalate,
# not to add an override.
def test_the_real_tables_move_the_ucpu_update_and_sflg_stays_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_mig, "NEW_ENTRY_BULLETS", REAL_NEW_ENTRY_BULLETS)
    _, entries = _mig.split_entries(DECISIONS.read_text(encoding="utf-8"))
    _mig.apply_overrides(entries, _mig.OVERRIDES)
    sflg = _mig.find_entry(entries, "lode-sflg")
    sflg_body = "".join(sflg.lines)
    vlok_body = "".join(_mig.find_entry(entries, "lode-vlok").lines)
    moved = "  - **Update (lode-ucpu, 2026-11-20, maintainer):**"
    assert vlok_body.count(moved) == 1
    assert moved not in sflg_body
    assert "  - **Update (lode-smpk, 2026-11-21):** both questions" in sflg_body
    _mig.derive_status(sflg)
    assert sflg.status == "decided"


def test_bodies_concatenate_to_the_source_minus_the_preamble() -> None:
    result = _mig.migrate(
        _text(), blame=_blame(_text()), overrides=(), rng=random.Random(0)
    )
    expected = "".join(_text().splitlines(keepends=True)[result.preamble_lines :])
    assert "".join("".join(e.lines) for e in result.entries) == expected
    assert result.source_body == expected


def test_written_files_hold_each_entry_verbatim(tmp_path: Path) -> None:
    result = _mig.migrate(
        _text(), blame=_blame(_text()), overrides=(), rng=random.Random(0)
    )
    _write(result, tmp_path / "out")
    for entry in result.entries:
        assert _body(tmp_path / "out" / entry.filename) == "".join(entry.lines)
    assert _mig.verify(tmp_path / "out", result) == []


def test_update_marker_counts_are_conserved(tmp_path: Path) -> None:
    result = _mig.migrate(
        _text(), blame=_blame(_text()), overrides=(), rng=random.Random(0)
    )
    _write(result, tmp_path / "out")
    in_text = _mig._MARKER_RE.findall(result.source_body)
    out_text = "".join(_body(p) for p in (tmp_path / "out").glob("*.md"))
    assert len(in_text) == len(_mig._MARKER_RE.findall(out_text)) == 6


def test_override_moves_and_drops_a_block_and_verify_accounts_for_it(
    tmp_path: Path,
) -> None:
    result = _mig.migrate(
        _text(), blame=_blame(_text()), overrides=(MOVE, DROP), rng=random.Random(0)
    )
    _write(result, tmp_path / "out")
    assert _mig.verify(tmp_path / "out", result) == []
    moved = next(a for a in result.applied if a.override is MOVE)
    assert moved.text.splitlines()[0].startswith("  **Update (lode-aaa3")
    assert "Its second line." in moved.text
    assert "lode-fff2" not in moved.text
    home = _by_title(result, "Cache sizing")
    body = "".join(home.lines)
    assert body.count("**Update (lode-aaa3") == 1
    # Appended after the entry's last text, ahead of its trailing blank line.
    assert body.endswith(moved.text + "\n")
    away = "".join(_by_title(result, "Second section").lines)
    assert "lode-aaa3" not in away
    assert "lode-fff2" not in away
    assert "**Update (lode-fff3" in away
    counts = len(_mig._MARKER_RE.findall(result.source_body))
    kept = sum(len(_mig._MARKER_RE.findall("".join(e.lines))) for e in result.entries)
    assert counts - kept == 1


def test_an_override_that_matches_nothing_or_twice_is_an_error() -> None:
    bad_key = _mig.Override("lode-nope", "**Update (x", "x")
    twice = _mig.Override("lode-eee1", "**Update (lode-eee2", "x")
    bad_marker = _mig.Override("lode-fff1", "**Update (lode-zzz", "x")
    for override in (bad_key, twice, bad_marker):
        with pytest.raises(ValueError):
            _mig.migrate(_text(), blame=_blame(_text()), overrides=(override,))


def test_verify_catches_a_body_that_was_edited(tmp_path: Path) -> None:
    result = _mig.migrate(
        _text(), blame=_blame(_text()), overrides=(), rng=random.Random(0)
    )
    _write(result, tmp_path / "out")
    victim = tmp_path / "out" / result.entries[1].filename
    victim.write_text(victim.read_text().replace("Retry twice", "Retry thrice"))
    assert any("differ" in p for p in _mig.verify(tmp_path / "out", result))


def test_verify_catches_a_moved_block_outside_its_target(tmp_path: Path) -> None:
    result = _mig.migrate(
        _text(), blame=_blame(_text()), overrides=(MOVE, DROP), rng=random.Random(0)
    )
    _write(result, tmp_path / "out")
    moved = next(a for a in result.applied if a.override is MOVE)
    home = tmp_path / "out" / _by_title(result, "Cache sizing").filename
    other = tmp_path / "out" / _by_title(result, "Retry policy").filename
    home.write_text(home.read_text().replace(moved.text, ""))
    other.write_text(other.read_text() + moved.text)
    problems = _mig.verify(tmp_path / "out", result)
    assert any("not in its target entry" in p for p in problems)


# Lines 7 and 8 are markers in the B section; the override moves line 7's to A,
# which shortens B by one line and lengthens A by one.
_SHIFTED = (
    "# t\n\n"
    "- **A (lode-a1, 2026-01-01).** x\n\n"
    "## B (lode-b1)\n\n"
    "  **Update (lode-a1, 2026-01-02):** misfiled, it belongs to the lode-a1 entry.\n"
    "  **Update (lode-a1, 2026-01-03):** stays here.\n\n"
    "### Tail heading\n"
)
_SHIFT = _mig.Override(
    "lode-b1", "**Update (lode-a1, 2026-01-02)", "misfiled", target="lode-a1"
)


@pytest.mark.usefixtures("_no_new_entry_bullets")
def test_an_override_leaves_suspect_lines_and_the_mapping_on_source_lines() -> None:
    result = _mig.migrate(
        _SHIFTED, blame=_blame(_SHIFTED), overrides=(_SHIFT,), rng=random.Random(0)
    )
    (moved,) = result.applied
    assert moved.source_line == 7
    # The moved marker is the override table's; the one after it keeps line 8.
    assert [s.split(" (")[0] for s in result.suspects] == ["line 8"]
    section = _by_title(result, "B (lode-b1)")
    tail = next(r for r in result.mapping if r["old_anchor"] == "tail-heading")
    assert (tail["old_line"], tail["new_file"]) == (10, section.filename)


def test_verify_catches_a_missing_file(tmp_path: Path) -> None:
    result = _mig.migrate(
        _text(), blame=_blame(_text()), overrides=(), rng=random.Random(0)
    )
    _write(result, tmp_path / "out")
    (tmp_path / "out" / result.entries[0].filename).unlink()
    assert any("files out" in p for p in _mig.verify(tmp_path / "out", result))


def test_status_and_dates_are_derived_from_the_head() -> None:
    result = _mig.migrate(
        _text(), blame=_blame(_text()), overrides=(MOVE, DROP), rng=random.Random(0)
    )
    by = {e.title.split(" (")[0]: e for e in result.entries}
    cache = _by_title(result, "Cache sizing")
    assert (cache.status, cache.date, cache.date_source) == (
        "decided",
        "2026-01-05",
        "title",
    )
    assert _by_title(result, "Retry policy").date_source.startswith("git blame -M -C")
    assert _by_title(result, "Retry policy").date == "2026-04-01"
    assert cache.status_note.startswith("by Update (lode-aaa2")
    rotation = _by_title(result, "log rotation")
    assert rotation.status == "open"
    assert not rotation.status_unclear
    assert rotation.status_note == "no Update"
    assert rotation.date == "2026-04-01"
    assert _by_title(result, "Fenced example").status == "decided"
    assert by["First section"].status == "decided"


@pytest.mark.usefixtures("_no_new_entry_bullets")
def test_unclear_status_defaults_to_open_and_is_flagged() -> None:
    entry = _entry("- **Open and decided (2026-01-01).** body\n")
    assert (entry.status, bool(entry.status_unclear)) == ("open", True)
    entry = _entry(
        "- **Open: sizing (2026-01-01).** body\n"
        "  **Update (lode-x, 2026-01-02):** wording only.\n"
    )
    assert (entry.status, bool(entry.status_unclear)) == ("open", True)


def test_files_pass_the_per_entry_gate(tmp_path: Path) -> None:
    result = _mig.migrate(_text(), blame=_blame(_text()), overrides=(MOVE, DROP))
    _write(result, tmp_path / "out")
    assert _gate.check_edges(tmp_path / "out") == []
    loaded, problems = _gate.load_entries(tmp_path / "out")
    assert problems == []
    assert len(loaded) == len(result.entries)


def test_ids_are_unique_base36_and_a_seed_repeats_them() -> None:
    first = _mig.migrate(
        _text(), blame=_blame(_text()), overrides=(), rng=random.Random(7)
    )
    again = _mig.migrate(
        _text(), blame=_blame(_text()), overrides=(), rng=random.Random(7)
    )
    ids = [e.entry_id for e in first.entries]
    assert ids == [e.entry_id for e in again.entries]
    assert len(set(ids)) == len(ids)
    assert all(len(i) == 5 and i.isalnum() and i == i.lower() for i in ids)
    assert all(e.filename.startswith(e.entry_id + "-") for e in first.entries)


def test_suspects_list_column_zero_updates_and_misfiled_markers() -> None:
    result = _mig.migrate(_text(), blame=_blame(_text()), overrides=())
    joined = "\n".join(result.suspects)
    assert "column-zero Update entry" in joined
    misfiled = [s for s in result.suspects if "lode-aaa3" in s]
    assert misfiled
    assert (
        "misfiled" in misfiled[0] or "belongs" in misfiled[0] or "heads" in misfiled[0]
    )


def test_mapping_covers_every_heading_anchor_with_github_numbering() -> None:
    result = _mig.migrate(
        _text(), blame=_blame(_text()), overrides=(), rng=random.Random(0)
    )
    anchors = {r["old_anchor"]: r for r in result.mapping if r["old_anchor"]}
    assert "first-section-decided-lode-eee1" in anchors
    assert "first-section-decided-lode-eee1-1" in anchors
    assert "fixture-decisions-log" in anchors
    assert anchors["fixture-decisions-log"]["new_file"] == "README.md"
    first = next(e for e in result.entries if "First section" in e.title)
    assert anchors["first-section-decided-lode-eee1"]["new_file"] == first.filename
    assert "not-a-section-either" not in anchors
    bullets = [r for r in result.mapping if r["kind"] == "bullet"]
    assert len(bullets) == 6
    # A section's sub-point keeps its row, pointing at the section's file.
    sub_point = next(r for r in bullets if "lode-eee3" in r["old_title"])
    assert (sub_point["old_line"], sub_point["new_file"]) == (31, first.filename)


@pytest.mark.usefixtures("_no_new_entry_bullets")
def test_an_explicit_anchor_the_mapping_has_no_row_for_is_an_error() -> None:
    text = '# t\n\n- **A (2026-01-01).** x\n  <a id="pin"></a>\n'
    with pytest.raises(ValueError, match="pin"):
        _mig.migrate(text, overrides=(), blame=_blame(text))


def test_unmapped_links_finds_a_dangling_anchor(tmp_path: Path) -> None:
    result = _mig.migrate(
        _text(), blame=_blame(_text()), overrides=(), rng=random.Random(0)
    )
    # Joined at run time so the repo-wide link scan does not read these as links.
    page = "decisions.md"
    (tmp_path / "a.md").write_text(
        f"[ok]({page}#first-section-decided-lode-eee1) [bad]({page}#gone-heading)"
    )
    found, missing = _mig.unmapped_links(tmp_path, result.mapping)
    assert found == 2
    assert missing == ["a.md: #gone-heading"]


def test_cli_writes_entries_and_mapping_and_prints_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_mig, "OVERRIDES", (MOVE, DROP))
    monkeypatch.setattr(_mig, "git_blame_dates", lambda src: _blame(_text()))
    target = tmp_path / "out"
    mapping = tmp_path / "map.json"
    result = runner.invoke(
        _mig.app,
        [str(FIXTURE), str(target), "--mapping", str(mapping), "--seed", "3"],
    )
    assert result.exit_code == 0, result.output
    assert "VERIFY: ok" in result.output
    assert "OVERRIDE TABLE" in result.output
    assert "(3 lines from line 38)" in result.output
    assert "SECTION SUB-POINTS (1 bullets merged into 1 sections" in result.output
    assert "line 31 (section at line 25): 'A bullet inside the section" in result.output
    assert "NEW-ENTRY BULLETS (1):" in result.output
    assert "line 46: 'Update (lode-ggg1, 2026-03-10)" in result.output
    assert "SUSPECTS" in result.output
    assert "Every entry with open wording (2):" in result.output
    assert "line 13: open (no Update)" in result.output
    # A date not in the title is listed with its blame source.
    assert "line 10: 2026-04-01 (git blame -M -C, commit aaaaaaaaaa)" in result.output
    assert len(list(target.glob("*.md"))) == 8
    assert json.loads(mapping.read_text())


def test_cli_refuses_a_target_that_already_holds_files(tmp_path: Path) -> None:
    target = tmp_path / "out"
    target.mkdir()
    (target / "README.md").write_text("keep")
    result = runner.invoke(_mig.app, [str(FIXTURE), str(target)])
    assert result.exit_code == 2
    assert (target / "README.md").read_text() == "keep"


# Heads of the lode-m4rzt examples (decisions.md lines 59, 144, 151, 159, 173, 183,
# 270) and of the question-titled bullet with a leaning that the ticket quotes.
@pytest.mark.usefixtures("_no_new_entry_bullets")
@pytest.mark.parametrize(
    "text",
    [
        (
            "- **External refresh: on-access revalidation vs. scheduled background refresh.** "
            "Leaning\n  **on-access with a short TTL cache** (2026-01-01).\n"
        ),
        (
            "- **History compaction / squash policy.** Not needed for years; revisit if "
            "storage matters. (2026-01-01)\n"
        ),
        (
            "- **Cache rebuild cost is non-uniform** (2026-01-01). Embeddings rebuild "
            "cheaply.\n  Decide whether to snapshot them.\n"
        ),
        (
            "- **Span-annotation fuzzy re-anchor threshold** — tune when span annotations "
            "are actually built. (2026-01-01)\n"
        ),
        (
            "- **Faithfulness entailment threshold (ships untuned, must be revisited).** "
            "v1 verifies (2026-01-01).\n"
        ),
        (
            "- **Chunk size + overlap.** The fallback threshold and the overlap are tuning "
            "knobs; tuning waits. (2026-01-01)\n"
        ),
        (
            "- **Rerank model + threshold tuning.** Choosing/tuning the model waits until "
            "there is a corpus. (2026-01-01)\n"
        ),
        (
            "- **A repo-wide shared factory, or file-local helpers?** Leaning toward "
            "file-local. (2026-01-01)\n"
        ),
    ],
)
def test_deferred_leaning_revisit_entries_derive_open(text: str) -> None:
    entry = _entry(text)
    assert entry.status == "open"
    assert entry.title_status == "decided"


@pytest.mark.usefixtures("_no_new_entry_bullets")
@pytest.mark.parametrize(
    "text",
    [
        (
            "- **lode-3v1p (2026-07-20) closes the dirt-axis residual left open above: "
            "git clean runs.** body\n"
        ),
        (
            "- **2026-08-08 (`lode-oca9`) — re-cut the seam left open by `lode-r9z0`; both "
            "candidates adopted.** body\n"
        ),
    ],
)
def test_left_open_phrase_in_a_title_is_not_open_wording(text: str) -> None:
    entry = _entry(text)
    assert (entry.status, entry.title_status) == ("decided", "open")


@pytest.mark.usefixtures("_no_new_entry_bullets")
def test_a_title_that_starts_with_left_open_is_open() -> None:
    entry = _entry("- **Left open: the web-fetch seam (2026-01-01).** body\n")
    assert entry.status == "open"


@pytest.mark.usefixtures("_no_new_entry_bullets")
def test_decided_title_beats_deferral_wording_in_the_first_paragraph() -> None:
    entry = _entry("- **DECIDED (2026-01-01): X.** Revisit only if Y shows up.\n")
    assert entry.status == "decided"


@pytest.mark.usefixtures("_no_new_entry_bullets")
@pytest.mark.parametrize(
    ("head", "status", "unclear"),
    [
        # decisions.md line 366: the title says the item was NOT deferred.
        ("**Epic auto-close — rejected, not merely deferred.** Text", "decided", False),
        ("**Eval harness.** It is **no longer deferred**", "decided", False),
        # Lines 350 and 2601: deferred, and the decided word is the negated one.
        ("**Bounce-lineage cap — deferred, not built.** Text", "open", False),
        ("**Revision pinning — deferred, not rejected.** Text", "open", False),
        ("**Debug flag (accepted tradeoff, split deferred).** Text", "open", True),
        # Line 5908: the entry is the revisit, and it settled the question.
        ("**Colour, revisited: theming settled.** Text", "decided", False),
        ("**Colour.** Stays unless the position is ever revisited", "open", False),
        ("**Colour knob: no longer open.** Text", "decided", False),
    ],
)
def test_negated_and_backward_looking_wording(
    head: str, status: str, unclear: bool
) -> None:
    entry = _entry(f"- {head} (2026-01-01).\n")
    assert (entry.status, bool(entry.status_unclear)) == (status, unclear)


@pytest.mark.usefixtures("_no_new_entry_bullets")
def test_a_phrase_wrapped_across_two_source_lines_is_read() -> None:
    entry = _entry(
        "- **Re-anchor threshold (2026-01-01).** A knob to tune\n  when built.\n"
    )
    assert entry.status == "open"


@pytest.mark.usefixtures("_no_new_entry_bullets")
@pytest.mark.parametrize(
    ("section", "status"),
    [
        ("## Rerank model (2026-01-01)\n\nLeaning toward X.\n\nMore.\n", "open"),
        ("## Rerank model (2026-01-01)\n\nX it is.\n\nLeaning was Y.\n", "decided"),
        ("## Rerank model (decided, 2026-01-01)\n\nThe leaning was X.\n", "decided"),
    ],
)
def test_a_section_is_read_past_its_heading_to_its_first_paragraph_only(
    section: str, status: str
) -> None:
    assert _entry(section).status == status


@pytest.mark.usefixtures("_no_new_entry_bullets")
@pytest.mark.parametrize(
    ("entry", "row"),
    [
        (
            "- **Chunk size (2026-01-01).** Tuning waits.\n",
            "line 3: decided -> open (first paragraph says 'Tuning'): 'Chunk",
        ),
        (
            "- **2026-01-01 closes the gap left open above.** body\n",
            "line 3: open -> decided (title's 'open' is not read as open wording): ",
        ),
    ],
)
def test_report_lists_a_title_disagreement_with_the_wording(
    entry: str, row: str
) -> None:
    result = _mig.migrate("# t\n\n" + entry, overrides=())
    report = _mig.render_report(result, [], None)
    assert "TITLE DISAGREEMENTS (1" in report
    assert row in report


@pytest.mark.usefixtures("_no_new_entry_bullets")
def test_title_date_wins_and_an_undated_entry_takes_its_blame_date() -> None:
    text = "# t\n\n- **Dated (2026-01-05).** body\n\n- **Undated.** body\n"
    blame = {3: ("2030-01-01", "b" * 40), 5: ("2026-07-28", "c" * 40)}
    dated, undated = _mig.migrate(text, overrides=(), blame=blame).entries
    assert (dated.date, dated.date_source) == ("2026-01-05", "title")
    assert (undated.date, undated.date_source) == (
        "2026-07-28",
        "git blame -M -C, commit cccccccccc",
    )


@pytest.mark.usefixtures("_no_new_entry_bullets")
def test_an_iso_date_past_the_title_is_not_a_date_source() -> None:
    text = "# t\n\n- **Undated.** decided 2020-02-02 in the body\n"
    (entry,) = _mig.migrate(
        text, overrides=(), blame={3: ("2026-07-28", "c" * 40)}
    ).entries
    assert entry.date == "2026-07-28"


@pytest.mark.usefixtures("_no_new_entry_bullets")
def test_an_undated_entry_without_blame_is_an_error_not_a_guess() -> None:
    with pytest.raises(_mig.BlameUnavailable):
        _mig.migrate("# t\n\n- **Undated.** body\n", overrides=())


def _git(repo: Path, *args: str, when: str = "2026-05-01T12:00:00+00:00") -> None:
    env = {
        "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": os.environ["PATH"], "HOME": str(repo),
    }  # fmt: skip
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def test_git_blame_dates_reads_each_lines_commit_date_in_utc(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    f = tmp_path / "d.md"
    f.write_text("one\ntwo\n")
    _git(tmp_path, "add", "d.md")
    _git(tmp_path, "commit", "-q", "-m", "a", when="2026-05-01T23:30:00-05:00")
    f.write_text("one\ntwo\nthree\n")
    _git(tmp_path, "commit", "-q", "-am", "b", when="2026-06-02T01:00:00+00:00")
    dates = _mig.git_blame_dates(f)
    assert [dates[n][0] for n in (1, 2, 3)] == [
        "2026-05-02",
        "2026-05-02",
        "2026-06-02",
    ]
    f.write_text("one\ntwo\nthree\nfour\n")
    with pytest.raises(_mig.BlameUnavailable, match="uncommitted"):
        _mig.git_blame_dates(f)


def test_git_blame_dates_fails_loudly_outside_a_repository(tmp_path: Path) -> None:
    f = tmp_path / "d.md"
    f.write_text("one\n")
    with pytest.raises(_mig.BlameUnavailable):
        _mig.git_blame_dates(f)


def test_cli_exits_2_when_blame_is_unavailable(tmp_path: Path) -> None:
    src = tmp_path / "d.md"
    src.write_text("# t\n\n- **Undated.** body\n")
    result = runner.invoke(_mig.app, [str(src), str(tmp_path / "out")])
    assert result.exit_code == 2
    assert not (tmp_path / "out").exists()
