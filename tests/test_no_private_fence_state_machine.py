"""A mechanical, AST-based gate: no private fence-toggle state machine may
survive outside tests/conftest.py (lode-k5qb).

Five separate tickets (lode-ovgs, lode-p4qb, lode-kjei, lode-jm4a, plus the
salvaged lode-oqqw) each ended with a PROSE claim in tests/conftest.py's
fenced-bash header that no private copy of the fence parser survives. That
claim was falsified five times, always by an independently hand-rolled inline
state machine in a module nobody grepped -- the thing that was supposed to
hold the line was a comment, not a check.

This is the generic check lode-jm4a's AC5 note floated but did not build:
"A test asserting 'no private fence state machine survives under tests/'
would end that chain in a way another prose comment demonstrably cannot."

Why AST, not regex over raw text: tests/test_bd_list_limit_gate.py holds many
literal ``` fence markers inside markdown *string fixtures* -- regex over the
source text would trip on every one of them. The AST only ever sees a fence
marker where it actually sits inside an ``if`` test, never inside a string
literal used as data, so that file (this gate's explicit allowlisting model)
passes for free.

The shape being forbidden, concretely: an ``if`` whose test is a literal
fence-marker ``.startswith(...)`` call or ``in``/``not in`` membership check,
whose body (or else-branch) assigns a boolean-flag-shaped value to a plain
name -- i.e. a private open/close toggle. tests/conftest.py's own
``fence_scan`` is exactly this shape, which is why it is the one sanctioned
exemption rather than something the gate could ever selectively allow inside
other files.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = (REPO_ROOT / "tests", REPO_ROOT / "scripts")
EXEMPT = {REPO_ROOT / "tests" / "conftest.py"}


def _contains_fence_literal(node: ast.AST) -> bool:
    """Whether ``node``'s subtree contains a string constant carrying a 3+
    run of backticks or tildes -- CommonMark's own fence-marker shape."""
    return any(
        isinstance(sub, ast.Constant)
        and isinstance(sub.value, str)
        and ("```" in sub.value or "~~~" in sub.value)
        for sub in ast.walk(node)
    )


def _is_fence_marker_test(test: ast.expr) -> bool:
    """Whether ``test`` is a ``.startswith(...)`` call or an ``in``/``not in``
    membership comparison against a literal fence marker -- the two shapes
    every private copy of this parser has used (lode-ovgs, lode-jm4a). A
    fence check built on a precompiled regex object (``_FENCE_RE.match(...)``,
    scripts/check_links.py's own shape) does not match here: the fence-marker
    string lives in the regex literal, not directly in this ``if``'s test."""
    if isinstance(test, ast.Call):
        func = test.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "startswith"
            and any(_contains_fence_literal(a) for a in test.args)
        ):
            return True
    if isinstance(test, ast.Compare) and any(
        isinstance(op, (ast.In, ast.NotIn)) for op in test.ops
    ):
        operands = [test.left, *test.comparators]
        if any(_contains_fence_literal(o) for o in operands):
            return True
    if isinstance(test, ast.BoolOp):
        return any(_is_fence_marker_test(v) for v in test.values)
    return False


def _looks_like_a_boolean_flag(value: ast.expr) -> bool:
    """Whether an assigned value is the shape a fence open/close flag takes:
    a literal bool, a ``not <flag>`` toggle, a ternary, a boolean combinator,
    or a comparison (a membership test deciding the new state) -- every shape
    the five prior private machines used to flip their flag."""
    if isinstance(value, ast.Constant) and isinstance(value.value, bool):
        return True
    if isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.Not):
        return True
    return isinstance(value, (ast.IfExp, ast.BoolOp, ast.Compare))


def _toggles_a_flag(body: list[ast.stmt]) -> bool:
    """Whether any statement in ``body`` assigns a boolean-flag-shaped value
    to a plain name -- searched recursively, since the assignment may sit one
    level deeper (e.g. inside a nested ``if``/``else``)."""
    return any(
        isinstance(sub, ast.Assign)
        and any(isinstance(t, ast.Name) for t in sub.targets)
        and _looks_like_a_boolean_flag(sub.value)
        for stmt in body
        for sub in ast.walk(stmt)
    )


