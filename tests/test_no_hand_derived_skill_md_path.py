"""A mechanical gate: no hand-derived ``.claude/skills/*/SKILL.md`` (or
``.claude/agents/*.md``) Path expression may survive outside tests/conftest.py
(lode-1el8).

lode-b8jc consolidated five hand-derived copies of the sweep SKILL.md path
(``REPO_ROOT / ".claude" / "skills" / "sweep" / "SKILL.md"``) into
tests/conftest.py's ``SWEEP_SKILL``, next to ``LAND_SKILL`` (itself
consolidated from four copies by lode-va47). The duplication had already
RE-FORKED once during lode-b8jc's own lifetime: tests/test_sweep_source_query_failure.py
landed on trunk (236a17c) carrying a fresh hand-derived copy AFTER the first
consolidation attempt (lode-vqlx) was written -- the ticket predicted this
exact re-fork and it happened anyway. Prose in conftest.py asking readers to
import rather than re-derive is what was holding the line, and it had already
failed once. This is the mechanical check that replaces the prose.

Modeled on tests/test_no_private_fence_state_machine.py's shape (lode-k5qb):
an AST scan of every tests/*.py module except conftest.py itself, which is
the one sanctioned home for these constants.

What this catches, concretely: a chain of ``/`` (``ast.BinOp`` with
``ast.Div``) whose string-literal operands include the consecutive pair
``".claude"``, ``"skills"`` (in that order, anywhere in the chain) and whose
LAST segment is the literal ``"SKILL.md"`` -- the exact shape every one of
the nine removed duplicates took (``REPO_ROOT / ".claude" / "skills" /
"sweep" / "SKILL.md"``, ``LAND_SKILL``'s own pre-consolidation copies, ...).
The ``.claude/agents/`` sibling shape (``".claude"``, ``"agents"`` followed
by a literal, non-glob ``"*.md"``-shaped filename as the last segment) is
caught the same way, per the ticket's "or .claude/agents/" scope.

Deliberately narrow (per the ticket's stated preference over a broad "any
repeated constant" detection): this only fires on the literal
``'.claude'``/``'skills'`` (or ``'agents'``) segment-sequence terminating in
a concrete filename. It does NOT resolve names, so a chain built on a
module-level directory constant (``SKILLS_DIR / "code" / "SKILL.md"``, as
tests/test_skill_bash_state.py does against its own ``SKILLS_DIR = CLAUDE_DIR
/ "skills"``) is not flagged -- that shape does not repeat the ``.claude`` /
``skills`` literal pair itself, and is not one of the shapes that has
actually re-forked. Two deliberately generic multi-document scans stay green
for the same reason: tests/test_skill_bash_state.py's
``SKILLS_DIR.glob("*/SKILL.md")`` never chains a *literal* ``"SKILL.md"``
segment (the glob pattern is a separate string argument, not a Path
segment), and tests/test_bd_list_limit_gate.py's path-keyed table holds
``".claude/skills/land/SKILL.md"``-shaped strings as dict keys, not as
chained Path expressions at all.

KNOWN, DELIBERATE BLIND SPOTS (narrowness is the ticket's choice; these are
stated limits, not accidents). None of them is a shape that has actually
re-forked, and each would be caught by the same review that would catch a
freshly invented evasion:

* a single joined string segment -- ``ROOT / ".claude/skills/land/SKILL.md"``
  -- or a whole-path ``Path(".claude/skills/land/SKILL.md")``;
* an f-string or any non-literal segment (``_flatten_div_chain`` bails);
* ``.joinpath(".claude", "skills", ...)`` -- a Call, not a ``/`` chain;
* a chain rooted in a name that already holds ``.claude`` (``CLAUDE_DIR /
  "skills" / "land" / "SKILL.md"``), since this gate does not resolve names.
  That last one is load-bearing rather than merely tolerated: it is what
  keeps tests/test_skill_bash_state.py's own ``SKILLS_DIR`` green.

SCOPE is tests/*.py only -- deliberately narrower than
tests/test_no_private_fence_state_machine.py, whose ``SCAN_DIRS`` also covers
scripts/. The duplication this gate exists to stop is a *test-fixture* one:
all nine consolidated copies (lode-va47, lode-b8jc) were test modules pinning
a skill doc to assert against, and tests/conftest.py -- the constant's home,
and the remedy the failure message points at -- is importable only from
tests/. A script hand-deriving a SKILL.md path would have no conftest constant
to import, so widening the scan would produce a finding with no fix. Widen it
if a scripts/ offender ever appears, and give it a home first.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIR = REPO_ROOT / "tests"
CONFTEST_PATH = SCAN_DIR / "conftest.py"
EXEMPT = {CONFTEST_PATH}


def _flatten_div_chain(node: ast.expr) -> list[str] | None:
    """Flatten a chain of ``x / "a" / "b" / ...`` (``/`` is left-associative,
    so the outermost BinOp is the last segment) into the
    ordered list of its string-literal segments, base excluded. Returns
    ``None`` if any segment along the way is not a plain string constant
    (e.g. a variable, an f-string, a glob pattern built dynamically) -- such
    a chain does not repeat a literal and is out of scope for this gate."""
    segments: list[str] = []
    current = node
    while isinstance(current, ast.BinOp) and isinstance(current.op, ast.Div):
        right = current.right
        if not (isinstance(right, ast.Constant) and isinstance(right.value, str)):
            return None
        segments.append(right.value)
        current = current.left
    segments.reverse()
    return segments


def _is_hand_derived_skill_or_agent_path(segments: list[str]) -> bool:
    """Whether a flattened segment list is the forbidden shape: a literal
    ``.claude`` immediately followed by ``skills`` (chain ending in the
    literal filename ``SKILL.md``), or ``.claude`` immediately followed by
    ``agents`` (chain ending in a literal, non-glob ``*.md``-shaped
    filename)."""
    for i in range(len(segments) - 1):
        if segments[i] != ".claude":
            continue
        if segments[i + 1] == "skills" and segments[-1] == "SKILL.md":
            return True
        if (
            segments[i + 1] == "agents"
            and segments[-1].endswith(".md")
            and "*" not in segments[-1]
        ):
            return True
    return False


def hand_derived_findings(tree: ast.AST) -> list[tuple[int, list[str]]]:
    """Every Path-division chain in ``tree`` that hand-derives a
    ``.claude/skills/*/SKILL.md`` or ``.claude/agents/*.md`` path instead of
    importing the shared conftest.py constant, as ``(lineno, segments)``.

    This is the ONE implementation of the detection: the gate below calls it,
    and so do the sabotage-proof tests -- so the AC3 non-vacuity evidence
    applies to the code the gate actually runs, not to a parallel copy.

    Inner BinOps of a chain are walked too (``ast.walk`` visits every node)
    but cannot match: their flattened segments stop short of the filename, so
    the ``"SKILL.md"``/``*.md`` terminal condition fails. No de-duplication is
    therefore needed."""
    findings: list[tuple[int, list[str]]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
            continue
        segments = _flatten_div_chain(node)
        if segments is None:
            continue
        if _is_hand_derived_skill_or_agent_path(segments):
            findings.append((node.lineno, segments))
    return findings


def _suggested_constant(segments: list[str]) -> str:
    """The conftest.py constant name this path would use (``LAND_SKILL``,
    ``SWEEP_SKILL``, ...), derived the same way those two were named -- the
    segment right before the final ``SKILL.md``, upper-cased. The
    ``.claude/agents/`` sibling shape is named after its filename stem
    (``coding.md`` -> ``CODING_AGENT``)."""
    if segments[-1] == "SKILL.md" and len(segments) >= 2:
        return f"{segments[-2].upper().replace('-', '_')}_SKILL"
    stem = segments[-1].removesuffix(".md").upper().replace("-", "_")
    return f"{stem}_AGENT"


def _scan_paths() -> list[Path]:
    return [path for path in sorted(SCAN_DIR.glob("*.py")) if path not in EXEMPT]


def test_no_hand_derived_skill_md_path_outside_conftest() -> None:
    """THE GATE: every module under tests/*.py, except tests/conftest.py (the
    one sanctioned home for these constants -- LAND_SKILL, SWEEP_SKILL, ...),
    must be free of a hand-derived ``.claude/skills/*/SKILL.md`` or
    ``.claude/agents/*.md`` Path expression."""
    conftest_text = CONFTEST_PATH.read_text(encoding="utf-8")
    offenders: list[str] = []
    for path in _scan_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, segments in hand_derived_findings(tree):
            suggestion = _suggested_constant(segments)
            if f"{suggestion} =" in conftest_text:
                howto = f"import {suggestion} from tests/conftest.py instead of re-deriving it"
            else:
                howto = (
                    f"tests/conftest.py has no {suggestion} yet -- add one there "
                    "(following LAND_SKILL/SWEEP_SKILL's pattern) and import it"
                )
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno} -- {howto}")
    assert not offenders, (
        "hand-derived SKILL.md/agent-md Path expression(s) found outside "
        f"tests/conftest.py: {offenders} (lode-1el8, lode-b8jc, lode-va47)"
    )


def test_gate_ignores_a_directory_constant_reused_for_a_single_file() -> None:
    """tests/test_skill_bash_state.py's own
    ``(SKILLS_DIR / "code" / "SKILL.md")`` -- built on a module-level
    ``SKILLS_DIR`` constant, not on the literal ``.claude`` / ``skills``
    pair -- must not trip the gate. This gate does not resolve names."""
    src = """
SKILLS_DIR = CLAUDE_DIR / "skills"

def f():
    return (SKILLS_DIR / "code" / "SKILL.md").read_text()
"""
    tree = ast.parse(src)
    assert hand_derived_findings(tree) == []


def test_gate_ignores_a_directory_level_chain_used_for_a_glob() -> None:
    """tests/test_skill_bash_state.py's own module-level
    ``SKILLS_DIR = CLAUDE_DIR / "skills"`` / ``AGENTS_DIR = CLAUDE_DIR /
    "agents"`` (feeding ``SKILLS_DIR.glob("*/SKILL.md")`` elsewhere) never
    chains a literal ``"SKILL.md"`` segment itself -- the glob pattern is a
    separate string argument, not a Path segment -- so it must stay green."""
    src = """
