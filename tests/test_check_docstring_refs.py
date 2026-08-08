"""Tests for scripts/check_docstring_refs.py, the docstring-role gate (lode-8oeu).

Exercises the units the gate is built from -- ``normalize_ref`` (whitespace
collapsing + ``~`` stripping), ``resolve_ref`` (module-attribute resolution,
including the dataclass/pydantic-field special case), and ``check`` (the
tracked-file walk) -- plus one end-to-end regression against a synthetic
fixture tree so a real dangling ref, a real re-export path, and a real
line-wrapped-but-correct ref are all covered together.

Fixture role text is assembled via ``_role`` rather than written as a literal
``:func:`...``` substring anywhere in THIS file -- this file is itself under
``tests/`` and therefore in-scope for ``nox -s docstringcheck``'s own scan of
the real repo; a literal fixture role would self-match as a (bogus)
unresolved or line-wrapped reference in this file's own source text.
"""

from __future__ import annotations

from pathlib import Path

from _gitrepo import _git
from conftest import load_module_from_path
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parent.parent

check_docstring_refs = load_module_from_path(
    "check_docstring_refs", REPO_ROOT / "scripts" / "check_docstring_refs.py"
)
check = check_docstring_refs.check
normalize_ref = check_docstring_refs.normalize_ref
resolve_ref = check_docstring_refs.resolve_ref
app = check_docstring_refs.app


def _role(kind: str, target: str) -> str:
    """Assemble a ``:kind:`target``` role string at RUNTIME so the literal
    substring never appears in this file's own source (see module
    docstring)."""
    colon = ":"
    backtick = "`"
    return colon + kind + colon + backtick + target + backtick


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
        role = _role("func", "lode.timestamps.parse_stamp")
        _write(tmp_path, "src/pkg/a.py", f'"""See {role}."""\n')
        _git_init(tmp_path)

        unresolved, wrapped = check(tmp_path)
        assert unresolved == []
        assert wrapped == []

    def test_dangling_lode_ref_is_reported(self, tmp_path):
        role = _role("func", "lode.timestamps.this_symbol_was_renamed_away")
        _write(tmp_path, "src/pkg/a.py", f'"""See {role}."""\n')
        _git_init(tmp_path)

        unresolved, _wrapped = check(tmp_path)
        assert len(unresolved) == 1
        assert unresolved[0].ref == "lode.timestamps.this_symbol_was_renamed_away"
        assert unresolved[0].line_no == 1

    def test_third_party_ref_is_never_flagged(self, tmp_path):
        func_role = _role("func", "httpx.get")
        class_role = _role("class", "pathlib.Path")
        _write(
            tmp_path,
            "src/pkg/a.py",
            f'"""See {func_role} and {class_role}, neither ours."""\n',
        )
        _git_init(tmp_path)

        unresolved, _wrapped = check(tmp_path)
        assert unresolved == []

    def test_wrapped_but_resolvable_ref_is_reported_separately(self, tmp_path):
        # `check()` only classifies -- whether a wrapped ref hard-fails is
        # main()'s call (see TestMain below, lode-hg49).
        role = _role("func", "lode.timestamps.\n    parse_stamp")
        _write(tmp_path, "src/pkg/a.py", f'"""See {role} for the real thing."""\n')
        _git_init(tmp_path)

        unresolved, wrapped = check(tmp_path)
        assert unresolved == []  # normalization makes it resolve fine
        assert len(wrapped) == 1
        assert wrapped[0].ref == "lode.timestamps.parse_stamp"

    def test_wrapped_and_dangling_ref_are_both_reported(self, tmp_path):
        role = _role("func", "lode.timestamps.\n    not_a_real_symbol")
        _write(tmp_path, "src/pkg/a.py", f'"""See {role} (also broken)."""\n')
        _git_init(tmp_path)

        unresolved, wrapped = check(tmp_path)
        assert len(unresolved) == 1
        assert len(wrapped) == 1

    def test_untracked_python_file_is_not_scanned(self, tmp_path):
        ok_role = _role("func", "lode.timestamps.parse_stamp")
        _write(tmp_path, "src/pkg/a.py", f'"""OK: {ok_role}."""\n')
        _git_init(tmp_path)
        # Untracked -- must not be swept in, mirroring check_links.py's
        # git-ls-files scoping (scratch/gitignored files stay invisible).
        bad_role = _role("func", "lode.timestamps.definitely_not_real")
        _write(tmp_path, "src/pkg/scratch.py", f'"""Bad: {bad_role}."""\n')

        unresolved, _wrapped = check(tmp_path)
        assert unresolved == []

    def test_every_symbol_naming_role_is_gated(self, tmp_path):
        # Not just the four roles lode-8oeu's text enumerated. :mod: and
        # :attr: carry the largest and second-largest bodies of lode.* refs
        # in the repo; a role the regex misses is a SILENT pass, which is
        # the failure mode that makes a green gate a lie rather than a
        # nuisance. Pin all eight so a future narrowing fails loudly.
        roles = ("func", "class", "data", "meth", "attr", "mod", "exc", "obj")
        body = "\n".join(
            f"# {_role(kind, f'lode.timestamps.not_real_{kind}')}" for kind in roles
        )
        _write(tmp_path, "src/pkg/a.py", body + "\n")
        _git_init(tmp_path)

        unresolved, _wrapped = check(tmp_path)
        assert {ref.ref for ref in unresolved} == {
            f"lode.timestamps.not_real_{kind}" for kind in roles
        }

    def test_only_scans_src_and_tests_dirs(self, tmp_path):
        bad_role = _role("func", "lode.timestamps.definitely_not_real")
        _write(tmp_path, "scripts/other.py", f'"""Bad: {bad_role}."""\n')
        _git_init(tmp_path)

        unresolved, _wrapped = check(tmp_path)
        assert unresolved == []


class TestMain:
    """End-to-end CLI coverage for the lode-hg49 disposition change: a
    wrapped-but-resolvable ref now HARD-FAILS main(), not just gets
    reported by check() -- exercised via CliRunner against ``--root``
    rather than importing/calling main() directly, so the exit code is the
    real one Typer produces."""

    runner = CliRunner()

    def test_clean_tree_exits_zero(self, tmp_path):
        role = _role("func", "lode.timestamps.parse_stamp")
        _write(tmp_path, "src/pkg/a.py", f'"""See {role}."""\n')
        _git_init(tmp_path)

        result = self.runner.invoke(app, ["--root", str(tmp_path)])
        assert result.exit_code == 0
        assert "resolves and none is line-wrapped" in result.stdout

    def test_wrapped_but_resolvable_ref_hard_fails(self, tmp_path):
        role = _role("func", "lode.timestamps.\n    parse_stamp")
        _write(tmp_path, "src/pkg/a.py", f'"""See {role} for the real thing."""\n')
        _git_init(tmp_path)

        result = self.runner.invoke(app, ["--root", str(tmp_path)])
        assert result.exit_code == 1
        assert "line-wrapped reference" in result.output
        assert "1 line-wrapped reference(s) found" in result.output
