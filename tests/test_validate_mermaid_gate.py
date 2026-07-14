"""Regression test for scripts/validate-mermaid.sh's engine-down handling (lode-9i2p).

Root cause: the old guard was ``command -v docker`` — a PROXY that only proves
some binary named ``docker`` is on PATH. When Docker Desktop's engine is stopped
(Resource Saver mode, or WSL integration switched off for a distro), the Windows
shim left on PATH still satisfies that check, then fails every ``docker run``
with "The command docker could not be found in this WSL 2 distro" — a message
indistinguishable, to a caller, from real per-doc mermaid syntax failures. A
docs-only build (lode-tktc) hit exactly this and handed off with the gate never
having actually parsed the diagram it touched, because the failure looked like
"docker absent" rather than "gate broken."

The fix guards on the INVARIANT instead (``docker info``, i.e. can it reach a
running engine) and exits 2 — distinct from exit 1's "invalid mermaid" — with a
message that names the actual cause and never prints a per-doc ``FAIL`` line.

Both failure shapes are covered here, because ``docker info`` fails identically
whether the engine is stopped or docker was never installed, and blaming
Resource Saver in the second case would just relocate the original bug (a
confident, plausible, false machine-level story):

* engine down — a fake ``docker`` shim on PATH, mirroring the real Windows shim
  (present on PATH, fails at runtime) → exit 2, "docker engine unreachable"
* docker absent — nothing named ``docker`` on PATH at all → exit 2, "no docker
  on PATH", and explicitly *not* the Resource Saver story

Both assert the gate reports "could not run", never "invalid content". Each runs
with a PATH built entirely by the test, so the result never depends on whether
the machine running the suite has a real Docker — in either direction.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "validate-mermaid.sh"


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """A PATH dir with no docker of any kind on it.

    It holds only ``dirname`` — the single external binary the script runs
    before it reaches the docker guard (to resolve its own repo root). That
    makes a PATH of exactly this one dir enough to reach the guard, and lets
    the docker-absent case be simulated by simply not putting a docker on it.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    real_dirname = shutil.which("dirname")
    assert real_dirname, "dirname not found — cannot build a hermetic PATH"
    (bin_dir / "dirname").symlink_to(real_dirname)
    return bin_dir


def _add_broken_docker(bin_dir: Path) -> None:
    """A fake docker that's present on PATH but can't reach an engine — the
    same shape as the real Windows shim once Docker Desktop's engine is
    stopped: `command -v docker` succeeds, every actual call fails."""
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/bin/bash\n"
        'echo "The command docker could not be found in this WSL 2 distro" >&2\n'
        "exit 1\n"
    )
    shim.chmod(0o755)


def _run_gate(path_dir: Path) -> subprocess.CompletedProcess:
    """Run the gate with PATH set to exactly ``path_dir`` — so whether docker
    is reachable is decided entirely by what this test put there, never by
    whether the machine running the suite happens to have a real Docker."""
    return subprocess.run(
        [str(SCRIPT)],
        cwd=REPO_ROOT,
        env={**os.environ, "PATH": str(path_dir)},
        capture_output=True,
        text=True,
        timeout=30,
    )


def _assert_gate_could_not_run(
    result: subprocess.CompletedProcess, *, says: str
) -> None:
    """Assert the shared contract of every gate-could-not-run exit: code 2, so
    it can never be read as exit 1's "invalid mermaid"; the GATE COULD NOT RUN
    banner; and — the whole point — not one per-doc FAIL line, because a broken
    tool must never be mistakable for broken content."""
    assert result.returncode == 2, (
        f"expected exit 2 (gate could not run), got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "GATE COULD NOT RUN" in result.stderr
    assert says in result.stderr
    assert "FAIL" not in result.stdout
    assert "FAIL" not in result.stderr


def test_engine_down_reports_gate_could_not_run_not_content_failures(fake_bin):
    """docker on PATH but unreachable — the lode-9i2p case. The old
    `command -v docker` guard passed here, ran the loop, and reported every doc
    as FAIL."""
    _add_broken_docker(fake_bin)

    _assert_gate_could_not_run(_run_gate(fake_bin), says="docker engine unreachable")


def test_docker_absent_is_gate_could_not_run_but_says_so_honestly(fake_bin):
    """No docker at all. Still exit 2 — a missing tool is not broken content
    either — but the message must NOT blame Docker Desktop's Resource Saver or
    WSL integration. `docker info` fails identically whether the engine is
    stopped, docker was never installed, or the socket denies permission, so
    telling one confident story for all three would just relocate the bug this
    gate exists to kill."""
    result = _run_gate(fake_bin)

    _assert_gate_could_not_run(result, says="no docker on PATH")
    assert "Resource Saver" not in result.stderr