CLAUDE_DIR = REPO_ROOT / ".claude"
SKILLS_DIR = CLAUDE_DIR / "skills"
AGENTS_DIR = CLAUDE_DIR / "agents"

def all_docs():
    return sorted(SKILLS_DIR.glob("*/SKILL.md")) + sorted(AGENTS_DIR.glob("*.md"))
"""
    tree = ast.parse(src)
    assert hand_derived_findings(tree) == []


def test_gate_ignores_the_bd_list_limit_gate_path_keyed_table() -> None:
    """tests/test_bd_list_limit_gate.py's ``EXPECTED`` table keys on plain
    strings like ``".claude/skills/land/SKILL.md"`` -- a single string
    constant, never a chained Path division -- so it must stay green."""
    src = """
EXPECTED = {
    (".claude/skills/land/SKILL.md", "bd list --label needs-rebase"): (
        "some finding text"
    ),
}
"""
    tree = ast.parse(src)
    assert hand_derived_findings(tree) == []


def test_gate_catches_the_pre_lode_b8jc_sweep_skill_shape() -> None:
    """SABOTAGE PROOF (lode-1el8 AC3): the exact hand-derived shape that
    lived, verbatim, in tests/test_sweep_digest_id.py before lode-b8jc
    consolidated it onto tests/conftest.py's shared ``SWEEP_SKILL`` --
    ``REPO_ROOT / ".claude" / "skills" / "sweep" / "SKILL.md"``. If this
    shape is ever reintroduced anywhere in scope, the gate above must go red
    on it.

    Re-verified during this ticket's own build against the REAL file: adding
    this exact line back into tests/test_sweep_digest_id.py reddens the gate
    above naming that file; removing it again turns the gate green."""
    src = """
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SWEEP_SKILL = REPO_ROOT / ".claude" / "skills" / "sweep" / "SKILL.md"
"""
    tree = ast.parse(src)
    findings = hand_derived_findings(tree)
    assert findings, (
        "gate failed to catch the pre-lode-b8jc hand-derived SWEEP_SKILL shape"
    )
    # AC2: the failure message must name the constant to import.
    assert [_suggested_constant(segments) for _, segments in findings] == [
        "SWEEP_SKILL"
    ]


def test_gate_catches_the_land_skill_shape() -> None:
    """The other historically-real shape (four copies consolidated by
    lode-va47): ``_CHECKOUT_ROOT / ".claude" / "skills" / "land" /
    "SKILL.md"``."""
    src = """
