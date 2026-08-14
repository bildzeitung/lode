# MISTAKES.md

Log of mistakes made while working on this repo (CLAUDE.md, General Directive 9).
Each entry: what happened / root cause / consequence / the rule that prevents a repeat.
Newest first.

## 2026-08-14 — `nox -s tests` hard-reset the developer's live checkout (observed three times)

- **What happened:** While building `lode-s9xe.13` (the `scripts/land-replay.sh` extraction),
  every `nox -s tests` run executed `git reset --hard origin/trunk` **in the developer's own
  live checkout**, resetting the worktree — branch ref included. Observed three times on
  2026-08-14 before the cause was found.
- **Root cause:** `tests/test_gate_lib.py::_run_script()` omitted `cwd`, inheriting pytest's
  cwd: the live checkout. The gate-lib sabotage tests deliberately run consumer scripts with
  the fail-closed guard removed, so `gate_could_not_run` was undefined, each
  `... || gate_could_not_run ...` merely yielded 127, and (no `set -e`) execution continued
  to whatever came next. `land-replay.sh` was the first consumer whose "next" statement is an
  unconditional cwd-resolved `git reset --hard origin/trunk`.
- **Consequence:** The developer's checkout was hard-reset on every test run — uncommitted
  work and branch position destroyed with nothing in reflog to recover the uncommitted half —
  until the test harness was fixed. It also made the branch landing-blocking: merging it
  unfixed would have made every future `nox -s tests` run do the same.
- **Rule that prevents a repeat:** Any test that executes a repo script which can reach a
  cwd-resolved mutating command MUST pass an explicit `cwd` that is not inside any git
  repository (e.g. `tmp_path`). `_run_script()` now makes `cwd` a required parameter, so a
  new call site cannot silently inherit the live checkout. (Fixed on `land/lode-s9xe.13`,
  landed 2026-08-14, merge `08ca1bc`; full account in the `_run_script` docstring,
  `tests/test_gate_lib.py`.)
