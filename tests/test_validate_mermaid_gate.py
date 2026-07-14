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
This test simulates the engine-down case with a fake ``docker`` shim on PATH
(mirroring the real Windows-shim behavior: present on PATH, fails at runtime)
and asserts the gate reports "could not run," not "invalid content." It needs
no real Docker installation, so it runs everywhere.
"""

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "validate-mermaid.sh"


def _make_broken_docker_shim(bin_dir: Path) -> None:
    """A fake docker that's present on PATH but can't reach an engine —
    the same shape as the real Windows shim once Docker Desktop's engine
    is stopped: `command -v docker` succeeds, everything else fails."""
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/bin/bash\n"
        'echo "The command docker could not be found in this WSL 2 distro" >&2\n'
        "exit 1\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)


def test_engine_down_reports_gate_could_not_run_not_content_failures(tmp_path):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _make_broken_docker_shim(bin_dir)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    result = subprocess.run(
        [str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    # Distinct exit code: 2 (gate-could-not-run) is never 1 (invalid mermaid).
    assert result.returncode == 2, (
        f"expected exit 2 (gate could not run), got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    # The message must name the real cause, not "docker not found on PATH" —
    # that phrasing is exactly what made the old failure read as a machine
    # fact rather than a broken gate.
    assert "GATE COULD NOT RUN" in result.stderr
    assert "docker engine unreachable" in result.stderr
    # No per-doc FAIL lines — a stopped engine must never look like broken
    # mermaid content.
    assert "FAIL" not in result.stdout
    assert "FAIL" not in result.stderr
