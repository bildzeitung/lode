# Onboarding — install & setup

> Get from a fresh clone to a working `lode` console script and a green test suite.
> For *what lode is* and *why*, start at [design.md](design.md); for the issue-tracking
> workflow, see [AGENTS.md](../AGENTS.md). This doc is purely "how to stand it up."

## Prerequisites

| Tool | Why | Notes |
|---|---|---|
| **Python ≥ 3.14** | Runs lode and its tests. | The repo pins a working version in [`.python-version`](../.python-version); [pyenv](https://github.com/pyenv/pyenv) is the easy way to match it (`pyenv install`). Any CPython ≥ 3.14 on your `PATH` works. |
| **git** | Clone the repo; the dev workflow branches every change into a worktree. | Already required to read this. |
| **Docker** | Validates the Mermaid diagrams in `docs/` (`scripts/validate-mermaid.sh`) via `minlag/mermaid-cli`. | Only needed if you touch a `docs/` diagram. No Node/Chromium toolchain is required on the host — the parser runs in the container. |
| **beads (`bd`)** | Issue tracker — *all* task tracking lives here, not in markdown TODOs. | **Required for every developer.** The issue database does not arrive with `git clone`; you restore it in [§3](#3-restore-the-issue-database-beads) below. Install from the [beads project](https://github.com/gastownhall/beads). It bundles Dolt (embedded mode) — no separate Dolt install. |

Python is the only hard requirement to *build and test* lode. Docker is needed only if
you touch a `docs/` diagram. beads is needed by anyone who claims, files, or closes
work — which is everyone contributing.

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

### 3. Restore the issue database (beads)

`git clone` gives you **no issues**. The Dolt database that backs `bd` lives in
`.beads/embeddeddolt/`, which is gitignored; the issue data travels on the *same* git
remote but under a separate ref, `refs/dolt/data`. `.beads/issues.jsonl` is a **passive
export** — a read-only snapshot, never the wire. On a fresh clone `bd ready` fails with
`no beads database found` until you do this.

Clone into a directory named `lode` (the issue prefix is derived from the checkout
directory), then:

```bash
bd init          # clones the Dolt DB from the remote configured in .beads/config.yaml
```

`sync.remote` is already committed in [`.beads/config.yaml`](../.beads/config.yaml), so
plain `bd init` finds it and prints `✓ bd initialized from git remote!`. It also sets
`core.hooksPath` to `.beads/hooks` — a *local* git config that does not travel with a
clone, which is the other reason this step is not optional.

> ⚠️ **`bd init` makes a git commit, and that commit is not welcome here.** It rewrites
> `CLAUDE.md`, `AGENTS.md`, `.claude/settings.json`, and `.gitignore` with its own
> boilerplate and adds `.codex/` — clobbering this repo's committed project config
> (including the `autoMode` consent rules). Drop it immediately:
>
> ```bash
> git log --oneline -1          # expect: "bd init: initialize beads issue tracking"
> git reset --hard origin/trunk
> ```
>
> Nothing is lost: the restored database is gitignored, so it survives the reset, and so
> does `core.hooksPath` (it lives in `.git/config`).

Verify all four:

```bash
bd ready                        # lists open issues — the DB is really here
bd memories                     # persistent project memories came across too
git config core.hooksPath       # → <repo>/.beads/hooks
grep '^import.auto' .beads/config.yaml   # → import.auto: false  (must stay false)
```

Then run `bd prime` once for the full command reference and the session-close protocol.

#### The rules that keep the database intact

Dolt is authoritative; the JSONL is a footnote. Violating these has silently reverted
closed issues in this repo before (`lode-6ra`):

- **Sync only via `bd dolt push` / `bd dolt pull`.** Run `bd dolt push` after any `bd`
  write, as part of the same session-close as `git push` — otherwise your issue changes
  never leave the machine.
- **Never `bd import` the JSONL**, and never hand-edit `.beads/issues.jsonl`. Treat it as
  a read-only export.
- **Keep `import.auto: false`.** beads defaults it to `true`, which makes the
  `post-checkout`/`post-merge` hooks re-import a *stale* committed JSONL after a pull or
  merge — replaying pre-`bd close` state and reverting closes. It is committed as `false`
  in `.beads/config.yaml`; if a `bd` upgrade flips it back, flip it off again.

Day to day: `bd ready` to find work, `bd show <id>`, `bd update <id> --claim` to claim,
`bd close <id>` when done. See [AGENTS.md](../AGENTS.md) for the full workflow.

### 4. Confirm the `lode` console script

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

### 5. Run the dev loop (nox)

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
(`tests/test_eval_live.py`); it is opt-in via an env var, not just
credential-gated (lode-b4w.7) — the test `skip`s unless `LODE_RUN_LIVE_EVAL=1`
is set, and `nox -s eval` is the only session that sets it, so a bare `nox`,
`nox -s tests`, and `nox -s unit` stay offline and never run it regardless of
what's ambient in the shell. It also still `skip`s without `ANTHROPIC_API_KEY`
once opted in. (Before lode-b4w.7, credential presence was the *only* gate —
an environment with the key set, common for coding agents, made `nox -s
tests` silently run this live, ~300s pass.)

A successful `nox -s tests` ends with a line like `=== N passed, M skipped ===` and
`Session tests was successful`. That green run is your "the environment is wired up
correctly" signal.

**Parallelism (lode-b4w.6).** Both `nox -s tests` and `nox -s unit` run under
`pytest-xdist` (`-n auto`, one worker per CPU core) — a pure wall-clock lever,
no marker filter change and no test ever skipped. The suite has no shared
on-disk state to race on (every test gets its own `$LODE_HOME` via the autouse
`_isolate_lode_home` fixture in `tests/conftest.py`), so distributing across
workers is safe; measured on an 8-core dev machine, offline, `nox -s unit` went
from ~152s serial to 33-41s parallel, and `nox -s tests` from ~127-134s serial
to 39-60s parallel, all green over repeated runs.

### 6. (Optional) Mermaid diagram validation

Only if you edit a diagram under `docs/`:

```bash
scripts/update-images.sh      # one-time: pull the mermaid-cli image
scripts/validate-mermaid.sh   # parse every fenced mermaid block, fail on syntax errors
```

### 7. (Optional) RTK command exclusions

Only if you use [RTK](https://github.com/rtk-ai/rtk) to proxy dev commands. lode
requires two commands to bypass RTK's rewrite so their output stays raw — beads
JSON (`bd … --json`) and `git worktree list --porcelain` (worktree GC parses real
porcelain, not RTK's reformatted table). RTK stores this only in your user-global
config with no project-level equivalent, so a one-time script makes it reproducible:

```bash
scripts/rtk-setup.sh          # idempotent: adds the two excludes to ~/.config/rtk/config.toml
```

## You're set up when

- `. ./venv/bin/activate` then `lode --help` prints the subcommand list, **and**
- `nox -s tests` reports all tests passing, **and**
- `bd ready` lists issues (not `no beads database found`), and `git status` is clean —
  no stray `bd init` commit.

From here, see [AGENTS.md](../AGENTS.md) (and `bd ready` / `bd prime`) for how work is
claimed and landed, and [design.md](design.md) for the architecture you're building on.
