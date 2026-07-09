---
name: code
description: Build one or more lode tasks as PRODUCERS in two phases — dispatch the `coding` subagent (Sonnet) to claim a bd issue, build in an isolated worktree, pass the quality gates, push its branch to origin, and hand off at ready-for-code-review; then dispatch the `code-reviewer` subagent (Opus) to drive that worktree via `git -C <path>`, run the technical review (/code-review + /simplify), re-gate, and swap the ticket to ready-for-land. Producers never merge/close/push trunk; a separate `/land` lander does. Every invocation also sweeps for `needs-rebase` tickets first (branches /land kicked back on a conflict) and dispatches a `coding` producer to rebase, re-gate, force-push, and swap each straight back to ready-for-land — no manual nudge needed. `/code <id>` (or `/code --single`) is one producer; bare `/code` / `/code --all-ready` / `/code <id> <id> …` fans out N parallel producers across the ready frontier. Use for any task that changes the lode repo (code, docs, configs). Examples — "/code" (fan out across `bd ready`), "/code lode-1 lode-2 lode-3", "/code lode-123", "/code --single" (top one item from `bd ready`), "/code add a --json flag to the search CLI".
---

# code

`/code` is lode's **sole producer** entry point, and it runs each task in **two dispatched phases**:

1. **Build — `coding` subagent (Sonnet):** **claim → worktree → working code → green gates → branch
   pushed to `origin/land/<id>` → ticket marked `ready-for-code-review` → keep the worktree → stop.**
   The builder does **not** review its own work.
2. **Technical review — `code-reviewer` subagent (Opus):** drives the builder's worktree in place via
   `git -C <path>` (never `EnterWorktree`), runs **`/code-review --fix` + `/simplify`**, re-gates,
   re-pushes `land/<id>`, and swaps the ticket to **`ready-for-land`** (or escalates).

Splitting build (cheap) from review (Opus) means the technical review is done by an agent that didn't
write the code — and the lander's later semantic review is too, so *neither* review of a branch is its
author's. A producer **never** merges to `trunk`, closes the ticket, pushes `trunk`, or writes the
main checkout — a single `/land` lander owns every write to `trunk` and lands reviewed branches.

There is **no separate `/code-parallel`.** Once landing left the producer, building one task and
building five became the same act — a producer just leaves a green branch on origin, and a *new*
branch ref doesn't race `trunk`, so N producers run safely in parallel. `/code` covers both:

- **bare `/code`** (no argument), **`/code --all-ready`**, or **`/code <id> <id> …`** — **N producers
  in parallel**, each in its own isolated worktree. Bare `/code` is the **default**: it fans out
  across the whole independent, unblocked ready frontier.
- **`/code <id>`** or **`/code --single`** — **one** producer.

Both subagents already own *how producer work flows* in lode (they honor `CLAUDE.md` / `AGENTS.md`,
`docs/agents-workflow.md`, and the phase-a skeleton order) — this skill's only job is to launch them
correctly **in order, build then review**, one task at a time, and relay what came back.

## What to do when invoked

