"""Tests for scripts/check_docstring_refs.py, the docstring-role gate (lode-8oeu).

Exercises the units the gate is built from -- ``normalize_ref`` (whitespace
collapsing + ``~`` stripping), ``resolve_ref`` (module-attribute resolution,
including the dataclass/pydantic-field special case), and ``check`` (the
tracked-file walk) -- plus one end-to-end regression against a synthetic
fixture tree so a real dangling ref, a real re-export path, and a real
line-wrapped-but-correct ref are all covered together.
"""

from __future__ import annotations

from pathlib import Path

from _gitrepo import _git
from conftest import load_module_from_path

REPO_ROOT = Path(__file__).resolve().parent.parent

check_docstring_refs = load_module_from_path(
    "check_docstring_refs", REPO_ROOT / "scripts" / "check_docstring_refs.py"
)
check = check_docstring_refs.check
normalize_ref = check_docstring_refs.normalize_ref
resolve_ref = check_docstring_refs.resolve_ref


def _git_init(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestNormalizeRef:
    def test_collapses_internal_whitespace(self):
        assert normalize_ref("lode.cited_answer.\n    _resolve_targets") == (
            "lode.cited_answer._resolve_targets"
        )

    def test_strips_leading_tilde(self):
        assert normalize_ref("~lode.cli._tabular_table") == "lode.cli._tabular_table"

    def test_plain_ref_is_unchanged(self):
        assert normalize_ref("lode.cli._short_date") == "lode.cli._short_date"


class TestResolveRef:
    def test_resolves_a_real_function(self):
        assert resolve_ref("lode.timestamps.parse_stamp") is True

    def test_resolves_a_module_attribute_re_export(self):
        # lode.cli re-exports _short_date from lode.timestamps -- a
        # module-attribute path, not a literal def site in cli/__init__.py.
        assert resolve_ref("lode.cli._short_date") is True

    def test_rejects_a_nonexistent_symbol(self):
        assert resolve_ref("lode.timestamps.this_does_not_exist") is False

    def test_rejects_a_nonexistent_module(self):
        assert resolve_ref("lode.no_such_module.whatever") is False

    def test_resolves_a_dataclass_field_with_no_class_level_default(self):
        # lode.chunking.Passage is a frozen dataclass; char_range has no
        # default, so it never shows up via plain hasattr() on the class.
        assert resolve_ref("lode.chunking.Passage.char_range") is True

    def test_resolves_a_pydantic_model_field_with_no_class_level_default(self):
        # lode.config.Settings is a pydantic BaseSettings; jira_base_url is
        # a declared field, same hasattr blind spot as the dataclass case.
        assert resolve_ref("lode.config.Settings.jira_base_url") is True

    def test_rejects_a_nonexistent_dataclass_field(self):
        assert resolve_ref("lode.chunking.Passage.no_such_field") is False


class TestCheck:
    def test_clean_tree_has_no_unresolved_refs(self, tmp_path):
        _write(
            tmp_path,
            "src/pkg/a.py",
            '"""See :func:`lode.timestamps.parse_stamp`."""\n',
        )
        _git_init(tmp_path)

        unresolved, wrapped = check(tmp_path)
        assert unresolved == []
        assert wrapped == []

    def test_dangling_lode_ref_is_reported(self, tmp_path):
        _write(
            tmp_path,
            "src/pkg/a.py",
            '"""See :func:`lode.timestamps.this_symbol_was_renamed_away`."""\n',
        )
        _git_init(tmp_path)

        unresolved, _wrapped = check(tmp_path)
        assert len(unresolved) == 1
        assert unresolved[0].ref == "lode.timestamps.this_symbol_was_renamed_away"
        assert unresolved[0].line_no == 1

    def test_third_party_ref_is_never_flagged(self, tmp_path):
        _write(
            tmp_path,
            "src/pkg/a.py",
            '"""See :func:`httpx.get` and :class:`pathlib.Path`, neither ours."""\n',
        )
        _git_init(tmp_path)

        unresolved, _wrapped = check(tmp_path)
        assert unresolved == []

    def test_wrapped_but_resolvable_ref_is_warned_not_failed(self, tmp_path):
        _write(
            tmp_path,
            "src/pkg/a.py",
            '"""See :func:`lode.timestamps.\n    parse_stamp` for the real thing."""\n',
        )
        _git_init(tmp_path)

        unresolved, wrapped = check(tmp_path)
        assert unresolved == []  # normalization makes it resolve fine
        assert len(wrapped) == 1
        assert wrapped[0].ref == "lode.timestamps.parse_stamp"

    def test_wrapped_and_dangling_ref_is_both_warned_and_failed(self, tmp_path):
        _write(
            tmp_path,
            "src/pkg/a.py",
            '"""See :func:`lode.timestamps.\n    not_a_real_symbol` (also broken)."""\n',
        )
        _git_init(tmp_path)

        unresolved, wrapped = check(tmp_path)
        assert len(unresolved) == 1
        assert len(wrapped) == 1

    def test_untracked_python_file_is_not_scanned(self, tmp_path):
        _write(tmp_path, "src/pkg/a.py", '"""OK: :func:`lode.timestamps.parse_stamp`."""\n')
        _git_init(tmp_path)
        # Untracked -- must not be swept in, mirroring check_links.py's
        # git-ls-files scoping (scratch/gitignored files stay invisible).
        _write(
            tmp_path,
            "src/pkg/scratch.py",
            '"""Bad: :func:`lode.timestamps.definitely_not_real`."""\n',
        )

        unresolved, _wrapped = check(tmp_path)
        assert unresolved == []

    def test_only_scans_src_and_tests_dirs(self, tmp_path):
        _write(
            tmp_path,
            "scripts/other.py",
            '"""Bad: :func:`lode.timestamps.definitely_not_real`."""\n',
        )
        _git_init(tmp_path)

        unresolved, _wrapped = check(tmp_path)
        assert unresolved == []
