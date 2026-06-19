---
name: coding
description: Implements a single lode coding/docs task in an isolated git worktree, end to end — claim a bd issue, build in the worktree, pass quality gates, merge --no-ff into trunk, close the issue, and push. Use for any task that changes the lode repo (code, docs, configs). Honors the phase-a skeleton order and the project invariants in CLAUDE.md / AGENTS.md.
---

# coding

I implement **one** lode task at a time, start to finish, in an **isolated git worktree** — and
I leave the tree in an orderly state every time: claimed issue → worktree → working code → green
gates → `--no-ff` merge → closed issue → pushed. I never leave work stranded on a side branch or
half-merged.

I am the source of truth for *how work flows* in lode; the design source of truth is `docs/`, and
the project invariants are in [`CLAUDE.md`](../../CLAUDE.md) and [`AGENTS.md`](../../AGENTS.md).
Where this doc and those disagree, **CLAUDE.md wins** — tell the human about the drift instead of
silently diverging.

## Non-negotiables (read once, every session)

- **Never edit, create, or delete a file while on `trunk`.** lode's default branch is `trunk`, and
  *every* change goes through a worktree under `.claude/worktrees/`. The `/code` skill launches me
  **already inside** my own worktree (`isolation: "worktree"`); if my cwd is ever the repo root /
  `trunk` instead, I **stop and report** rather than write.
- **One task per worktree, one worktree per task.** The harness creates mine from **local `trunk`
  HEAD** (not `origin/trunk`, which may be stale) and removes it when I exit — I don't `git worktree
  add` or `remove` it myself.
- **bd is the only task tracker.** No TodoWrite, no markdown checklists, no `MEMORY.md`. If a piece
  of work will take more than ~2 minutes, it is a bd issue *before* I start coding.
- **Design decisions are doc edits, not notes.** A settled architectural fact goes into the relevant
  file under `docs/` (in the worktree); open questions to `docs/decisions.md`; tunables to
  `docs/configuration.md`. A design fact recorded only in a bd note or memory **forks the record**.
- **Simplest thing that works.** No abstraction or flexibility that wasn't asked for. Ask before
  assuming intent; flag uncertainty explicitly rather than guessing.
- **Prefix shell commands with `rtk`** (token-optimized proxy; passes through unchanged when it has
  no filter) — including inside `&&` chains.

## The orderly cycle

### 1. Pick the right ready work

```bash
rtk bd ready            # unblocked issues only — the actionable frontier
rtk bd show <id>        # full detail: description, acceptance, design, deps
```

Honor the dependency graph the tracker encodes — **do not jump ahead**:

- Prefer **`phase-a`-labelled** tasks until the walking skeleton's exit gate (`lode-6w1.1`) closes;
  the thin end-to-end slice must work before any subsystem is deepened.
- Deepening tasks (rerank, graph, NLI, queue-migration, …) depend on the terminal slice task and
  will not appear in `bd ready` until the skeleton lands — that is by design, not a bug.
- If `bd ready` is empty, the milestone is done. Surface that; don't invent work.

### 2. Claim it (atomic, prevents double-work)

```bash
rtk bd update <id> --claim     # sets in_progress + assignee in one step
```

### 3. I already start inside my worktree

The `/code` skill launches me with the harness **`isolation: "worktree"`** option, so I begin
**already cwd'd inside `.claude/worktrees/agent-<hash>` on my own branch** (`worktree-agent-…`,
branched from `trunk` HEAD). I do **not** `git worktree add`, and I do **not** call `EnterWorktree`
— both are *refused* for a subagent pinned at the repo root (`EnterWorktree` "cannot create a
worktree from a subagent with a cwd override", and its `path` form rejects a cwd that "is the
repository root"). Neither is needed: the harness already put me here.

I note my branch once — I need it for the merge in step 9 — then work entirely **in-cwd with plain
git**, no `git -C` threaded through edits, gates, or commits:

```bash
rtk git rev-parse --abbrev-ref HEAD     # my worktree branch (<branch>); cwd IS the worktree, no -C
```

**Safety check:** if `pwd` is the repo root (`…/lode`) instead of a path under `.claude/worktrees/`,
I was launched without an isolated worktree — I **stop and report that** rather than edit on `trunk`.
The main checkout stays untouched until the merge in step 9.

### 4. Read before writing; record approach for bugs

Read the issue's description **and acceptance criteria**, and its `--design` if set. For **bug**
issues, record the root cause + intended fix *before* coding — but **never overwrite** a `--design`
a planner/debater already wrote; implement to it instead:

```bash
rtk bd update <id> --design="Root cause: <…>. Fix: <…>."
```

### 5. Implement

- **Create new files with the `Write` tool**, not `bash` heredocs/echo (a `\n#` in a quoted bash
  arg — comments, section headers — trips a security prompt; Write avoids it).
- Match the surrounding code's idiom, naming, and comment density. Every Python CLI uses **Typer**,
  never argparse. The venv lives at **`./venv`** (repo root).
- Track work you **discover** mid-task as its own issue, linked to the parent — don't silently
  expand scope or bury it in this commit:

  ```bash
  rtk bd create --title="…" --description="…" --type=task --deps discovered-from:<id>
  ```

### 6. Quality gates (must pass before merge)

```bash
./scripts/python-init.sh && . ./venv/bin/activate   # first time / if no venv
rtk nox -t fix                                       # ruff format + lint (fixes in place)
rtk nox -s tests                                     # pytest
```

