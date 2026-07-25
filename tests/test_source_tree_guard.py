"""Regression tests for the wrong-source-tree guard (guard 0, lode-jh80).

The hazard, discovered while reviewing lode-7abi: ``noxfile.py`` sets
``default_venv_backend = "none"``, so gates run in whatever venv is already
active. ``scripts/python-init.sh`` always installs the local package editable
(``-e .``, whether via the locked default path or ``--unlocked`` --
lode-g274.1), so that venv's ``lode`` resolves to the ``src`` of whichever
checkout it was built in. Activate the main checkout's venv while sitting in a
worktree and pytest collects **this** checkout's ``tests/`` while importing
**that** one's ``src`` -- silently, in either the false-FAIL or the
false-PASS direction.

These exercise ``tests/conftest.py``'s ``_wrong_source_tree_message`` directly.
The ``pytest_configure`` hook that calls it can't be tested from inside the
run it would abort, which is exactly why the comparison is a pure function.

The false-positive cases matter as much as the true positive here: this guard
sits in front of *every* pytest invocation in the repo, so a guard that fires
wrongly breaks every gate at once.
"""

from pathlib import Path

from conftest import _wrong_source_tree_message


def test_lode_under_the_checkout_passes() -> None:
    """The ordinary case -- an editable install pointing at this checkout's src."""
    root = Path("/repo")
    assert _wrong_source_tree_message("/repo/src/lode/__init__.py", root) is None


def test_lode_from_another_checkout_is_rejected() -> None:
    """The lode-jh80 hazard: main checkout's venv active while cwd is a worktree."""
    root = Path("/repo/.claude/worktrees/agent-x")
    message = _wrong_source_tree_message("/repo/src/lode/__init__.py", root)

    assert message is not None
    # The message must name both paths -- the whole point is that the operator
    # can see *which* tree got imported, not merely that something is wrong.
    assert "/repo/src/lode/__init__.py" in message
    assert str(root) in message
    assert "./scripts/python-init.sh" in message


def test_a_worktree_under_the_main_checkout_is_not_confused_for_it() -> None:
    """A worktree lives *inside* the main checkout, so containment is not enough.

    ``/repo`` is a parent of ``/repo/.claude/worktrees/agent-x``, so the
    mirror-image mistake -- running the main checkout's tests with a
    *worktree's* venv active -- is still under ``/repo`` and a naive "do the
    paths share a prefix?" check waves it through. Anchoring on ``/repo/src``
    rejects it.
    """
    message = _wrong_source_tree_message(
        "/repo/.claude/worktrees/agent-x/src/lode/__init__.py", Path("/repo")
    )
    assert message is not None


def test_a_stale_site_packages_copy_in_this_checkout_is_rejected() -> None:
    """A non-editable copy inside this checkout's own venv is not this tree's source."""
    message = _wrong_source_tree_message(
        "/repo/venv/lib/python3.14/site-packages/lode/__init__.py", Path("/repo")
    )
    assert message is not None


def test_a_symlinked_checkout_is_not_a_false_positive(tmp_path: Path) -> None:
    """Reaching the same tree through a symlink must not trip the guard.

    Both sides are ``resolve()``d, so a checkout reached via a symlinked path
    compares equal to its physical form instead of looking like a foreign tree.
    """
    real = tmp_path / "real-checkout"
    (real / "src" / "lode").mkdir(parents=True)
    init = real / "src" / "lode" / "__init__.py"
    init.touch()

    link = tmp_path / "linked-checkout"
    link.symlink_to(real)

    # lode reached through the symlink, checkout root given physically.
    assert (
        _wrong_source_tree_message(str(link / "src" / "lode" / "__init__.py"), real)
        is None
    )
    # …and the mirror image: physical lode, symlinked root.
    assert _wrong_source_tree_message(str(init), link.resolve()) is None


def test_the_real_running_checkout_passes() -> None:
    """End-to-end sanity: the guard is green for the suite currently running.

    If this ever fails, the venv really is resolving ``lode`` from another
    checkout -- which is the guard doing its job, not a broken test.
    """
    from conftest import _CHECKOUT_ROOT

    import lode

    assert _wrong_source_tree_message(lode.__file__, _CHECKOUT_ROOT) is None