0. **Sweep for `needs-rebase` kick-backs first — every invocation, regardless of argument.**
   `/land`'s cheap conflict precheck can kick a `ready-for-land` branch back: it strips
   `ready-for-land`, adds **`needs-rebase`**, and keeps the same `land/<id>` branch + build worktree
   (the ticket stays `in_progress`, so it never surfaces in `bd ready`). Nothing else consumes that
   label, so before resolving the requested task set, always check for stranded kick-backs:

   ```bash
   rtk bd list --label needs-rebase --status in_progress --json
   ```

   For **each** hit, dispatch a `coding` producer (`subagent_type: "coding"`, **`isolation:
   "worktree"`** — required for the same reason as Phase 1 below: a subagent is pinned at the repo
   root and **cannot** call `EnterWorktree` to *create* its own, so the harness must hand it a launch
   worktree at dispatch). From there it drives the *recorded* build worktree in place via `git -C
   <path>` — **not** `EnterWorktree`, which the isolation guard refuses for commands resolved into a
   path-entered worktree. Tell it explicitly this is a **rebase pickup**, not a fresh build, e.g.:

   > lode-ai1 carries `needs-rebase` (kicked back by `/land`'s conflict precheck) — run your "Rebase
   > pickup" cycle, not a fresh build: read `metadata.review_worktree` from bd, drive that worktree via
   > `git -C <path>` (do **not** `EnterWorktree` into it), `git -C <path> fetch origin trunk && git -C
   > <path> rebase origin/trunk`, re-gate via `nox -f <path>/noxfile.py`, `git -C <path> push
   > --force-with-lease` to the same `land/<id>` ref, refresh the head-SHA metadata, and swap
   > `needs-rebase` straight to `ready-for-land`. Do **not** merge, close, or push trunk. On a rebase
   > conflict, abort and escalate (`land-escalated`, leave the branch as it was) rather than guess a
   > resolution.

   Dispatch every hit **concurrently** with each other and with any Phase 1 builds below
   (`run_in_background: true`) — a rebase pickup and a fresh build never share a ticket, so they can't
   collide. **Do not dispatch a Phase 2 `code-reviewer` for a rebase pickup**: it lands directly at
   `ready-for-land`, skipping technical review entirely, the same way `/land`'s kick-back skipped
   `land-review` — the content was never judged bad, it only needed to replay onto where `trunk`
   moved. If the sweep finds nothing, say so and move on; it's not an error.

1. **Sweep for stranded `ready-for-code-review` re-entries too — same invocation, same reason.** A
   human resolving a `code-reviewer` technical-review escalation or a `coding` build-time escalation
   applies exit (a) per `docs/agents-workflow.md` by re-adding `ready-for-code-review` (and removing
   `land-escalated`) directly on the ticket, **outside** any `/code` run. That ticket stays
   `in_progress` exactly like a `needs-rebase` kick-back, so `bd ready` never returns it either — and
   unlike `needs-rebase`, nothing in the ordinary Phase 1/2 flow below ever looks for it, because
   Phase 2 only dispatches a reviewer for a ticket *this same invocation's* Phase 1 just built
   (lode-t83). Check for it the same way as step 0:

   ```bash
   rtk bd list --label ready-for-code-review --status in_progress --json
   ```

   Any hit here is, by construction, stranded from a **previous** invocation — this check runs before
   this invocation's own Phase 1 has built anything. For each hit, confirm the hand-off is actually
   reviewable before dispatching:

   ```bash
   rtk bd show <id> --json | jq -r '.[0].metadata.review_worktree'   # must be non-empty
   ```

   If it's empty (this can only happen for a build-time escalation predating the coding.md fix for
   lode-t83's Gap 1), don't guess a worktree — leave the label alone and surface it in the final
   report as needing a human to re-escalate or rebuild instead. Otherwise dispatch a `code-reviewer`
   exactly as Phase 2 does below (`subagent_type: "code-reviewer"`, **`isolation: "worktree"`**, same
   prompt shape: read `review_worktree`/`review_head`, drive via `git -C <path>`, `/code-review --fix`
   + `/simplify`, re-gate, re-push, swap to `ready-for-land` or escalate again). Dispatch every hit
   **concurrently** with each other, with any step-0 rebase pickups, and with this invocation's own
   Phase 1 builds — a stranded re-entry never shares a ticket with a fresh build or rebase pickup, so
   none of these collide. If the sweep finds nothing, say so and move on; it's not an error.

2. **Resolve the task set** from the argument:
   - **No argument** (the **default**), or **`--all-ready`** → read `bd ready` and **fan out** across
     the **independent, unblocked** frontier (honoring the dependency graph and phase-a ordering).
     Don't dispatch a ticket whose blocker is also in the batch — surface that instead of guessing the
     order.
   - **`--single`** (no ID) → one producer; tell the agent to pick the **top unblocked item from `bd
     ready`**. Do **not** pick the issue yourself — the subagent does that. (This is the former
     bare-`/code` behavior, now opt-in.)
   - **One bd issue ID** (e.g. `lode-ai1`) → one producer; tell the agent to claim and implement it.
   - **Several bd issue IDs** → **fan-out**: one producer per ID. Only dispatch IDs that are
     genuinely **independent** (no unmet dependency between them); if two share a dependency, say so
     and let the human sequence them rather than racing.
   - **Free-text** (e.g. "add a --json flag to search") → one producer; tell the agent that is the
     task — it files the bd issue itself before coding, per its own rules.

3. **Phase 1 — dispatch one `coding` builder per task** via the Agent tool with
   `subagent_type: "coding"` **and `isolation: "worktree"`**. The isolation is required: a subagent is
   pinned at the repo root and **cannot** call `EnterWorktree` to *create* its own, so the harness must
   hand each builder a worktree at dispatch — `isolation: "worktree"` launches it already cwd'd inside
   `.claude/worktrees/agent-<hash>` on its own branch off `trunk` HEAD.

   - **Solo** (`/code <id>`, `/code --single`, free-text): dispatch **exactly one** builder in the
     foreground.
   - **Fan-out** (bare `/code`, `--all-ready`, `/code <id> <id> …`): dispatch **one builder per ticket,
     concurrently** (`run_in_background: true`), and collect each result as it finishes. Each builds,
     pushes `origin/land/<its-id>`, **keeps its worktree**, and marks its own ticket
     `ready-for-code-review` independently; one builder's escalation must **not** block its siblings.

   Pass the resolved task in each prompt, e.g.:

   > Implement lode-ai1 as a producer following your cycle (claim → worktree → gates → push
   > `origin/land/<id>` → mark `ready-for-code-review`, recording `review_worktree`/`review_head` →
   > keep the worktree → stop). Do **not** review your own work, merge, close, or push trunk. Stop and
   > escalate (revert to green, annotate `land-escalated`, don't hand off) if a clarifying decision is
   > needed during the build; stop and report if a gate fails.

   (For the `--single` case: *"Pick the top ready item from `bd ready` and produce it…"*)

4. **Phase 2 — verify the hand-off, then dispatch a `code-reviewer` per built ticket.** Never trust a
   builder's task-notification alone: a builder that backgrounded its gates and stalled (lode-95o) can
   still emit a `status=completed` notification with a benign-looking summary, even though nothing was
   pushed and the ticket is still sitting `in_progress`. Before dispatching a reviewer, check the
   **actual** state in bd and on origin:

   ```bash
   rtk bd show <id> --json | jq -r '.[0].labels'          # ready-for-code-review? land-escalated?
   rtk git ls-remote origin refs/heads/land/<id>           # must resolve to a SHA
   ```

   Dispatch the reviewer **only** for a ticket where both checks pass. Otherwise read the labels before
   reacting — the two failure modes are not the same ticket:

   - **`land-escalated` present** → the builder escalated *deliberately*: it reverted to green, pushed,
     and stopped because a human owes a build decision. **Skip it.** No reviewer, and never resume it to
     "complete the hand-off" — that would override the escalation. Surface it in step 5.
   - **Otherwise** (no `ready-for-code-review`, or `origin/land/<id>` doesn't resolve) → the builder
     stalled or never finished. Do **not** send a reviewer into an unverified worktree. Resume that same
     builder (`SendMessage` to its agent id/name — it resumes with full context) and tell it plainly:
     any background gate it armed will never notify it back (a subagent with no live background children
     is stopped by the harness), so it must re-run the gate in the **FOREGROUND** within its own turn and
     complete the hand-off. Re-check both conditions once it returns before proceeding.

   For every ticket that passes both checks: use the Agent tool with `subagent_type: "code-reviewer"`
   **and `isolation: "worktree"`** — the isolation gives it a launch worktree off the repo root, so it
   never writes `trunk`. From there it drives the builder's worktree in place via `git -C <path>` —
   **not** `EnterWorktree`, which the isolation guard refuses for commands resolved into a path-entered
   worktree. Match the build cadence: one reviewer in the foreground for a solo build; one reviewer per
   ticket concurrently (`run_in_background: true`) for a fan-out, each dispatched as its builder's
   hand-off is verified.

   Pass the ticket id, e.g.:

   > Technically review lode-ai1 (it is `ready-for-code-review`): read `review_worktree`/`review_head`
   > from bd, drive that worktree via `git -C <path>` (do **not** `EnterWorktree` into it), run
   > `/code-review --fix` + `/simplify`, re-gate, re-push `land/<id>`, and swap the ticket to
   > `ready-for-land`. Do **not** merge, close, or push trunk. Escalate (revert to green, swap to
   > `land-escalated`, don't mark ready) only on a clarifying decision or "making it worse."

5. **Relay each result to the user.** Agent final messages aren't shown to the user — surface what
   matters per ticket across **both** phases: that the build gates passed and the technical review +
   re-gate passed, the **`land/<id>`** branch and head SHA, and that it reached **`ready-for-land`**
   (so `/land` can pick it up). If a **builder** escalated, say so — it reverted to green, pushed,
   applied `land-escalated`, did **not** hand off, and a human owes a build decision. If a **reviewer**
   escalated, likewise — green branch pushed, `land-escalated` set, not landable until the human
   decides. For a fan-out, give a per-ticket roll-up: which reached ready-for-land, which are still in
   review, which escalated (at build or review) and why. Report the **Step 0 sweep** the same way —
   which `needs-rebase` tickets were found, which rebased clean and are back at `ready-for-land`, and
   which hit a rebase conflict and were escalated (say which one, so a human can resolve it). Report
   step 1's sweep too — which stranded `ready-for-code-review` tickets were found, which got a
   `code-reviewer` dispatched, and which were left alone for missing `review_worktree`.

## Notes

- This skill is the **only** sanctioned way to spin up coding work from the main session, which is
  otherwise told not to spawn agents unprompted — invoking `/code` *is* the user asking. Fan-out is
  the *only* sanctioned way to run several producers at once (there is no `/code-parallel`).
- **Two phases, in order: build then review.** Phase 1 (`coding`, Sonnet) builds and **keeps its
  worktree**; phase 2 (`code-reviewer`, Opus) drives that same worktree via `git -C <path>` (never
  `EnterWorktree`) and runs the technical
  review. The review must run on the *built* branch, so always dispatch the reviewer *after* its
  builder returns `ready-for-code-review` — never in parallel with its own build. Don't dispatch a
  reviewer for a ticket that escalated at build time.
- **Step 0's rebase pickup is a third mode, not a phase.** It reuses the `coding` subagent (its
  "Rebase pickup" cycle, distinct from its normal build cycle) but skips Phase 2 entirely — a
  `needs-rebase` ticket already passed technical review before `/land` kicked it back, so it goes
  straight to `ready-for-land` once the rebase is clean. Never dispatch a `code-reviewer` for one.
- **Step 1's stranded-review sweep is Phase 2 pulled forward, not a fourth mode.** It dispatches the
  exact same `code-reviewer` subagent, the same way, for the same reason — the only difference is the
  ticket was left `ready-for-code-review` by a *previous* invocation (a human's exit-(a) re-entry)
  rather than by this invocation's own Phase 1 (lode-t83). Its guard (`review_worktree` must be
  non-empty) exists because one setter of `land-escalated` — `coding`'s build-time escalation — used
  to skip recording that metadata entirely; that gap is fixed in `coding.md`, but the guard stays as
  defense-in-depth for tickets escalated before the fix landed.
- Producers do all repo mutation inside `isolation: "worktree"` worktrees and push to
  `origin/land/<id>`; **nothing merges and nothing the author wrote is reviewed by its author.** The
  main session stays on `trunk` and never edits files here. Landing those branches is `/land`'s job,
  not this skill's.
- If an argument is genuinely ambiguous (looks like it might be an ID but isn't one that exists, or a
  fan-out set with hidden dependencies), ask the user before dispatching rather than guessing.
