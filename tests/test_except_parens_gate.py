"""Gate: the parenthesized multi-exception ``except`` fiat
(docs/conventions.md, lode-buay) actually holds, and cannot silently regress
(lode-z1ig).

THE DEFECT THIS CLOSES. lode-buay decided every multi-exception ``except``
must be written parenthesized -- ``except (A, B):``, never the bare
``except A, B:`` form PEP 758 re-legalized in Python 3.14 -- and, because
``ruff format`` (target-version inferred from ``requires-python = ">=3.14"``)
actively STRIPS those parentheses back to the bare form on every run, pins
each such site with a trailing ``# fmt: skip`` to hold the line still. That
mechanism degrades SILENTLY: delete (or reflow away) a ``# fmt: skip`` marker
and the next ``nox -t fix`` quietly rewrites the parenthesized site back to
the forbidden bare form -- no gate turns red, nothing notices. This is the
corpus-scan gate that closes it, in the same spirit as
tests/test_cli_help_corpus_gate.py, tests/test_validate_sha40_call_sites.py,
tests/test_no_hand_derived_skill_md_path.py, and
tests/test_tui_widget_seam_guard.py.

WHY A SOURCE-TEXT SCAN, NOT AN AST SCAN. ``except A, B:`` and
``except (A, B):`` parse to an IDENTICAL ``ast.Tuple`` node in
``ast.ExceptHandler.type`` -- parenthesization is pure surface syntax with no
trace left in the parse tree. An AST walk cannot tell the two forms apart,
so this gate reads the raw file TEXT and looks at the punctuation on the
``except`` line directly. This also means the gate -- not a human -- is the
thing that would notice if a future ruff version ever changed which form it
strips.

SCOPE / KNOWN BLIND SPOTS (deliberately narrow, matching the shape of the
sibling corpus gates above): this only understands a multi-exception
``except`` clause written on a SINGLE physical line ending in ``:`` on that
same line -- every real call site in this repo today is written that way
(the four sites this gate exists to pin), and a hand-wrapped multi-line
``except (\\n    A,\\n    B,\\n):`` would not be recognized as a violation
target by this scan. Widen it the day a real site takes that shape. String
literals and comments that happen to contain the substring ``except`` are
not filtered out; none do in this repo today.

NON-VACUITY / SABOTAGE VERIFICATION (acceptance criterion): three checks
prove this gate is not accidentally vacuous or dumb-see-nothing:

1. ``test_all_known_sites_are_parenthesized_and_marked`` runs the real scan
   over ``git ls-files`` and asserts the exact four known call sites
   (src/lode/confluence.py, src/lode/lock.py, src/lode/worker.py,
   src/lode/tui/screens/_markdown_area.py) are found, parenthesized, and
   marked -- pinning the expected count so a future ``git ls-files`` that
   returned nothing would not pass this test vacuously.
2. ``test_gate_flags_a_missing_fmt_skip_marker`` and
   ``test_gate_flags_the_bare_unparenthesized_form`` drive the matcher
   against hand-built synthetic source text proving each violation shape is
   actually caught.
3. ``test_ruff_format_really_does_strip_an_unmarked_site`` is the literal
   sabotage-verify from the ticket, executed for real: take one of the four
   known sites, copy it to a scratch file with its ``# fmt: skip`` marker
   removed, run the *actual* ``ruff format`` this repo's ``nox -t fix``
   invokes, and assert the parentheses are gone afterward -- proving the
   silent-regression mechanism this gate exists to catch is real, and that
   the gate (re-run against the now-stripped scratch text) goes red.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The four sites lode-buay parenthesized and pinned. Used only to pin the
# non-vacuity count in test_all_known_sites_are_parenthesized_and_marked --
# the scan itself does not consult this list.
KNOWN_SITES = {
    "src/lode/confluence.py",
    "src/lode/lock.py",
    "src/lode/worker.py",
    "src/lode/tui/screens/_markdown_area.py",
}

# Matches a single-physical-line `except <stuff>:` clause, capturing the
# clause body (everything between `except` and the terminating `:`) plus the
# rest of the line (for a trailing `# fmt: skip` comment check). Deliberately
# does not attempt to parse across a `:` inside a string literal within the
# clause body -- no real site does that.
_EXCEPT_LINE_RE = re.compile(
    r"^(?P<indent>\s*)except\s+(?P<body>[^\n:]+):(?P<rest>.*)$"
)


def _top_level_comma_count(text: str) -> int:
    """Count commas in `text` at paren/bracket/brace depth 0."""
    depth = 0
    count = 0
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count


class ExceptViolation:
    def __init__(self, path: str, lineno: int, line: str, reason: str) -> None:
        self.path = path
        self.lineno = lineno
        self.line = line
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"{self.path}:{self.lineno}: {self.reason} -- {self.line.strip()!r}"


def find_multi_except_violations(path: str, text: str) -> list[ExceptViolation]:
    """Scan `text` (the raw source of `path`, for error messages only) for
    multi-exception `except` clauses that are not both parenthesized and
    (absent an `as` binding) `# fmt: skip`-marked. Pure text scan -- see the
    module docstring for why an AST scan cannot do this job."""
    violations: list[ExceptViolation] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _EXCEPT_LINE_RE.match(line)
        if not m:
            continue
        body = m.group("body").strip()
        rest = m.group("rest")

        has_as = False
        type_part = body
        # Split off a trailing ` as name` binding, if present, at top level
        # (not inside parens -- there is none in a bare form anyway).
        as_match = re.search(r"\bas\b", body)
        if as_match:
            has_as = True
            type_part = body[: as_match.start()].strip()

        is_parenthesized = type_part.startswith("(") and type_part.endswith(")")
        inner = type_part[1:-1] if is_parenthesized else type_part

        if _top_level_comma_count(inner) < 1:
            # Single exception type (or a plain `except:`) -- not a
            # multi-exception clause, out of scope for this fiat.
            continue

        if not is_parenthesized:
            violations.append(
                ExceptViolation(
                    path, lineno, line, "multi-exception except is not parenthesized"
                )
            )
            continue

        if not has_as and "# fmt: skip" not in rest:
            violations.append(
                ExceptViolation(
                    path,
                    lineno,
                    line,
                    "parenthesized multi-exception except (no `as` binding) is "
                    "missing its `# fmt: skip` marker",
                )
            )

    return violations


def _tracked_python_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return out


def test_no_bare_or_unmarked_multi_exception_in_tracked_source() -> None:
    tracked = _tracked_python_files()
    assert tracked, "git ls-files returned no *.py paths -- this gate would be vacuous"

    all_violations: list[ExceptViolation] = []
    for rel_path in tracked:
        full = REPO_ROOT / rel_path
        text = full.read_text(encoding="utf-8")
        all_violations.extend(find_multi_except_violations(rel_path, text))

    assert all_violations == [], "multi-exception except fiat violated:\n" + "\n".join(
        repr(v) for v in all_violations
    )


def test_all_known_sites_are_parenthesized_and_marked() -> None:
    """Non-vacuity pin: the scan must actually find and clear the four real
    sites lode-buay parenthesized, not just return an empty (possibly
    vacuous) list."""
    found: set[str] = set()
    for rel_path in KNOWN_SITES:
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            m = _EXCEPT_LINE_RE.match(line)
            if not m:
                continue
            body = m.group("body").strip()
            type_part = body.split(" as ")[0].strip()
            inner = type_part[1:-1] if type_part.startswith("(") else type_part
            if _top_level_comma_count(inner) >= 1:
                found.add(rel_path)
                assert type_part.startswith("(") and type_part.endswith(")"), (
                    f"{rel_path}:{lineno} is a known multi-exception site but is not "
                    "parenthesized"
                )
                if " as " not in body:
                    assert "# fmt: skip" in m.group("rest"), (
                        f"{rel_path}:{lineno} is missing its `# fmt: skip` marker"
                    )

    assert found == KNOWN_SITES, (
        f"expected to find a multi-exception except in every known site; "
        f"missing={KNOWN_SITES - found}"
    )


def test_gate_flags_the_bare_unparenthesized_form() -> None:
    text = "try:\n    pass\nexcept ValueError, OSError:\n    pass\n"
    violations = find_multi_except_violations("synthetic.py", text)
    assert len(violations) == 1
    assert "not parenthesized" in violations[0].reason


def test_gate_flags_a_missing_fmt_skip_marker() -> None:
    text = "try:\n    pass\nexcept (ValueError, OSError):\n    pass\n"
    violations = find_multi_except_violations("synthetic.py", text)
    assert len(violations) == 1
    assert "fmt: skip" in violations[0].reason


def test_gate_accepts_a_correctly_marked_site() -> None:
    text = "try:\n    pass\nexcept (ValueError, OSError):  # fmt: skip\n    pass\n"
    assert find_multi_except_violations("synthetic.py", text) == []


def test_gate_accepts_an_as_binding_with_no_marker() -> None:
    # PEP 758 keeps parens mandatory once there's an `as` binding, so ruff
    # never strips these -- no `# fmt: skip` is required or expected.
    text = "try:\n    pass\nexcept (ValueError, OSError) as exc:\n    pass\n"
    assert find_multi_except_violations("synthetic.py", text) == []


def test_gate_ignores_a_single_exception_type() -> None:
    text = "try:\n    pass\nexcept (ValueError):\n    pass\nexcept OSError:\n    pass\n"
    assert find_multi_except_violations("synthetic.py", text) == []


def test_ruff_format_really_does_strip_an_unmarked_site(tmp_path: Path) -> None:
    """The literal sabotage-verify from the ticket: drop the `# fmt: skip`
    marker on a real known site and run the actual `ruff format` this repo's
    `nox -t fix` invokes -- prove the parens really do get silently stripped
    back to the forbidden bare form, and that the gate then goes red."""
    sample_path = REPO_ROOT / "src/lode/lock.py"
    original = sample_path.read_text(encoding="utf-8")
    unmarked = original.replace(
        "except (ValueError, OSError):  # fmt: skip",
        "except (ValueError, OSError):",
    )
    assert unmarked != original, (
        "expected src/lode/lock.py to contain the known lode-buay site verbatim"
    )

    scratch = tmp_path / "lock.py"
    scratch.write_text(unmarked, encoding="utf-8")

    # Before formatting: still parenthesized, gate correctly flags the
    # missing marker (not yet stripped).
    pre_violations = find_multi_except_violations(str(scratch), unmarked)
    assert any("fmt: skip" in v.reason for v in pre_violations)

    venv_ruff = REPO_ROOT / "venv" / "bin" / "ruff"
    ruff_cmd = (
        [str(venv_ruff)] if venv_ruff.exists() else [sys.executable, "-m", "ruff"]
    )
    result = subprocess.run(
        [
            *ruff_cmd,
            "format",
            "--config",
            str(REPO_ROOT / "pyproject.toml"),
            str(scratch),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    stripped = scratch.read_text(encoding="utf-8")
    assert "except ValueError, OSError:" in stripped, (
        "expected ruff format to strip the parens back to the bare PEP 758 form "
        "once the # fmt: skip marker was removed -- if this fails, ruff's "
        "stripping behavior changed and the fiat may no longer need this gate"
    )
    assert "except (ValueError, OSError)" not in stripped

    # And post-strip, the real gate function flags the now-bare form.
    post_violations = find_multi_except_violations(str(scratch), stripped)
    assert any("not parenthesized" in v.reason for v in post_violations)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__]))
