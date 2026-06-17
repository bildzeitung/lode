# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⛔ STOP — WORK IN A WORKTREE, NEVER ON `trunk`

**EVERY change to this repository — code, docs, configs, ANYTHING — MUST be made in a git worktree, NEVER directly on `trunk`** (this repo's default branch). This is non-negotiable.

Before editing, creating, or deleting a single file, you MUST first create/enter a worktree (use `EnterWorktree`, or the plan-mode worktree isolation option). Worktrees for this repo live under `.claude/worktrees/`. Branch them from **local `trunk` HEAD**, not `origin/trunk` (which may be stale). Once a worktree's branch is merged into `trunk`, delete the worktree.

If you find yourself about to run `Edit`, `Write`, or any mutating command while on `trunk`: **STOP.** Create the worktree first, then do the work there. When in doubt, confirm you are NOT on `trunk` before your first write.

**Commit after each completed task** for a granular record of changes. Merge with `--no-ff` so a unit of work lands grouped. End commit messages with:

```
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

## What this is

**lode** — an AI-first, TUI-first personal knowledge base for "things you learn during your day at work." Fast to capture, intelligent to retrieve: the bet is grounded, *cited* Q&A over your own notes. Status: **design captured, not yet built.**

The source of truth is the design under [`docs/`](docs/). Read [`docs/design.md`](docs/design.md) first — it's the index/overview with a map of the companion docs:

- [`docs/design.md`](docs/design.md) — the core problem, the bet, principles, the save path, build sequencing
- [`docs/storage.md`](docs/storage.md) — ownership boundary, event-sourced version chains, invalidation, the async work queue, data shape
- [`docs/retrieval.md`](docs/retrieval.md) — the hybrid retrieval pipeline + the citation-faithfulness gate
- [`docs/externals.md`](docs/externals.md) — external sources, the knowledge graph, link-rot immunity, privacy, hard delete
- [`docs/stack.md`](docs/stack.md) — the decided stack and the split-store rationale
- [`docs/configuration.md`](docs/configuration.md) — every tunable knob and build constant
- [`docs/decisions.md`](docs/decisions.md) — open decisions, deferred but not forgotten

**Build sequencing lives in `docs/design.md` §7** — core-first (notes + version chains + cited Q&A + a minimal eval harness), then connectors one at a time. Do not fan out before the core loop works.

## Diagrams

The design docs contain **Mermaid** diagrams. Validate them against the same parser GitHub renders with — `minlag/mermaid-cli` in Docker, no Node/Chromium toolchain on the host:

```bash
scripts/update-images.sh      # pull the mermaid-cli image (one-time / on update)
scripts/validate-mermaid.sh   # parse every ```mermaid block in docs/, fail on syntax errors
```

In Mermaid labels use `<br>` for line breaks (never `\n`), and avoid `<b>`/`<i>` (GitHub renders them literally).

## Python environment

Build the venv with the lightweight init script (creates `./venv` from `requirements.txt`):

```bash
./scripts/python-init.sh
. ./venv/bin/activate
```

- The venv lives at **`./venv`** (repo root), not in module subdirs.
- Every Python CLI in this repo uses **Typer**, never argparse.
- Run **`nox -t fix`** (and `nox -s tests`) before merging any Python change; run tests via nox, not a hand-rolled venv.
- `requirements.txt` is seeded from the decided stack (`docs/stack.md`) and unpinned until the build starts.

## General Directives

1. **Ask, don't assume.** If something is unclear, ask before writing a single line. Never make silent assumptions about intent, architecture, or requirements.
2. **Simplest solution first.** Always implement the simplest thing that could work. Do not add abstractions or flexibility that weren't explicitly requested.
3. **Flag uncertainty explicitly.** If you are not confident about an approach or technical detail, say so before proceeding.

# RTK (Rust Token Killer) — Token-Optimized Commands

## Golden Rule

**Always prefix commands with `rtk`**. If RTK has a dedicated filter, it uses it; otherwise it passes through unchanged, so RTK is always safe. This holds **even inside `&&` chains**:

```bash
# ❌ Wrong
git add . && git commit -m "msg" && git push
# ✅ Correct
rtk git add . && rtk git commit -m "msg" && rtk git push
```

## Commands by workflow

```bash
# Git (59–80% savings) — passthrough works for ALL subcommands
rtk git status | log | diff | show | add | commit | push | pull | branch | worktree

# GitHub
rtk gh pr view <n> | gh pr checks | gh run list | gh issue list | gh api

# Tests / build / lint (60–99%)
rtk pytest | nox -s tests        # Python test failures only
rtk ruff                          # lint violations grouped

# Files & search (60–75%) — format flags (-c, -l, -o, -Z) run raw
rtk ls <path> | read <file> | grep <pattern> | find <pattern>

# Infra / analysis
rtk docker ps | docker images | docker logs <c>
rtk err <cmd> | log <file> | json <file> | env

# Meta
rtk gain            # token-savings analytics
rtk gain --history  # command history with savings
rtk discover        # find missed RTK opportunities
rtk proxy <cmd>     # run raw, unfiltered (debugging)
```

Overall **60–90% token reduction** on common dev operations. `rtk --version` / `which rtk` to verify the binary (name collision: a different `rtk` exists — if `rtk gain` fails, you have the wrong one).