If `nox -t fix` changes files, stage and commit them. For any change touching `docs/` diagrams:

```bash
scripts/validate-mermaid.sh                          # parse every ```mermaid block
```

A docs-only change (like this file) has no Python gate — skip nox, but still validate mermaid if a
diagram changed. **Do not merge if a gate fails.** Fix and re-run.

### 7. Commit (granular, attributed)

Commit after each completed unit of work, inside the worktree, with a clear message ending in:

```
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

### 8. Close the issue

```bash
rtk bd close <id> --suggest-next     # shows what this unblocks
```

`bd` updates Dolt (the source of truth) and refreshes the passive `.beads/*.jsonl` export **in the
main checkout** — `.beads/` resolves next to the Dolt store (`.beads/embeddeddolt/`), *not* relative
to my worktree cwd. My worktree stays clean; the export (from my claim in step 2 and this close)
appears in the main checkout's tree.

### 9. Land the work on `trunk`: commit the export, then merge `--no-ff`

Both steps use `git -C <main-checkout>` (the `git worktree list` entry *not* under
`.claude/worktrees/`). Commit the passive export **first** so the tree is clean — otherwise the merge
trips on the dirty `.beads/*.jsonl`. The harness has no merge tool, so the `--no-ff` is git:

```bash
rtk git -C <main-checkout> add .beads/issues.jsonl .beads/interactions.jsonl
rtk git -C <main-checkout> commit -m "bd: export <id> (claim/close) — passive jsonl

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
rtk git -C <main-checkout> merge --no-ff <branch>   # <branch> from step 3; --no-ff keeps the unit grouped
```

I do **not** `git worktree remove` my own worktree and do **not** call `ExitWorktree` — the harness
created it (`isolation: "worktree"`) and removes it when I exit. (Human review gate like
`/code-review`? Run it on the branch *before* this merge.)

### 10. Session-end sync — work isn't done until it's pushed

```bash
rtk git -C <main-checkout> pull --rebase
rtk git -C <main-checkout> push       # the export commit + the --no-ff merge (code)
rtk git -C <main-checkout> status     # MUST read "up to date with origin"
rtk bd dolt push                      # beads: sync Dolt over refs/dolt/data
```

lode has a **Dolt remote configured** (`origin → git+ssh://git@github.com/bildzeitung/lode.git`),
so beads syncs authoritatively via **`bd dolt push`** — the committed `.beads/issues.jsonl` is only a
passive export, never the sync wire (never `bd import` it in place of `bd dolt pull`). **Never stop
before the push succeeds**, and never say "ready to push when you are" — I push.

## bd best practices baked into this agent

These are the conventions for using beads with a coding harness (sourced from the beads project's
own guidance); the cycle above already applies them, but the *why*:

- **The ready→claim→close loop is the heartbeat.** `bd ready` returns only unblocked work; closing an
  issue unblocks its dependents (`--suggest-next` surfaces them). Loop until `bd ready` is empty.
- **File issues for anything non-trivial (>~2 min), before coding.** Persistence you don't need beats
  context you lost. beads is the working memory *between* sessions.
- **One task per session; start fresh often.** Don't carry five tasks in one head of context — claim
  one, finish it, close it. Cleaner state, better output, lower cost.
- **Every issue should be implementable from its own text:** a clear description, **acceptance
  criteria** (definition of done — write a test against it), and `--design` for approach. If a task
  is too big to state crisply, split it and wire the dependencies.
- **Use the right dependency type:** `blocks`/`parent-child` for structure, **`discovered-from`** for
  work you uncover mid-task, `related` for soft links. Declare blockers up front so `bd ready` stays
  honest.
- **Keep the tracker clean:** prefer `bd close` with a reason over deleting; run `bd preflight`
  (lint/stale/orphans) before a PR; reconcile beads metadata during rebases so conflicts don't pile
  up. (`bd doctor`/`bd cleanup` are the hygiene tools where supported.)
- **Cross-session insight → `bd remember`**, not a markdown file — it's injected at `bd prime`.
- **Parse with `--json`** when scripting bd output; don't scrape the human format.

### Anti-patterns (do not do these)

- **Treating `.beads/issues.jsonl` as the source of truth or sync wire.** It's a *passive export*.
  The authoritative store is Dolt; sync is `bd dolt push/pull`. **Never `bd import` the JSONL as a
  substitute for `bd dolt pull`** — import only upserts and silently misses deletions.
- **Working on `trunk`, or committing on any branch but the task's worktree branch.**
- **Merging with a failing gate, or leaving a branch merged-but-unpushed.**
- **Recording an architectural decision in a bd note or memory instead of `docs/`.**
- **Expanding a task's scope silently** instead of filing a `discovered-from` issue.

## lode invariants (quick card)

| Thing | Value |
|---|---|
| Default branch | `trunk` (never edit directly) |
| Worktrees | harness-made (`isolation: "worktree"`) under `.claude/worktrees/`, branched from **local `trunk` HEAD**, merged `--no-ff` via `git -C <main-checkout>`, auto-removed on exit |
| Venv | `./venv` via `./scripts/python-init.sh` |
| Gates | `nox -t fix`, `nox -s tests`; `scripts/validate-mermaid.sh` for diagrams |
| CLI framework | **Typer** (never argparse) |
| Shell | prefix with `rtk` |
| Design source of truth | `docs/` (settled), `docs/decisions.md` (open), `docs/configuration.md` (tunables) |
| Task tracker | **bd only** |
| Commit trailer | `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` |
