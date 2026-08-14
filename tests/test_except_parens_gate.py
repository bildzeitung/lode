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

``except*`` IS IN SCOPE. ruff strips ``except* (A, B):`` back to
``except* A, B:`` exactly as it does the plain form (verified against the
pinned ruff), so a PEP 654 exception-group handler is subject to the same
fiat and the same silent regression. The matcher accepts an optional ``*``
after the keyword for that reason -- leaving it out would have left the
gate blind to precisely the failure it exists to catch.

SCOPE / KNOWN BLIND SPOTS (deliberately narrow, matching the shape of the
sibling corpus gates above): this only understands a multi-exception
``except`` clause written on a SINGLE physical line ending in ``:`` on that
same line. A hand-wrapped multi-line ``except (\\n    A,\\n    B,\\n):`` is
not recognized as a violation target. The repo does contain one such site
today -- ``src/lode/tool_dispatch.py``'s four-exception handler -- but it
carries an ``as`` binding, so the formatter never touches its parentheses
and it needs no marker; the blind spot only bites the day a multi-line site
appears WITHOUT an ``as`` binding. Widen the matcher then. Separately,
string literals and comments whose own text starts a line with ``except ``
are not filtered out and would be reported as violations; none do in this
repo today, and a false RED here is loud and self-explaining rather than
silent.

NON-VACUITY / SABOTAGE VERIFICATION (acceptance criterion): three kinds of
check prove this gate is not accidentally vacuous or dumb-see-nothing -- a
real-corpus scan pinned to the known call sites, synthetic source text
driving the matcher through every violation and compliance shape, and a
live ``ruff format`` sabotage-verify that strips a real site's marker and
asserts the parentheses really do vanish (proving the silent-regression
mechanism this gate exists to catch is real, and that the gate then goes
red). Per-test detail lives on the tests themselves, not restated here.

WHY NOT JUST PIN ``[tool.ruff] target-version``: already measured and
rejected -- ``ruff check --target-version py313`` reports 9 errors over
this tree that exist only because PEP 649 makes ``if TYPE_CHECKING:``
imports legal under 3.14. See docs/configuration.md's lode-buay entry
before re-proposing it; ``ruff format`` alone looking clean under py313 is
not the whole measurement.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
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

# Matches a single-physical-line `except`/`except*` clause, capturing the
# clause body (everything between the keyword and the terminating `:`) plus
# the rest of the line (for a trailing `# fmt: skip` comment check). The
# optional `*` is load-bearing -- see the module docstring. Deliberately does
# not attempt to parse across a `:` inside a string literal within the clause
# body -- no real site does that.
_EXCEPT_LINE_RE = re.compile(r"^\s*except\*?\s+(?P<body>[^\n:]+):(?P<rest>.*)$")


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


def _is_wrapped(text: str) -> bool:
    """True if `text` is wholly enclosed in ONE matched pair of parentheses.

    Deliberately not `startswith("(") and endswith(")")`: that is fooled by
    `(A), (B)` -- a genuinely UNparenthesized two-exception list -- which it
    would wave through as compliant, and whose inner slice `A), (B` then
    counts zero top-level commas, so the clause would fall out of the scan
    entirely rather than merely being misjudged.
    """
    if not text.startswith("("):
        return False
    depth = 0
    for i, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return i == len(text) - 1
    return False


@dataclass(frozen=True)
class MultiExceptClause:
    """A multi-exception `except` clause found on one physical line."""

    path: str
    lineno: int
    line: str
    is_parenthesized: bool
    has_as: bool
    has_marker: bool

    @property
    def violation(self) -> str | None:
        """The fiat breach this clause commits, or None if it is compliant."""
        if not self.is_parenthesized:
            return "multi-exception except is not parenthesized"
        if not self.has_as and not self.has_marker:
            return (
                "parenthesized multi-exception except (no `as` binding) is "
                "missing its `# fmt: skip` marker"
            )
        return None

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: {self.violation} -- {self.line.strip()!r}"


