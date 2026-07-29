# Onboarding — install & setup

> Fresh clone → working `lode` console script, restored issue database, green test suite.
> For *what lode is*, read [design.md](design.md); for how work is claimed and landed,
> [AGENTS.md](../AGENTS.md) and [CLAUDE.md](../CLAUDE.md). This doc is only "how to stand it up."

## Prerequisites

| Tool | Why | Notes |
|---|---|---|
| **Python ≥ 3.14** | Runs lode and its tests. | [`.python-version`](../.python-version) pins a known-good version; `pyenv install` matches it. Any CPython ≥ 3.14 on `PATH` works. |
| **git** | Clone, and branch every change into a worktree. | |
| **beads (`bd`)** | Issue tracker — *all* task tracking lives here, not in markdown TODOs. | **Required.** The issue database does not arrive with `git clone`; restore it in [§3](#3-restore-the-issue-database-beads). Install from [beads](https://github.com/gastownhall/beads); it bundles Dolt, so no separate Dolt install. |
| **jq** | Parses tool-call payloads for the committed `PreToolUse(Bash)` guards in [`.claude/settings.json`](../.claude/settings.json) (the `bd create --deps blocks:` inversion guard, `lode-ij24`; the external-tracker write guard, `lode-o29m`; and the fabricated-SHA guard, `lode-fpmi`) and for `.claude/statusline.sh`. | **Required.** Without it, all three guards **deny every Bash call** rather than silently letting a security-relevant check fall through unchecked — see [`docs/decisions.md`](decisions.md) for why fail-closed was chosen over fail-open. Install: `apt-get install jq` / `brew install jq` / `choco install jq`. |
| **Docker** | Validates `docs/` Mermaid diagrams via `minlag/mermaid-cli`. | Only if you edit a diagram. No Node/Chromium needed on the host. |

## Install & setup

All commands run from the repo root.

### 1. Clone

```bash
git clone <your-remote>/lode.git
cd lode
pyenv install --skip-existing   # optional: match .python-version
```

### 2. Build the virtualenv

`scripts/python-init.sh` creates `./venv` at the repo root and installs from the
committed, hash-verified `requirements.lock` by default — the exact, fully-resolved
runtime dependency set (`pyproject.toml` stays the *intent* layer: ranges/floors, never
exact versions). See [`docs/stack.md`](stack.md#dependency-locking-lode-g2741) for the
full split.

```bash
./scripts/python-init.sh
. ./venv/bin/activate
```

Re-activate in every new shell; re-run the script only when dependencies change.

#### Updating dependencies

`scripts/update-deps.sh` is the **only** sanctioned way to move `requirements.lock` —
never hand-edit it or run a bare `uv pip compile`. It recompiles a candidate lock from
`pyproject.toml`, prints a readable version diff against the committed lock (names and
versions only, no hash noise — the diff is the point), then installs the candidate into
a freshly rebuilt `./venv` and runs the quality gates (`nox -t fix`, `nox -s tests`). On
green it promotes the candidate over `requirements.lock` for you to review and commit.

On ANY other failure — the candidate install itself (an uninstallable or
hash-mismatched pin, a yanked release, a network blip) just as much as a red gate — it
prints a failure report meant to be pasted straight into a bd ticket, then trashes
whatever venv state exists and rebuilds `./venv` clean from the unchanged committed
lock. The committed lock is never touched by a failed update, in every case. The venv
rebuild itself is best-effort: it's expected to succeed, but if that rollback rebuild
also hits a failure (e.g. its own network blip), the script prints a loud warning after
the report — rather than silently losing the report — and tells you to re-run
`scripts/python-init.sh` by hand to restore `./venv`.

```bash
scripts/update-deps.sh --dry-run          # see what would change; touches nothing
scripts/update-deps.sh                    # recompile + gate + promote/rollback the WHOLE lock
scripts/update-deps.sh --package trafilatura   # bump just one package
```

### 3. Restore the issue database (beads)

`git clone` gives you **no issues** — until you do this, `bd ready` fails with `no beads
database found`. The Dolt database backing `bd` lives in the gitignored
`.beads/embeddeddolt/`; the data travels on the *same* git remote under a separate ref,
`refs/dolt/data`. `.beads/issues.jsonl` is a passive export, never the wire.

```bash
bd init     # clones the Dolt DB from sync.remote in .beads/config.yaml
```

`sync.remote` and `issue-prefix: lode` are both committed, so plain `bd init` finds the
remote, prints `✓ bd initialized from git remote!`, and mints correct issue IDs whatever
the checkout directory is called. It also sets `core.hooksPath` — a *local* git config
that no clone inherits, which is the other reason this step isn't optional.

> ⚠️ **`bd init` lands a git commit you must drop.** It rewrites `CLAUDE.md`, `AGENTS.md`,
> `.claude/settings.json`, and `.gitignore` with its own boilerplate and adds `.codex/`,
> clobbering this repo's committed project config (including the `autoMode` consent rules):
>
> ```bash
> git log --oneline -1          # expect: "bd init: initialize beads issue tracking"
> git reset --hard origin/trunk
> ```
>
> Nothing is lost — the database is gitignored and `core.hooksPath` lives in `.git/config`,
> so both survive the reset.

Verify, then run `bd prime` once for the full command reference:

```bash
bd ready                                  # lists open issues
bd memories                               # project memories came across too
git config core.hooksPath                 # → <repo>/.beads/hooks
grep '^import.auto' .beads/config.yaml    # → import.auto: false
```

#### Rules that keep the database intact

Dolt is authoritative. Breaking these has silently reverted closed issues here before
(`lode-6ra`):

- **Sync only via `bd dolt push` / `bd dolt pull`.** Push after any `bd` write, alongside
  `git push` — otherwise your issue changes never leave the machine.
- **Never `bd import` or hand-edit `.beads/issues.jsonl`.** It is a read-only export.
- **Keep `import.auto: false`.** beads defaults it to `true`, which lets the
  `post-checkout`/`post-merge` hooks re-import a *stale* committed JSONL after a pull or
  merge, replaying pre-`bd close` state and reverting closes. If a `bd` upgrade flips it
  back, flip it off again.

### 4. Confirm the `lode` console script

The editable install puts the `lode` entry point (`lode.cli:app`) on your `PATH`:

```bash
lode --help        # every listed subcommand is real
lode version
```

The eval harness (`lode.eval.harness.score_golden_set`) is deliberately *not* a
subcommand — it is a maintainer/CI integration test, run via `nox -s eval`
(`docs/decisions.md`, Shape A).

### 5. Warm the local model cache

`lode`'s embedder and reranker/NLI cross-encoder run **locally** (fastembed/ONNX,
no server) — but on a cold cache, the *first* call downloads ~500MB of weights
from HuggingFace, right in the middle of your first `lode work` or `lode ask`.
Pull them deliberately instead, once:

```bash
lode models pull
```

This warms `$LODE_HOME/models/` (the [model cache directory](configuration.md#paths--locations),
`lode-gmo`) so a reboot never re-triggers the download. After this, indexing and
retrieval are fully offline; see [configuration.md](configuration.md#models) for
the `HF_HUB_OFFLINE=1` air-gapped escape hatch.

**`lode ask` returning nothing after pulling a release?** A schema-changing release makes the LanceDB
vector cache self-heal on next open (it drops and silently recreates the table under the new
schema, lode-t08v) — so retrieval goes quiet, empty, until the cache is repopulated. See
[storage.md's "Rebuild the vector
cache"](storage.md#rebuild-the-vector-cache-after-a-schema-mismatch-crash-lode-2lu2) how-to.

### 6. Run the dev loop (nox)

[`noxfile.py`](../noxfile.py) runs **inside the already-built `./venv`**, not an isolated
env, so activate first. `nox -t fix` and `nox -s tests` are the two entry points REQUIRED
before any merge (CLAUDE.md) — a narrower claim than "runs by default": a bare `nox`
invocation also runs `nox -s shellcheck` and `nox -s linkcheck` (four sessions total; see
[`noxfile.py`](../noxfile.py)'s module docstring for what those two check):

```bash
nox -t fix         # ruff format + ruff check --fix
nox -s tests       # FULL suite, no marker filter — must be green before any merge
```

`nox -s tests` is what `/land` re-runs before trunk; nothing is ever skipped from it. A
green run ending `Session tests was successful` is your "environment is wired up" signal.
Five opt-in sessions sit outside the default set:

| Session | What it does |
|---|---|
| `nox -s unit` | `pytest -m "not slow"` — fast inner loop while iterating. Excludes only the handful of tests dominated by a real model load (the un-mocked `FastEmbedCrossEncoder` reranker). A convenience, **never** a substitute for `nox -s tests`. |
| `nox -s build` | Builds a wheel + sdist and asserts the shipped package-data is present. |
| `nox -s eval` | The live eval test (`tests/test_eval_live.py`). Needs `ANTHROPIC_API_KEY` **and** `LODE_RUN_LIVE_EVAL=1`, which only this session sets — so `tests` and `unit` stay offline even where a key is ambient. CI-only. |
| `nox -s coverage` | The FULL test suite under `pytest-cov`, emitting a coverage report — CI-only (lode-qxdn.3). |
| `nox -s lock_currency` | Verifies `requirements.lock` is still what `pyproject.toml` resolves to — a local mirror of CI's lock-currency job (lode-sys4); `/land`'s re-gate runs this so a stale lock never lands. |

Three pytest markers are registered in `pyproject.toml` under `--strict-markers` (a typo'd marker
is a collection error, not a silently-ignored no-op):

| Marker | Meaning |
|---|---|
| `slow` | Real model-load cost (the `FastEmbedCrossEncoder` reranker, or the live eval Q&A leg) — excluded from `nox -s unit`, always included in `nox -s tests`. Also relaxes the guard's *socket* half (only), so a cold model cache can pull its weights down; the Anthropic-client half still applies. Do **not** reach for `slow` to quiet the guard on a test that is not about a real model load — use `network`, which says what it means. |
| `network` | The **sole sanctioned** escape hatch from `tests/conftest.py`'s autouse network/LLM-client guard (lode-85q): this test deliberately reaches real, un-mocked Anthropic-SDK / network machinery. Every other test fails **loudly**, not silently, when it falls through to a real `anthropic.Anthropic()` construction, or to a real network call (`slow` excepted, above). |
| `trips_network_guard` | **Not** an escape hatch — the opposite. This test trips the guard *on purpose* and catches the raise itself (`tests/test_network_guard.py`), so it consumes the violation the guard records instead of failing on it in teardown (lode-sx17). The call is still blocked. A marked test that trips nothing also fails, so the marker cannot go stale unnoticed. |

The guard is a net for *accidents*, not an adversary — but it no longer depends on nobody
swallowing its `pytest.fail`. Both halves record the violation before raising, and an autouse
teardown check fails the test on anything left unconsumed, so a connect made **off the main
thread**, or inside a third-party `except Exception` (huggingface_hub's agent-harness registry
fetch was the case that surfaced this — lode-sx17), is now caught rather than merely blocked. Still
out of reach: a connect made in a **subprocess**. `tests/conftest.py`'s module docstring is the full
account.

Both `tests` and `unit` run under `pytest-xdist` (`-n 8` by default — `LODE_TEST_WORKERS`
overrides, including back to `-n auto`; see `noxfile.py`'s module docstring and
[`docs/agents-workflow.md`](agents-workflow.md#concurrency-cap-lode-2cf) for why `auto` stopped
being the default, lode-bv6y); every test gets its own `$LODE_HOME` via the autouse
`_isolate_lode_home` fixture, so there is no shared on-disk state to race on.

### 7. (Optional) Mermaid diagram validation

Only if you edit a diagram under `docs/`:

```bash
scripts/update-images.sh      # one-time: pull the mermaid-cli image
scripts/validate-mermaid.sh   # parse every fenced mermaid block, fail on syntax errors
```

### 8. (Optional) RTK command exclusions

Only if you use [RTK](https://github.com/rtk-ai/rtk) to proxy dev commands. Two commands
must bypass RTK's rewrite so their output stays raw: beads JSON (`bd … --json`) and `git
worktree list --porcelain` (worktree GC parses real porcelain, not RTK's reformatted
table). RTK keeps this in your user-global config only, so a script makes it reproducible:

```bash
scripts/rtk-setup.sh          # idempotent: adds the excludes to ~/.config/rtk/config.toml
```

## You're set up when

- `lode --help` prints the subcommand list (venv activated), **and**
- `nox -s tests` passes, **and**
- `bd ready` lists issues, with `git status` clean — no stray `bd init` commit.

## Before your first change

Four project rules that bite newcomers; [CLAUDE.md](../CLAUDE.md) is authoritative:

- **Never edit `trunk` directly** — code, docs, config, anything. Every change starts in a
  git worktree under `.claude/worktrees/`, branched from **`origin/trunk`** (`worktree.baseRef:
  "fresh"`, `lode-jzbz`; can lag local `trunk` by however long since `/land`'s last push — usually
  small, never measured). Merge back with `--no-ff`.
- **Gate before merging:** `nox -t fix` and `nox -s tests`.
- **A pre-commit hook re-exports and stages `.beads/issues.jsonl` on every commit.** Stage
  explicit paths — never `git add -A`.
- **Close the session by pushing both wires:** `git push` *and* `bd dolt push`.
