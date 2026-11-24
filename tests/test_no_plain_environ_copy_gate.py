"""Gate: no test builds a plain full copy of ``os.environ`` (lode-jtozo).

A plain ``dict`` of the whole environment prints its first variables, values
included, in a pytest failure whenever a subprocess call it feeds cannot start
its command. ``tests/_gitrepo.py``'s ``os_env()`` returns a mapping whose repr
shows only a variable count, and lode-ovcvs / lode-fe2xj converted every site.
This scan keeps the conversion from regrowing: it fails on the shapes
``dict(os.environ)``, ``os.environ.copy()``, ``**os.environ``,
``os.environ.items()`` and ``os.environ | ...`` in any tracked ``tests/*.py``
except ``tests/_gitrepo.py``, which builds the quiet mapping.

AST, not text: the rule's own name appears in docstrings and comments
(``tests/_nested_pytest.py``), which a text grep would flag.

Blind spots, deliberately left on prose: a dict rebuilt from an ``os_env()``
result held in a variable (``{**env}``, ``dict(env, K=v)``, ``env.copy()``),
and ``from os import environ`` spellings. ``pytest --showlocals`` prints the
environment whatever its type.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conftest import tracked_paths

REPO_ROOT = Path(__file__).resolve().parent.parent
_EXEMPT = {"tests/_gitrepo.py"}


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def find_environ_copies(text: str) -> list[tuple[int, str]]:
    """(lineno, shape) for every plain full-copy use of ``os.environ``."""
    tree = ast.parse(text)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Name)
                and func.id == "dict"
                and node.args
                and _is_os_environ(node.args[0])
            ):
                found.append((node.lineno, "dict(os.environ)"))
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"copy", "items"}
                and _is_os_environ(func.value)
            ):
                found.append((node.lineno, f"os.environ.{func.attr}()"))
            found.extend(
                (kw.value.lineno, "**os.environ")
                for kw in node.keywords
                if kw.arg is None and _is_os_environ(kw.value)
            )
        elif isinstance(node, ast.Dict):
            found.extend(
                (value.lineno, "{**os.environ}")
                for key, value in zip(node.keys, node.values, strict=True)
                if key is None and _is_os_environ(value)
            )
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            if _is_os_environ(node.left) or _is_os_environ(node.right):
                found.append((node.lineno, "os.environ | ..."))
    return found


def test_no_plain_environ_copy_under_tests() -> None:
    tracked = [p for p in tracked_paths("tests/*.py") if p not in _EXEMPT]
    assert tracked, "no tracked tests/*.py paths -- this gate would be vacuous"

    hits = [
        f"{rel}:{lineno}: {shape}"
        for rel in tracked
        for lineno, shape in find_environ_copies(
            (REPO_ROOT / rel).read_text(encoding="utf-8")
        )
    ]
    assert not hits, (
        "plain full copy of os.environ under tests/ -- use os_env() from "
        "tests/_gitrepo.py (its repr shows a variable count only):\n"
        + "\n".join(hits)
    )


@pytest.mark.parametrize(
    ("line", "shape"),
    [
        ("e = dict(os.environ)", "dict(os.environ)"),
        ("e = dict(os.environ, A='1')", "dict(os.environ)"),
        ("e = os.environ.copy()", "os.environ.copy()"),
        ("e = {**os.environ, 'A': '1'}", "{**os.environ}"),
        ("f(**os.environ)", "**os.environ"),
        ("e = [k for k, v in os.environ.items()]", "os.environ.items()"),
        ("e = os.environ | {'A': '1'}", "os.environ | ..."),
    ],
)
def test_matcher_flags_each_shape(line: str, shape: str) -> None:
    assert [s for _, s in find_environ_copies(f"import os\n{line}\n")] == [shape]


@pytest.mark.parametrize(
    "line",
    [
        "e = os.environ.get('A')",
        "e = os.environ['A']",
        "e = 'A' in os.environ",
        "e = {**other, 'A': '1'}",
        "e = dict(other)",
        "'''dict(os.environ) in a docstring'''",
    ],
)
def test_matcher_passes_non_copies(line: str) -> None:
    assert find_environ_copies(f"import os\n{line}\n") == []