def find_multi_except_clauses(path: str, text: str) -> list[MultiExceptClause]:
    """Scan `text` (the raw source of `path`, used for error messages only)
    for multi-exception `except` clauses, compliant or not. Pure text scan --
    see the module docstring for why an AST scan cannot do this job."""
    clauses: list[MultiExceptClause] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        # Cheap substring pre-filter: this scan runs over every tracked *.py
        # in the repo (~110k lines) and all but ~0.1% of them can be rejected
        # without starting the regex engine.
        if "except" not in line:
            continue
        m = _EXCEPT_LINE_RE.match(line)
        if not m:
            continue
        body = m.group("body").strip()

        # Split off a trailing ` as name` binding, if present.
        as_match = re.search(r"\bas\b", body)
        has_as = as_match is not None
        type_part = body[: as_match.start()].strip() if as_match else body

        is_parenthesized = _is_wrapped(type_part)
        inner = type_part[1:-1] if is_parenthesized else type_part
        if _top_level_comma_count(inner) < 1:
            # Single exception type (or a plain `except:`) -- not a
            # multi-exception clause, out of scope for this fiat.
            continue

        clauses.append(
            MultiExceptClause(
                path=path,
                lineno=lineno,
                line=line,
                is_parenthesized=is_parenthesized,
                has_as=has_as,
                has_marker="# fmt: skip" in m.group("rest"),
            )
        )
    return clauses


def find_multi_except_violations(path: str, text: str) -> list[MultiExceptClause]:
    """The clauses from `find_multi_except_clauses` that breach the fiat."""
    return [c for c in find_multi_except_clauses(path, text) if c.violation]


