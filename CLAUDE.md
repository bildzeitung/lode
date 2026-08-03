# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⛔ STOP — WORK IN A WORKTREE, NEVER ON `trunk`

**EVERY change to this repository — code, docs, configs, ANYTHING — MUST be made in a git worktree, NEVER directly on `trunk`** (this repo's default branch). This is non-negotiable.

Before editing, creating, or deleting a single file, you MUST first create/enter a worktree (use `EnterWorktree`, or the plan-mode worktree isolation option). Worktrees for this repo live under `.claude/worktrees/`. They branch from **`origin/trunk`** — `.claude/settings.json`'s `worktree.baseRef: "fresh"` (a deliberate choice, `lode-jzbz`; the harness supports no way to pin a literal local-`trunk` ref at all — `"fresh"`/`"head"` are the only two values it accepts, see [`docs/agents-workflow.md`](docs/agents-workflow.md#recycled-worktree-guard-lode-nt98)). `origin/trunk` can therefore lag local `trunk` by however long it's been since the last push — `/land` pushes `trunk` immediately after every merge, so that window is expected to usually be small, but it has never been measured. Once a worktree's branch is merged into `trunk`, delete the worktree.

If you find yourself about to run `Edit`, `Write`, or any mutating command while on `trunk`: **STOP.** Create the worktree first, then do the work there. When in doubt, confirm you are NOT on `trunk` before your first write.

**Commit after each completed task** for a granular record of changes. Merge with `--no-ff` so a unit of work lands grouped. End commit messages with:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

## What this is

**lode** — an AI-first, TUI-first personal knowledge base for "things you learn during your day at work." Fast to capture, intelligent to retrieve: the bet is grounded, *cited* Q&A over your own notes. Status: **built end-to-end.** The core loop ships — notes, version chains, cited Q&A, a minimal eval harness, and a web connector — behind a Textual TUI, plus a full CLI. Additional connectors (e.g. Atlassian) are in progress; see [`docs/decisions.md`](docs/decisions.md) for what's still open.

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

Build the venv with the lightweight init script (creates `./venv` and installs from the lock):

```bash
./scripts/python-init.sh
. ./venv/bin/activate
```

- The venv lives at **`./venv`** (repo root), not in module subdirs.
- Run **`nox -t fix`** (and `nox -s tests`) before merging any Python change; run tests via nox, not a hand-rolled venv.
- `pyproject.toml` is the INTENT layer (ranges/floors); `requirements.lock` is the ONLY place exact
  runtime versions live (hash-verified, `dev` extra deliberately unlocked) — `scripts/python-init.sh`
  installs from it by default, with `--unlocked` as the escape hatch to resolve fresh from
  `pyproject.toml`. Full split: [`docs/stack.md`](docs/stack.md#dependency-locking-lode-g2741)
  (`lode-g274.1`).

## Coding conventions

Prescriptive style **fiats** (unilateral maintainer preferences, no independent rationale) live in
their own single source of truth, imported below so they load into the main session **and** every
non-fork subagent — the `coding` producer, the `code-reviewer`, the `land-review` agent, and the
`land` session that dispatches it read the identical text, so a fiat cannot drift between who writes
code and who reviews it. Add new style fiats to [`docs/conventions.md`](docs/conventions.md), not here. (Reasoned architecture still
goes in the relevant `docs/` design doc — the litmus is in that file's preamble.)

@docs/conventions.md

## New machine setup

Everything portable travels on two wires from the same git remote: **git** (code, docs, committed `.claude/` config — settings, skills, agents) and **Dolt** (`refs/dolt/data` — the bd issue DB *and* `bd remember` memories). On a fresh clone:

0. Install **jq** (`apt-get install jq` / `brew install jq` / `choco install jq`) — a hard prerequisite, not optional tooling. The committed `PreToolUse(Bash)` guards in `.claude/settings.json` (the `bd create --deps blocks:` inversion guard, the external-tracker write guard, `lode-o29m`, and the fabricated-SHA guard, `lode-fpmi`) shell out to it; without it, all three now **deny every Bash call** rather than silently falling through unchecked (`docs/decisions.md`). Full prerequisite table: [`docs/onboarding.md` §Prerequisites](docs/onboarding.md#prerequisites).
1. `./scripts/python-init.sh && . ./venv/bin/activate`
2. `bd init` — restores the full issue DB and persistent memories from `refs/dolt/data` (`bd ready` / `bd memories` to verify). **Not `bd dolt pull`** — with no local DB yet that fails with `no beads database found`; `bd dolt pull` is for *later*, once the DB exists. `bd init` also lands a git commit that rewrites `CLAUDE.md` / `AGENTS.md` / `.claude/settings.json` / `.gitignore` with beads boilerplate and adds `.codex/` — drop it (`git reset --hard origin/trunk`); the DB and `core.hooksPath` survive. Full walkthrough: [`docs/onboarding.md` §3](docs/onboarding.md).
3. Install **rtk**, then run `scripts/rtk-setup.sh` (idempotent; installs the required `exclude_commands` into `~/.config/rtk/config.toml`). If rtk is absent, skip this and the hook in step 4 — plain commands work fine and the committed `Bash(rtk *)` allow entry is inert.
4. Re-create the deliberately **user-scope** (`~/.claude/settings.json`) pieces as wanted — these do NOT travel: the rtk PreToolUse hook (`{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}`), `"defaultMode": "acceptEdits"`, model choice, personal statusline. A related but distinct per-machine knob lives in the **project-scoped** `.claude/settings.local.json` instead (gitignored, so it doesn't travel either): an **optional** pin of `/code`'s concurrency cap, `LODE_CODE_MAX_CONCURRENT_AGENTS`, in that file's `"env"` block. **A fresh clone needs nothing here** — left unset, `/code` re-derives the cap at the start of *every* invocation, so it tracks the machine on its own. A pinned value wins outright and is then a cached constant that goes stale, so just ask Claude to recompute it after a hardware/VM-size change. The derivation itself lives in [`scripts/code-concurrency-cap.sh`](scripts/code-concurrency-cap.sh) (lode-54mo); override syntax and full rationale: [`docs/agents-workflow.md` — Concurrency cap](docs/agents-workflow.md#concurrency-cap-lode-2cf).

Project-scope permissions and auto-mode consent rules live in the committed [`.claude/settings.json`](.claude/settings.json) and travel with the clone; machine-local one-offs go in `.claude/settings.local.json` (gitignored).

## Workflow gotchas (learned the hard way)

- **A beads pre-commit hook re-exports and stages `.beads/issues.jsonl` during every commit** — even when you staged only one file with an explicit `git add`. For a commit that must not carry the jsonl (e.g. a direct doc-only commit to trunk), use `git commit --no-verify`, then confirm with `git show --stat HEAD` that only the intended files rode along. A slipped jsonl is inert here (`import.auto: false`) — a hygiene slip, not an emergency.
- **The session-close `git pull --rebase` flattens a just-made `--no-ff` merge** when trunk is 0 behind origin: rebase drops merge commits, silently discarding the merge bubble this file mandates. After merging a worktree branch into trunk, check `git rev-list --count trunk..origin/trunk` — if 0, push directly and skip the rebase; if actually behind, prefer `git merge origin/trunk` over rebasing to keep the bubble.
- **Never `git add -A` on trunk** — it sweeps in unrelated untracked files, and the pre-commit hook adds the passive jsonl on top. Stage explicit paths only.

## General Directives

1. **Ask, don't assume.** If something is unclear, ask before writing a single line. Never make silent assumptions about intent, architecture, or requirements.
2. **Simplest solution first.** Always implement the simplest thing that could work. Do not add abstractions or flexibility that weren't explicitly requested.
3. **Flag uncertainty explicitly.** If you are not confident about an approach or technical detail, say so before proceeding.
4. **Advisor, not assistant.** Never open with agreement. Challenge my thinking first or ask the question I'm avoiding. When I'm wrong, say it directly.

5. **Add confidence tags.** Rate your confidence: [Certain], [Likely], or [Guessing]. Never pretend to know.

6. **Kill the filler.** Never say 'Great question' or 'You're absolutely right.' Lead with the most useful thing first.

7. **Hold the line.** If I push back, don't fold unless I give you genuinely new information." Save it. Every chat now starts with an advisor, not a yes-man.
8. **Never file/write to an external tracker under my identity.** `gh` (and any other external-tracker CLI) is authed as me, so any WRITE it performs — `gh issue create`, `gh pr create`, `gh issue/pr comment`, `gh pr review`, `gh release`/`gist`/`repo fork`, `gh api` with a non-GET method — **including the *implicit* POST that `gh api -f/-F/--field/--raw-field/--input` performs with no `-X` on the line at all, which is `gh`'s documented default and is NOT a way around this rule** — or the equivalent on a non-GitHub tracker, goes out publicly under my name, even when a ticket's own text asks for it. Draft the issue/PR/comment text instead, mark it PENDING A HUMAN in your hand-off, and stop — I file it myself. Read-only external calls (`gh issue view`, `gh api` GET, `WebFetch`) and all internal bd filing are unaffected — this binds the main session the same as every subagent (lode-o29m; full rationale: [`docs/agents-workflow.md`](docs/agents-workflow.md#never-write-to-an-external-tracker-under-the-users-identity-lode-o29m)).

## Memory & where project knowledge lives

Claude Code gives each session an automatic, per-project memory store — no setup needed. But for lode, **`docs/` is the source of truth for every design decision**, so route knowledge deliberately:

- **Design facts and decisions → `docs/`**, never memory. Settled architecture goes in the relevant doc; open questions in [`docs/decisions.md`](docs/decisions.md) (corrections there are appended, not rewritten in place — see that file's preamble); tunables in [`docs/configuration.md`](docs/configuration.md). A design fact that lives only in memory **forks the record** — the next reader trusts the docs and misses it.
- **Memory is for working context and user preferences** that don't belong in the design — how the user likes things done, in-flight task state, cross-session reminders. Not a second home for architecture.
- When a conversation settles something architectural, the deliverable is a **doc edit (in a worktree)**, not a memory entry.

# RTK (Rust Token Killer) — Token-Optimized Commands

## Golden Rule

**Always prefix commands with `rtk`**. If RTK has a dedicated filter, it uses it; otherwise it passes through unchanged, so RTK is always safe. This holds **even inside `&&` chains**:

```bash
# ❌ Wrong
git add . && git commit -m "msg" && git push
# ✅ Correct
rtk git add . && rtk git commit -m "msg" && rtk git push
```

**One known exception — `git log` is NOT a faithful passthrough (lode-eza9).** `rtk git log`
**silently drops merge commits** (upstream [rtk-ai/rtk#2305](https://github.com/rtk-ai/rtk/issues/2305)):
measured on a real range, 7 commits → 4, all three `--no-ff` merges gone, with no marker, no count,
and exit status 0. So the "passes through unchanged, so RTK is always safe" premise above does not
hold here. Use **bare `git log`** wherever a *missing* merge commit would change a decision — history
audits, residue checks before a destructive reset, anything reasoning about merge structure. The
divergence is scoped to `log`: `rev-list` (including `--first-parent`) and `show` are faithful and
stay on `rtk`. Live exception sites are commented at the call site; the load-bearing one is
[`.claude/skills/land/SKILL.md`](.claude/skills/land/SKILL.md) Section 1's pass-start residue print.

## Commands by workflow

```bash
# Git (59–80% savings) — every subcommand is accepted, but see the `git log` exception above:
# `rtk git log` DROPS MERGE COMMITS. Faithful passthrough is not guaranteed per-subcommand.
rtk git status | diff | show | add | commit | push | pull | branch | worktree
git log   # ← BARE, not `rtk git log`, when a missing merge commit would change a decision

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


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:970c3bf2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

## Beads: Dolt is authoritative (lode's rules, not beads')

Everything above between the `BEADS INTEGRATION` markers is **regenerated by `bd init` / a beads upgrade** — anything written inside those markers is silently overwritten (that is how this section came to be lost once). lode's own beads rules live **here, below the markers**, and must stay here.

**Dolt is authoritative — `issues.jsonl` is EXPORT-ONLY (`import.auto: false`).** beads' default `import.auto: true` makes the `post-checkout`/`post-merge` git hooks auto-import `issues.jsonl` back into Dolt; a `git pull --rebase` or merge after a `bd close` then replays an intermediate committed jsonl from *before* the close and silently **reverts** it (this bit `lode-8bh` / `lode-wvf` / `lode-bxz`). It is disabled in [`.beads/config.yaml`](.beads/config.yaml) (committed, so it travels to every machine). Keep it off. Practical rules: **sync bd state only via `bd dolt push` / `bd dolt pull`** (never `bd import` the jsonl as a substitute); always `bd dolt push` after bd writes so the wire carries them; treat a committed `issues.jsonl` as a read-only snapshot, never edit or import it by hand. (lode-6ra)