LAND_SKILL = _CHECKOUT_ROOT / ".claude" / "skills" / "land" / "SKILL.md"
"""
    tree = ast.parse(src)
    findings = hand_derived_findings(tree)
    assert findings, "gate failed to catch the pre-lode-va47 LAND_SKILL shape"
    assert [_suggested_constant(segments) for _, segments in findings] == ["LAND_SKILL"]


def test_gate_catches_the_agents_equivalent_shape() -> None:
    """The ``.claude/agents/*.md`` sibling shape the ticket also asks for:
    a hand-derived single-agent-file Path chain."""
    src = """
CODING_AGENT = REPO_ROOT / ".claude" / "agents" / "coding.md"
"""
    tree = ast.parse(src)
    findings = hand_derived_findings(tree)
    assert findings, "gate failed to catch a hand-derived single-agent-file path"
    # No CODING_AGENT exists in conftest.py today, so the gate's message takes
    # its "add one there" branch -- which still has to name something concrete.
    assert [_suggested_constant(segments) for _, segments in findings] == [
        "CODING_AGENT"
    ]


def test_gate_ignores_an_agents_directory_level_chain() -> None:
    """``REPO_ROOT / ".claude" / "agents"`` on its own (no trailing filename
    segment) is a legitimate directory constant, not a hand-derived
    single-file path -- must not trip the gate."""
    src = """
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
"""
    tree = ast.parse(src)
    assert hand_derived_findings(tree) == []
