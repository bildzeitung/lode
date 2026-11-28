"""Tests for the decisions.md migration dry run (lode-ebayj.3), on a small fixture."""

import json
import random
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


def _text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _body(path: Path) -> str:
    return _mig._strip_frontmatter(path.read_text(encoding="utf-8"))


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


def test_bodies_concatenate_to_the_source_minus_the_preamble() -> None:
    result = _mig.migrate(_text(), overrides=(), rng=random.Random(0))
    expected = "".join(_text().splitlines(keepends=True)[result.preamble_lines :])
    assert "".join("".join(e.lines) for e in result.entries) == expected
    assert result.source_body == expected


def test_written_files_hold_each_entry_verbatim(tmp_path: Path) -> None:
    result = _mig.migrate(_text(), overrides=(), rng=random.Random(0))
    _write(result, tmp_path / "out")
    for entry in result.entries:
        assert _body(tmp_path / "out" / entry.filename) == "".join(entry.lines)
    assert _mig.verify(tmp_path / "out", result) == []


def test_update_marker_counts_are_conserved(tmp_path: Path) -> None:
    result = _mig.migrate(_text(), overrides=(), rng=random.Random(0))
    _write(result, tmp_path / "out")
    in_text = _mig._MARKER_RE.findall(result.source_body)
    out_text = "".join(_body(p) for p in (tmp_path / "out").glob("*.md"))
    assert len(in_text) == len(_mig._MARKER_RE.findall(out_text)) == 6


def test_override_moves_and_drops_a_block_and_verify_accounts_for_it(
    tmp_path: Path,
) -> None:
    result = _mig.migrate(_text(), overrides=(MOVE, DROP), rng=random.Random(0))
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
            _mig.migrate(_text(), overrides=(override,))


def test_verify_catches_a_body_that_was_edited(tmp_path: Path) -> None:
    result = _mig.migrate(_text(), overrides=(), rng=random.Random(0))
    _write(result, tmp_path / "out")
    victim = tmp_path / "out" / result.entries[1].filename
    victim.write_text(victim.read_text().replace("Retry twice", "Retry thrice"))
    assert any("differ" in p for p in _mig.verify(tmp_path / "out", result))


def test_verify_catches_a_moved_block_outside_its_target(tmp_path: Path) -> None:
    result = _mig.migrate(_text(), overrides=(MOVE, DROP), rng=random.Random(0))
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
    result = _mig.migrate(_SHIFTED, overrides=(_SHIFT,), rng=random.Random(0))
    (moved,) = result.applied
    assert moved.source_line == 7
    # The moved marker is the override table's; the one after it keeps line 8.
    assert [s.split(" (")[0] for s in result.suspects] == ["line 8"]
    section = _by_title(result, "B (lode-b1)")
    tail = next(r for r in result.mapping if r["old_anchor"] == "tail-heading")
    assert (tail["old_line"], tail["new_file"]) == (10, section.filename)


def test_verify_catches_a_missing_file(tmp_path: Path) -> None:
    result = _mig.migrate(_text(), overrides=(), rng=random.Random(0))
    _write(result, tmp_path / "out")
    (tmp_path / "out" / result.entries[0].filename).unlink()
    assert any("files out" in p for p in _mig.verify(tmp_path / "out", result))


def test_status_and_dates_are_derived_from_the_head() -> None:
    result = _mig.migrate(_text(), overrides=(MOVE, DROP), rng=random.Random(0))
    by = {e.title.split(" (")[0]: e for e in result.entries}
    cache = _by_title(result, "Cache sizing")
    assert (cache.status, cache.date, cache.date_source) == (
        "decided",
        "2026-01-05",
        "title",
    )
    assert _by_title(result, "Retry policy").date_source == "first paragraph"
    assert _by_title(result, "Retry policy").date == "2026-02-03"
    assert cache.status_note.startswith("by Update (lode-aaa2")
    rotation = _by_title(result, "log rotation")
    assert rotation.status == "open"
    assert not rotation.status_unclear
    assert rotation.status_note == "no Update"
    assert rotation.date_source == "inferred from a neighbour"
    assert rotation.date == "2026-02-03"
    assert _by_title(result, "Fenced example").status == "decided"
    assert by["First section"].status == "decided"


@pytest.mark.usefixtures("_no_new_entry_bullets")
def test_unclear_status_defaults_to_open_and_is_flagged() -> None:
    both = "# t\n\n- **Open and decided (2026-01-01).** body\n"
    entry = _mig.migrate(both, overrides=()).entries[0]
    assert (entry.status, bool(entry.status_unclear)) == ("open", True)
    unruled = (
        "# t\n\n- **Open: sizing (2026-01-01).** body\n"
        "  **Update (lode-x, 2026-01-02):** wording only.\n"
    )
    entry = _mig.migrate(unruled, overrides=()).entries[0]
    assert (entry.status, bool(entry.status_unclear)) == ("open", True)


def test_files_pass_the_per_entry_gate(tmp_path: Path) -> None:
    result = _mig.migrate(_text(), overrides=(MOVE, DROP))
    _write(result, tmp_path / "out")
    assert _gate.check_edges(tmp_path / "out") == []
    loaded, problems = _gate.load_entries(tmp_path / "out")
    assert problems == []
    assert len(loaded) == len(result.entries)


def test_ids_are_unique_base36_and_a_seed_repeats_them() -> None:
    first = _mig.migrate(_text(), overrides=(), rng=random.Random(7))
    again = _mig.migrate(_text(), overrides=(), rng=random.Random(7))
    ids = [e.entry_id for e in first.entries]
    assert ids == [e.entry_id for e in again.entries]
    assert len(set(ids)) == len(ids)
    assert all(len(i) == 5 and i.isalnum() and i == i.lower() for i in ids)
    assert all(e.filename.startswith(e.entry_id + "-") for e in first.entries)


def test_suspects_list_column_zero_updates_and_misfiled_markers() -> None:
    result = _mig.migrate(_text(), overrides=())
    joined = "\n".join(result.suspects)
    assert "column-zero Update entry" in joined
    misfiled = [s for s in result.suspects if "lode-aaa3" in s]
    assert misfiled
    assert (
        "misfiled" in misfiled[0] or "belongs" in misfiled[0] or "heads" in misfiled[0]
    )


def test_mapping_covers_every_heading_anchor_with_github_numbering() -> None:
    result = _mig.migrate(_text(), overrides=(), rng=random.Random(0))
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
        _mig.migrate(text, overrides=())


def test_unmapped_links_finds_a_dangling_anchor(tmp_path: Path) -> None:
    result = _mig.migrate(_text(), overrides=(), rng=random.Random(0))
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
    assert "Every title with open wording (2):" in result.output
    assert "line 13: open (no Update)" in result.output
    # A date found past the title is listed as a guess.
    assert "line 10: 2026-02-03 (first paragraph)" in result.output
    assert len(list(target.glob("*.md"))) == 8
    assert json.loads(mapping.read_text())


def test_cli_refuses_a_target_that_already_holds_files(tmp_path: Path) -> None:
    target = tmp_path / "out"
    target.mkdir()
    (target / "README.md").write_text("keep")
    result = runner.invoke(_mig.app, [str(FIXTURE), str(target)])
    assert result.exit_code == 2
    assert (target / "README.md").read_text() == "keep"
