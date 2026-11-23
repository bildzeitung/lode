"""A mechanical, AST-based gate: no private fence-toggle state machine may
survive outside tests/conftest.py (lode-k5qb) or src/lode/fence_parsing.py
(lode-ee7b).

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
whose body (or else-branch) assigns any value to a plain name (plain, tuple-target
or annotated assignment) -- i.e. private
open/close state, whether a boolean flag or a string holding the open marker
(lode-8ph6). tests/conftest.py's own ``fence_scan`` keeps exactly this state,
which is why it is a sanctioned exemption rather than something the gate could
ever selectively allow inside other files.

What it deliberately does not do. It does not resolve names: a fence marker
hoisted to a module constant (``_FENCE = "```"`` ... ``if
line.startswith(_FENCE)``) puts no literal in the ``if`` test and slips
through, as does the precompiled-regex shape below. For the same reason it
does not ask whether the assigned name is fence state: ``lang = "bash"`` under
a literal marker test is flagged too. That width costs nothing today --
measured at lode-8ph6 over every scanned module, ZERO ``if``s carry a literal
fence marker in their test at all. Plain ``name = value``, tuple-target and annotated assignments
count; augmented assignments (``depth += 1``) and walruses do not, since a bare
counter keeps no open/close state (lode-un73). Subpackages are scanned
recursively. Reading ``fence_scan``'s output and testing a block's own leading marker
without assigning to a name is a legitimate shape (pinned below). This gate
catches the shapes that have actually recurred five times, not an adversary.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = (REPO_ROOT / "tests", REPO_ROOT / "scripts", REPO_ROOT / "src" / "lode")
#: The sanctioned homes, and the only ones: ``fence_scan`` for the test side,
#: ``fence_parsing`` for the importable primitive (lode-ee7b). ``fence_parsing``
#: is listed even though its current regex-based shape is invisible to the
#: detector anyway -- so that the exemption this module's docstring and failure
#: message both CLAIM is mechanically real, and rewriting the one home in the
#: ``startswith`` idiom cannot make it fail its own gate.
EXEMPT = {
    REPO_ROOT / "tests" / "conftest.py",
    REPO_ROOT / "src" / "lode" / "fence_parsing.py",
}


def _contains_fence_literal(node: ast.AST) -> bool:
    """Whether ``node``'s subtree contains a string constant carrying ``` or
    ~~~ -- a plain substring test, which a longer CommonMark fence run
    (````bash) contains too."""
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


def _binds_a_name(target: ast.expr) -> bool:
    """Whether an assignment target is a plain name or a tuple/list of them
    (starred or nested included)."""
    if isinstance(target, ast.Name):
        return True
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_binds_a_name(e) for e in target.elts)
    if isinstance(target, ast.Starred):
        return _binds_a_name(target.value)
    return False


def _assigns_a_plain_name(node: ast.If) -> bool:
    """Whether ``node``'s body or else-branch assigns ANY value to a plain
    name (plain, tuple-target or annotated assignment), at any depth (the assignment may sit inside a nested
    ``if``/``else``). Any value, not just a boolean-flag shape: a machine
    keeping the open marker in a string variable (``fence = marker`` /
    ``fence = ''``) is the same private open/close state (lode-8ph6). Walking
    the whole node is safe: its test is an expression, which cannot hold an
    ``ast.Assign``."""
    return any(
        (isinstance(sub, ast.Assign) and any(_binds_a_name(t) for t in sub.targets))
        or (isinstance(sub, ast.AnnAssign) and _binds_a_name(sub.target))
        for sub in ast.walk(node)
    )


def fence_toggle_findings(tree: ast.AST) -> list[int]:
    """The line number of every ``ast.If`` in ``tree`` whose test is a literal
    fence-marker startswith/membership check AND whose body (or else-branch)
    assigns any value to a plain name -- the private open/close state
    machine this gate exists to forbid."""
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and _is_fence_marker_test(node.test)
        and _assigns_a_plain_name(node)
    ]


def _scan_paths() -> list[Path]:
    return [
        path
        for scan_dir in SCAN_DIRS
        for path in sorted(scan_dir.rglob("*.py"))
        if path not in EXEMPT
    ]


def test_no_private_fence_toggle_state_machine_outside_conftest() -> None:
    """THE GATE: every module under tests/, scripts/, and
    src/lode/ (recursively), except the two sanctioned homes (tests/conftest.py's
    ``fence_scan`` and src/lode/fence_parsing.py's primitives), must be free
    of a private fence-toggle open/close state machine."""
    offenders: list[str] = []
    for path in _scan_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(
            f"{path.relative_to(REPO_ROOT)}:{lineno}"
            for lineno in fence_toggle_findings(tree)
        )
    assert not offenders, (
        "private fence-toggle state machine(s) found outside tests/conftest.py: "
        f"{offenders} -- reuse tests/conftest.py's fence_scan/bash_fence_blocks "
        "(tests/scripts) or src/lode/fence_parsing.py (src/) "
        "instead of hand-rolling new open/close state -- a flag or an "
        "open-marker variable; the gate flags any name assigned under an `if` "
        "that tests a literal fence marker "
        "(lode-ovgs, lode-p4qb, lode-kjei, lode-jm4a, lode-oqqw, lode-k5qb, lode-ee7b, "
        "lode-8ph6)"
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
    lode-k5qb unified it onto tests/conftest.py's shared ``bash_fence_blocks``,
    reproduced verbatim below rather than cited by sha -- the commit that
    carried it is on a merged branch and will not survive gc. If this shape is
    ever reintroduced anywhere in scope, the gate above must go red on it.

    Re-verified during lode-k5qb's review against the REAL file, not just this
    synthetic copy: restoring the pre-change tests/test_sweep_digest_id.py
    reddens the gate above with
    ``tests/test_sweep_digest_id.py:175``."""
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
    """A fence-marker test that only ``continue``s or appends to a list
    (assigns no plain name) keeps no state -- e.g. `fenced_violations`-style
    code that reads `fence_scan`'s output rather than re-deriving fence state
    itself."""
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


def test_scan_dirs_covers_src_lode(tmp_path, monkeypatch) -> None:
    """lode-ee7b AC4: the gate's scan is widened to cover src/*.py, and fails
    on a newly introduced private fence toggle there -- proven by pointing the
    gate's own SCAN_DIRS at a throwaway tree standing in for src/lode/,
    holding the exact pre-lode-jm4a shape from
    ``test_gate_sabotage_catches_the_pre_lode_jm4a_shape`` above, rather than
    mutating the real src/lode/ tree the gate itself is running from.

    The monkeypatched half alone would stay green if ``src/lode`` were dropped
    from ``SCAN_DIRS`` again -- it only proves ``_scan_paths`` globs whatever it
    is handed -- so the widening itself is asserted directly first."""
    assert REPO_ROOT / "src" / "lode" in SCAN_DIRS

    gate = sys.modules[__name__]

    fake_src_lode = tmp_path / "src_lode"
    fake_src_lode.mkdir()
    (fake_src_lode / "offender.py").write_text(
        """
def f(text):
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_block = not in_block
            continue
"""
    )
    monkeypatch.setattr(gate, "SCAN_DIRS", (fake_src_lode,))
    monkeypatch.setattr(gate, "EXEMPT", set())

    offenders: list[str] = []
    for path in gate._scan_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(fence_toggle_findings(tree))
    assert offenders, (
        "widened scan failed to catch a private toggle under a src/-style dir"
    )


def test_gate_catches_a_string_valued_open_fence_variable() -> None:
    """lode-8ph6: the private loop the lode-4615 builder wrote in
    tests/test_instruction_rule_inventory_gate.py (that ticket's review
    replaced it with conftest's ``fence_scan``), reproduced verbatim. It keeps
    the open marker in a string variable, so the assigned value is a Name / str
    constant, not a boolean-flag shape. It must be flagged."""
    src = r"""
def _outside_fences(text: str) -> str:
    kept = []
    fence = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3]
        if fence:
            if marker == fence:
                fence = ""
            continue
        if marker in ("```", "~~~"):
            fence = marker
            continue
        kept.append(line)
    return "\n".join(kept)
"""
    assert fence_toggle_findings(ast.parse(src)), (
        "gate failed to catch a string-valued open-fence variable"
    )


def test_gate_catches_a_tuple_target_open_line() -> None:
    """lode-un73: conftest's ``fence_scan`` opens with a tuple target; a
    private copy mirroring it must be flagged."""
    src = """
def f(lines, marker, n):
    for line in lines:
        if line.startswith("```"):
            fence, opened = marker, n
"""
    assert fence_toggle_findings(ast.parse(src))


def test_gate_catches_an_annotated_assignment() -> None:
    """lode-un73: ``fence: str = marker`` under a literal marker test."""
    src = """
def f(lines, marker):
    for line in lines:
        if line.startswith("```"):
            fence: str = marker
"""
    assert fence_toggle_findings(ast.parse(src))


def test_scan_paths_recurse_into_subpackages(tmp_path, monkeypatch) -> None:
    """lode-un73: a .py file in a subdirectory of a scan dir is scanned."""
    gate = sys.modules[__name__]
    (tmp_path / "sub").mkdir()
    nested = tmp_path / "sub" / "nested.py"
    nested.write_text("x = 1\n")
    monkeypatch.setattr(gate, "SCAN_DIRS", (tmp_path,))
    monkeypatch.setattr(gate, "EXEMPT", set())
    assert nested in gate._scan_paths()