def fence_toggle_findings(tree: ast.AST) -> list[tuple[int, str]]:
    """Every ``ast.If`` in ``tree`` whose test is a literal fence-marker
    startswith/membership check AND whose body (or else-branch) assigns a
    boolean-flag-shaped value -- the private open/close state machine this
    gate exists to forbid. Returns ``(lineno, dumped-test)`` pairs."""
    findings = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and _is_fence_marker_test(node.test)
            and (_toggles_a_flag(node.body) or _toggles_a_flag(node.orelse))
        ):
            findings.append((node.lineno, ast.dump(node.test)))
    return findings


def _scan_paths() -> list[Path]:
    paths: list[Path] = []
    for scan_dir in SCAN_DIRS:
        paths.extend(sorted(scan_dir.glob("*.py")))
    return [p for p in paths if p not in EXEMPT]


def test_no_private_fence_toggle_state_machine_outside_conftest() -> None:
    """THE GATE: every module under tests/*.py and scripts/*.py, except
    tests/conftest.py (the one sanctioned home, ``fence_scan``), must be free
    of a private fence-toggle open/close state machine."""
    offenders: list[str] = []
    for path in _scan_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, _test_src in fence_toggle_findings(tree):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        "private fence-toggle state machine(s) found outside tests/conftest.py: "
        f"{offenders} -- reuse tests/conftest.py's fence_scan/bash_fence_blocks "
        "instead of hand-rolling a new open/close flag "
        "(lode-ovgs, lode-p4qb, lode-kjei, lode-jm4a, lode-oqqw, lode-k5qb)"
    )


def test_gate_ignores_string_fixtures_holding_literal_fence_markers() -> None:
    """tests/test_bd_list_limit_gate.py holds many literal ``` markers inside
    markdown fixtures (module-level string constants) -- none of them sit
    inside an ``ast.If`` test, so the AST gate must never trip on them.
    Modeled directly on that file (lode-k5qb AC2)."""
    src = '''
markdown = """
```bash
bd list --json
```
"""
'''
    tree = ast.parse(src)
    assert fence_toggle_findings(tree) == []


def test_gate_sabotage_catches_the_pre_lode_jm4a_shape() -> None:
    """SABOTAGE PROOF (lode-k5qb AC3): the exact inline state machine that
    lived in tests/test_sweep_digest_id.py's
    ``test_both_sweep_call_sites_use_the_script_not_an_inline_query`` before
    lode-k5qb unified it onto tests/conftest.py's shared ``bash_fence_blocks``
    (see git history at 5344414). If this shape is ever reintroduced anywhere
    in scope, the gate above must go red on it."""
    src = """
def f(text):
    in_block = False
    executed = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_block = False if in_block else stripped in {"```bash", "```sh"}
            continue
        if in_block and not stripped.startswith("#"):
            executed.append(line)
    return executed
"""
    tree = ast.parse(src)
    findings = fence_toggle_findings(tree)
    assert findings, "gate failed to catch the pre-lode-jm4a inline fence machine"


def test_gate_catches_the_pre_lode_ovgs_land_lock_shape() -> None:
    """The other historically-real shape (tests/test_land_lock.py's original
    bug, per tests/conftest.py's own header comment): a plain ``startswith``
    toggle with no membership test in the else-branch at all."""
    src = """
def g(text):
    in_block = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_block = not in_block
            continue
"""
    tree = ast.parse(src)
    assert fence_toggle_findings(tree), (
        "gate failed to catch a plain startswith-toggle fence machine"
    )


def test_gate_ignores_check_links_regex_based_fence_toggle() -> None:
    """scripts/check_links.py's own ``in_fence = not in_fence`` loop matches a
    precompiled regex (``_FENCE_RE.match(line)``), not a literal fence-marker
    string inside the ``if`` test itself -- the gate must not flag it. It is
    the canonical scripts/-side implementation, the same role
    tests/conftest.py plays for tests/*.py, and is intentionally NOT added to
    EXEMPT: the detection rule itself is what keeps it clean, not a carve-out."""
    src = r"""
import re
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

def content_lines(text):
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        yield line
"""
    tree = ast.parse(src)
    assert fence_toggle_findings(tree) == []


def test_gate_ignores_a_bare_if_with_no_flag_assignment() -> None:
    """A fence-marker test that only ``continue``s or appends to a list (not a
    boolean flag) is not a toggle -- e.g. `fenced_violations`-style code that
    reads `fence_scan`'s output rather than re-deriving fence state itself."""
    src = """
def h(blocks):
    out = []
    for block in blocks:
        if block.startswith("```"):
            out.append(block)
    return out
"""
    tree = ast.parse(src)
    assert fence_toggle_findings(tree) == []
