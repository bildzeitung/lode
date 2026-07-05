# Onboarding — install & setup

> Get from a fresh clone to a working `lode` console script and a green test suite.
> For *what lode is* and *why*, start at [design.md](design.md); for the issue-tracking
> workflow, see [AGENTS.md](../AGENTS.md). This doc is purely "how to stand it up."

## Prerequisites

| Tool | Why | Notes |
|---|---|---|
| **Python ≥ 3.10** | Runs lode and its tests. | The repo pins a working version in [`.python-version`](../.python-version); [pyenv](https://github.com/pyenv/pyenv) is the easy way to match it (`pyenv install`). Any CPython ≥ 3.10 on your `PATH` works. |
| **git** | Clone the repo; the dev workflow branches every change into a worktree. | Already required to read this. |
| **Docker** | Validates the Mermaid diagrams in `docs/` (`scripts/validate-mermaid.sh`) via `minlag/mermaid-cli`. | Only needed if you touch a `docs/` diagram. No Node/Chromium toolchain is required on the host — the parser runs in the container. |
| **beads (`bd`)** | Issue tracker — *all* task tracking lives here, not in markdown TODOs. | Only needed if you intend to pick up or file work. Install from the [beads project](https://github.com/gastownhall/beads); then run `bd prime` for the workflow. |

Python is the only hard requirement to build and test lode. Docker and beads are needed
only for the specific contributor tasks noted above.

## Install & setup

All commands run from the repo root.

### 1. Clone

```bash
git clone <your-remote>/lode.git
cd lode
```

If you have pyenv and want to match the pinned interpreter:

```bash
pyenv install --skip-existing   # reads .python-version
```

### 2. Build the virtualenv

`scripts/python-init.sh` creates `./venv` (at the repo root) and installs the project
**editable with its dev extras** (`-e .[dev]`), so `pyproject.toml` stays the single
source of truth for dependencies:

```bash
./scripts/python-init.sh
. ./venv/bin/activate
```

After activation your shell prompt is inside `./venv`. Re-activate
(`. ./venv/bin/activate`) in any new shell; re-run the init script only when
dependencies change.

### 3. Confirm the `lode` console script

The editable install puts the `lode` entry point on your `PATH` (it maps to
`lode.cli:app`):

```bash
lode --help        # lists the subcommands: add, ask, status, jobs, ...
lode version       # prints the installed version
```

Every subcommand is real: `add`, `ask`, `status`, `jobs`, `egress`, `purge`, and
`config`. The eval harness (`lode.eval.harness.score_golden_set`) is a
maintainer/CI integration test, not a shipped end-user command — run it via
`nox -s eval` (see below and `docs/decisions.md`, Shape A).

### 4. Run the dev loop (nox)

[`noxfile.py`](../noxfile.py) defines the two gates every change must pass. Nox runs
**inside the already-built `./venv`** (not an isolated env), so activate the venv first:

```bash
nox -t fix         # ruff format + ruff check --fix (the pre-merge fixer)
nox -s tests       # pytest — the FULL test gate (every test, no marker filter)
```

`nox -s tests` is the suite that must stay green before any merge, and the one
`/land`'s re-gate runs — every test runs here, nothing is ever skipped before trunk.

**Fast inner loop (lode-pql).** The full suite has a handful of tests whose
wall-clock is dominated by a real model load (the un-mocked `FastEmbedCrossEncoder`
reranker, hit by a few end-to-end CLI/skeleton-gate tests) rather than by test
logic; profiling with `pytest --durations` found these are the multi-second
outliers in an otherwise sub-second suite. Those tests are tagged
`@pytest.mark.slow` (registered in `pyproject.toml`) and are the *only* thing a
second, opt-in session excludes:

```bash
nox -s unit        # pytest -m "not slow" — fast code-time inner loop
```

Run `nox -s unit` while iterating; run `nox -s tests` (the full suite) before
every merge — `nox -s unit` is a convenience, never a substitute for the gate.
No test is dropped: every test runs in `nox -s tests` regardless of its marker,
and most also run in `nox -s unit` (only the `slow`-tagged ones are deferred).

A third session, `nox -s eval`, runs the live eval integration test
(`tests/test_eval_live.py`); it is opt-in and credential-gated (it `skip`s
without `ANTHROPIC_API_KEY`), so a bare `nox`, `nox -s tests`, and `nox -s unit`
stay offline and never run it (that test is also `@pytest.mark.slow`, since an
environment where the key *is* set would otherwise run it — live, ~300s — inside
`nox -s tests` too).

A successful `nox -s tests` ends with a line like `=== N passed, M skipped ===` and
`Session tests was successful`. That green run is your "the environment is wired up
correctly" signal.

### 5. (Optional) Mermaid diagram validation

Only if you edit a diagram under `docs/`:

```bash
scripts/update-images.sh      # one-time: pull the mermaid-cli image
scripts/validate-mermaid.sh   # parse every fenced mermaid block, fail on syntax errors
```

## You're set up when

- `. ./venv/bin/activate` then `lode --help` prints the subcommand list, **and**
- `nox -s tests` reports all tests passing.

From here, see [AGENTS.md](../AGENTS.md) (and `bd ready` / `bd prime`) for how work is
claimed and landed, and [design.md](design.md) for the architecture you're building on.