def _tracked_python_files() -> list[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()


def test_no_bare_or_unmarked_multi_exception_in_tracked_source() -> None:
    tracked = _tracked_python_files()
    assert tracked, "git ls-files returned no *.py paths -- this gate would be vacuous"

    all_violations: list[MultiExceptClause] = []
    for rel_path in tracked:
        full = REPO_ROOT / rel_path
        text = full.read_text(encoding="utf-8")
        all_violations.extend(find_multi_except_violations(rel_path, text))

    assert not all_violations, "multi-exception except fiat violated:\n" + "\n".join(
        str(v) for v in all_violations
    )


def test_all_known_sites_are_parenthesized_and_marked() -> None:
    """Non-vacuity pin: the scan must actually FIND and clear the four real
    sites lode-buay parenthesized, not just return an empty (possibly
    vacuous) violation list. Runs the same matcher the gate above runs -- a
    second, hand-rolled copy of the parse here could drift from it and
    quietly stop pinning anything."""
    for rel_path in sorted(KNOWN_SITES):
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert find_multi_except_clauses(rel_path, text), (
            f"{rel_path} is a known multi-exception call site but the scan "
            "found no multi-exception except in it. If the site legitimately "
            "moved or its handler was narrowed to one exception type, update "
            "KNOWN_SITES; otherwise the matcher has stopped seeing it."
        )
        assert not find_multi_except_violations(rel_path, text)


# (except clause, expected violation substring or None if compliant). These
# drive the matcher through every shape it is meant to judge, so a matcher
# that silently stopped recognizing one of them cannot pass.
_MATCHER_CASES = [
    # -- violations --------------------------------------------------------
    ("except ValueError, OSError:", "not parenthesized"),
    ("except (ValueError, OSError):", "fmt: skip"),
    # PEP 654 exception groups are subject to the same fiat: ruff strips
    # `except* (A, B):` back to `except* A, B:` just as it does the plain
    # form, so a matcher that only understood `except ` would be blind to
    # exactly the regression this gate exists to catch.
    ("except* ValueError, OSError:", "not parenthesized"),
    ("except* (ValueError, OSError):", "fmt: skip"),
    # `(A), (B)` is an unparenthesized TWO-exception list that merely looks
    # parenthesized at its first and last character -- see `_is_wrapped`.
    ("except (ValueError), (OSError):", "not parenthesized"),
    # -- compliant ---------------------------------------------------------
    ("except (ValueError, OSError):  # fmt: skip", None),
    ("except* (ValueError, OSError):  # fmt: skip", None),
    # PEP 758 keeps parens mandatory once there's an `as` binding, so ruff
    # never strips these -- no `# fmt: skip` is required or expected.
    ("except (ValueError, OSError) as exc:", None),
    # Single-type clauses are out of scope for the fiat entirely.
    ("except (ValueError):", None),
    ("except OSError:", None),
]


@pytest.mark.parametrize(("except_line", "expected"), _MATCHER_CASES)
def test_matcher_judges_each_clause_shape(
    except_line: str, expected: str | None
) -> None:
    text = f"try:\n    pass\n{except_line}\n    pass\n"
    violations = find_multi_except_violations("synthetic.py", text)
    if expected is None:
        assert violations == []
    else:
        assert len(violations) == 1
        assert expected in violations[0].violation


def test_ruff_format_really_does_strip_an_unmarked_site(tmp_path: Path) -> None:
    """The literal sabotage-verify from the ticket: drop the `# fmt: skip`
    marker on a real known site and run the actual `ruff format` this repo's
    `nox -t fix` invokes -- prove the parens really do get silently stripped
    back to the forbidden bare form, and that the gate then goes red."""
    rel_path = "src/lode/lock.py"
    original = (REPO_ROOT / rel_path).read_text(encoding="utf-8")

    # Locate the marked site with the gate's OWN matcher rather than a
    # hardcoded source literal: this test is about ruff's behavior, and
    # should not go red merely because lock.py changed which exceptions it
    # catches.
    # Keyed on "parenthesized", NOT "marked": if the marker is ever actually
    # dropped from the tree, the corpus scan above is the test that should
    # report it, and this one should still be able to do its own job rather
    # than failing with a confusing second message.
    sites = [
        c for c in find_multi_except_clauses(rel_path, original) if c.is_parenthesized
    ]
    assert sites, f"expected a parenthesized multi-exception site in {rel_path}"
    site = sites[0]
    unmarked_line = site.line.replace("  # fmt: skip", "")
    unmarked = original.replace(site.line, unmarked_line)

    scratch = tmp_path / "lock.py"
    scratch.write_text(unmarked, encoding="utf-8")

    # Before formatting: still parenthesized, gate correctly flags the
    # missing marker (not yet stripped).
    pre_violations = find_multi_except_violations(str(scratch), unmarked)
    assert any("fmt: skip" in v.violation for v in pre_violations)

    # Resolve ruff to the project's OWN venv, and fail loudly if it is not
    # there. An ambient-PATH fallback would let this test validate a ruff
    # that is not the pinned one -- exactly the resolution bug lode-0yfn
    # closed in noxfile.py's `_venv_tool`.
    venv_ruff = REPO_ROOT / "venv" / "bin" / "ruff"
    assert venv_ruff.exists(), (
        f"{venv_ruff} is missing -- run ./scripts/python-init.sh. This test "
        "asserts version-specific ruff formatting behavior, so it must run "
        "the pinned ruff and never whatever copy happens to be on PATH."
    )
    result = subprocess.run(
        [
            str(venv_ruff),
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
    assert unmarked_line not in stripped, (
        "expected ruff format to rewrite the now-unmarked site -- if this "
        "fails, ruff's stripping behavior changed and the fiat may no longer "
        "need this gate"
    )

    # Post-strip, the real gate function flags the now-bare form. This is the
    # assertion that matters: the silent regression is real, and the gate
    # sees it.
    post_violations = find_multi_except_violations(str(scratch), stripped)
    assert any("not parenthesized" in v.violation for v in post_violations), (
        f"gate did not flag the stripped site; scratch now reads:\n{stripped}"
    )
