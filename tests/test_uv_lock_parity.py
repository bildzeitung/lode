"""TRANSITIONAL drift backstop (lode-s09w.1): uv.lock and requirements.lock coexist
until the lode-s09w retire child lands, and must not disagree on any runtime pin.
Delete this file together with requirements.lock.
"""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_PIN = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", re.MULTILINE)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements_pins(text: str) -> dict[str, str]:
    return {_norm(n): v for n, v in _PIN.findall(text)}


def _uv_versions(text: str) -> dict[str, str | None]:
    return {_norm(p["name"]): p.get("version") for p in tomllib.loads(text)["package"]}


def _mismatches(req: dict[str, str], uv: dict[str, str | None]) -> list[str]:
    return [
        f"{n}: requirements.lock {v} vs uv.lock {uv.get(n)}"
        for n, v in sorted(req.items())
        if uv.get(n) != v
    ]


def test_runtime_pins_agree_between_locks() -> None:
    req = _requirements_pins((ROOT / "requirements.lock").read_text())
    uv = _uv_versions((ROOT / "uv.lock").read_text())
    assert req, "parsed no pins from requirements.lock"
    assert not _mismatches(req, uv)


def test_parity_check_detects_a_differing_pin() -> None:
    req = _requirements_pins("foo==1.0 \\\n    --hash=sha256:abc\nBar_Baz==2.0\n")
    uv = _uv_versions(
        '[[package]]\nname = "foo"\nversion = "1.0"\n'
        '[[package]]\nname = "bar-baz"\nversion = "2.1"\n'
    )
    assert _mismatches(req, uv) == ["bar-baz: requirements.lock 2.0 vs uv.lock 2.1"]


def test_parity_check_detects_a_missing_package() -> None:
    req = _requirements_pins("foo==1.0\n")
    assert _mismatches(req, {}) == ["foo: requirements.lock 1.0 vs uv.lock None"]


@pytest.mark.parametrize("name", ["uv.lock", "requirements.lock"])
def test_both_locks_exist(name: str) -> None:
    assert (ROOT / name).is_file()
