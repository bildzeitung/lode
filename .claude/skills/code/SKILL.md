---
name: code
description: Build one or more lode tasks as PRODUCERS in two phases — dispatch the `coding` subagent (Sonnet) to claim a bd issue, build in an isolated worktree, pass the quality gates, push its branch to origin, and hand off at ready-for-code-review; then dispatch the `code-reviewer` subagent (Opus) to fetch that branch and check it out into its own launch worktree, run the technical review (/code-review + /simplify), re-gate, and swap the ticket to ready-for-land. Producers never merge/close/push trunk; a separate `/land` lander does. Every invocation also sweeps for `needs-rebase` tickets first (branches /land kicked back on a conflict) and dispatches a `coding` producer to merge trunk in, re-gate, and push the result itself — an ordinary, non-force push, since a merge never rewrites what's already on origin — swapping each straight back to ready-for-land itself (lode-cln) — self-heals a clean merge or a mechanical (independent, non-overlapping) conflict on its own; a conflict where the two sides genuinely disagree still needs a human. `/code <id>` (or `/code --single`) is one producer; bare `/code` / `/code --all-ready` / `/code <id> <id> …` fans out N parallel producers across the ready frontier, throttled to a shared concurrency cap (builders + reviewers + sweep dispatches combined; memory-derived default, user-overridable, lode-2cf) so the fan-out never runs more agents at once than the machine can gate safely. Use for any task that changes the lode repo (code, docs, configs). Examples — "/code" (fan out across `bd ready`), "/code lode-1 lode-2 lode-3", "/code lode-123", "/code --single" (top one item from `bd ready`), "/code add a --json flag to the search CLI".
---

# code

`/code` is lode's **sole producer** entry point, and it runs each task in **two dispatched phases**:

1. **Build — `coding` subagent (Sonnet):** **claim → worktree → working code → green gates → branch
   pushed to `origin/land/<id>` → ticket marked `ready-for-code-review` → keep the worktree → stop.**
   The builder does **not** review its own work.
2. **Technical review — `code-reviewer` subagent (Opus):** fetches the pushed `land/<id>` branch and
   checks it out into its own launch worktree (never `EnterWorktree`, never the builder's worktree),
   runs **`/code-review --fix` + `/simplify`**, re-gates, re-pushes `land/<id>`, and swaps the ticket
   to **`ready-for-land`** (or escalates).

Splitting build (cheap) from review (Opus) means the technical review is done by an agent that didn't
write the code — and the lander's later semantic review is too, so *neither* review of a branch is its
author's. A producer **never** merges to `trunk`, closes the ticket, pushes `trunk`, or writes the
main checkout — a single `/land` lander owns every write to `trunk` and lands reviewed branches.

There is **no separate `/code-parallel`.** Once landing left the producer, building one task and
building five became the same act — a producer just leaves a green branch on origin, and a *new*
branch ref doesn't race `trunk`, so N producers run safely in parallel *within one `/code`
invocation*. `/code` covers both:

- **bare `/code`** (no argument), **`/code --all-ready`**, or **`/code <id> <id> …`** — **N producers
  in parallel**, each in its own isolated worktree. Bare `/code` is the **default**: it fans out
  across the whole independent, unblocked ready frontier.
- **`/code <id>`** or **`/code --single`** — **one** producer.

Both subagents already own *how producer work flows* in lode (they honor `CLAUDE.md` / `AGENTS.md`,
`docs/agents-workflow.md`, and the phase-a skeleton order) — this skill's only job is to launch them
correctly **in order, build then review**, one task at a time, and relay what came back.

## What to do when invoked

> **Topology — run only one `/code` invocation at a time (concurrent invocations are unsupported,
> lode-pzr).** Fan-out (N producers in parallel) is safe and encouraged **within** a single
> invocation — see [Notes](#notes) for why. But **step 0 and step 1 below race across two
> *concurrent* invocations**: both sweeps select on a ticket's label (`needs-rebase` /
> `ready-for-code-review`), and that label is only swapped at the very **end** of the dispatched
> agent's work — so invocation B's sweep can select a ticket whose agent from invocation A is still
> live, and dispatch a **second** agent onto the same worktree via `git -C`. Today's consequence is
> benign (the loser's push non-fast-forward-rejects; clean-tree assertions guard the worktree), but
> it is a real race, not an invariant — don't start a second `/code` while one is still running
> against this repo. Need more parallelism? Pass more IDs (or use bare `/code`) to the **same**
> invocation instead of launching a second one.

> **Concurrency cap — one shared budget for every dispatch below (lode-2cf).** 7 concurrent agents
> (builders + reviewers) crashed the Claude Code host twice on 2026-07-10: each agent's gate is
> `nox -s tests` with pytest-xdist — back then `-n auto`, **one worker per CPU core** (noxfile.py) —
> each holding a cached ONNX reranker, and 7 × 8 workers exhausted this 15GiB/8-core WSL2 machine's
> memory. Staggering to ~4 concurrent agents ran the identical workload with zero further crashes.
> **Before step 0**, compute the cap once and hold it for the rest of the invocation:
>
> ```bash
> CODE_MAX_CONCURRENT_AGENTS="${LODE_CODE_MAX_CONCURRENT_AGENTS:-$(
>   mem_kib=$(awk '/^MemAvailable:/{print $2; exit}' /proc/meminfo 2>/dev/null)
>   [ -z "$mem_kib" ] && mem_kib=$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo 2>/dev/null)
>   nproc_n=$(nproc 2>/dev/null || echo 4)
>   workers_n="${LODE_TEST_WORKERS:-8}"    # same effective width noxfile.py's gate uses (lode-bv6y)
>   # Anything that is not a positive integer — `auto`, xdist's `logical`, a typo,
>   # an exported-empty var — means "we cannot know the width here", and the widest
>   # the gate can get is one worker per core. Assume that. Bash arithmetic silently
>   # evaluates a non-numeric string to 0, which would collapse the per-agent budget
>   # to the 2GiB floor and OVER-dispatch (cap 12 on a 24-core box) — a bad value
>   # must err tight, never optimistic; over-dispatch is what crashed this host.
>   case "$workers_n" in ''|*[!0-9]*|0) workers_n=$nproc_n ;; esac
>   if [ -n "$mem_kib" ]; then
>     # Per-agent gate budget = 2GiB fixed + 0.125GiB/xdist-worker: the gate's
>     # footprint scales with the WORKER COUNT the gate actually spawns
>     # (LODE_TEST_WORKERS, default 8 — lode-bv6y), not with this machine's
>     # core count.
>     # Measured, not extrapolated; 3GiB/agent @ 8 workers, 5GiB @ 24 workers.
>     # docs/agents-workflow.md#concurrency-cap-lode-2cf holds the measurements,
>     # the modelling assumption, and how to re-measure when the suite grows.
>     per_agent_kib=$(( 2 * 1024 * 1024 + workers_n * 1024 * 1024 / 8 ))
>     by_mem=$(( mem_kib / per_agent_kib ))
>   else
>     by_mem=4                            # /proc/meminfo unavailable (non-Linux) — conservative fallback
>   fi
>   by_cpu=$(( nproc_n / 2 ))
>   [ "$by_cpu" -lt 1 ] && by_cpu=1
>   cap=$by_mem
>   [ "$cap" -gt "$by_cpu" ] && cap=$by_cpu
>   [ "$cap" -lt 1 ] && cap=1
>   echo "$cap"
> )}"
> ```
>
> - **`LODE_CODE_MAX_CONCURRENT_AGENTS`** (env var) wins outright when set — no clamping, no
>   derivation; a static per-machine number the user sets beats a heuristic that guesses wrong. Set it
>   durably, machine-locally, **without editing this file**, via `.claude/settings.local.json`'s
>   `"env"` block (gitignored, applied to every session on that machine):
>   `{"env": {"LODE_CODE_MAX_CONCURRENT_AGENTS": "6"}}` — or export it in the shell that launches
>   `claude` for a one-off override. The skill re-reads it fresh at the start of every invocation.
> - **Unset** → derived from `/proc/meminfo` (`MemAvailable`, falling back to `MemTotal`), divided by a
>   per-agent gate budget that scales with **the effective `pytest -n` worker count** — `LODE_TEST_WORKERS`
>   if set (mirroring whatever the gate itself uses, `noxfile.py`), else its default of **8** regardless
>   of core count (SPIKE lode-mtuy: `-n auto` measured *slower* than `-n 8` on this suite, so it is no
>   longer the default anyone hits by not setting anything) — `2 + workers/8` GiB — then capped at
>   `nproc/2` and floored at 1. Resolves to **4** on the original 15GiB/8-core WSL2 machine (unchanged —
>   `2 + 8/8` = 3GiB/agent, identical to before, so the empirically-stable stagger is preserved) and to
>   **9** on a 31GiB/24-core box now that its gate defaults to 8 workers instead of 24 (`2 + 8/8` =
>   3GiB/agent; `29 / 3` clamped by `by_cpu = 24/2 = 12` → 9) — up from the prior **5**, which was the
>   safe number *only* while that box's gate still spawned 24 workers by default. Setting
>   `LODE_TEST_WORKERS=auto` on a box (opting back into one worker per core) restores the old
>   nproc-scaled budget and the cap correspondingly drops back toward 5 there — the two knobs are meant
>   to move together. All of these numbers are backed by a real measurement of the gate's peak memory,
>   recorded in [`docs/agents-workflow.md`](../../../docs/agents-workflow.md#concurrency-cap-lode-2cf) —
>   the cap is a throughput heuristic, **not** a worst-case memory bound; don't "tighten" it into one
>   without reading that section.
> - **The cap is one shared budget across every dispatch source in this invocation** — step 0's
>   rebase pickups, step 1's stranded-review pickups, Phase 1 builders, and Phase 2 reviewers all draw
>   from the same count; builders and reviewers are not separate pools. Track how many dispatched
>   agents are still in flight; when the next dispatch would exceed `CODE_MAX_CONCURRENT_AGENTS`,
>   **queue** it instead and dispatch it only once an in-flight agent completes and frees a slot —
>   across *all* sources, not per-step (e.g. a Phase 2 review that becomes dispatchable while step 0's
>   rebase pickups are still running competes for the same slots they hold). Full rationale and the
>   override mechanism are documented in
>   [`docs/agents-workflow.md`](../../../docs/agents-workflow.md#concurrency-cap-lode-2cf).

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
   worktree at dispatch). From there it fetches `origin/land/<id>` and checks it out **into that same
   launch worktree** — no `EnterWorktree`, no `git -C` into anything else needed; `Edit`/`Write`/`nox`
   all work natively once the branch is checked out locally. Tell it explicitly this is a **rebase
   pickup**, not a fresh build, e.g.:

   > lode-ai1 carries `needs-rebase` (kicked back by `/land`'s conflict precheck) — fetch it and
   > **merge current `trunk` in** (do not rebase): `git fetch origin land/lode-ai1 trunk && git
   > checkout -B land/lode-ai1 FETCH_HEAD` (`--detach` if that branch name is checked out elsewhere),
   > `git merge origin/trunk`, re-gate (`nox -t fix` / `nox -s tests`), commit anything the gate loop
   > produced, then `git push origin HEAD:land/lode-ai1` (an ordinary push — the merge only appends,
   > it never rewrites what's already on `land/lode-ai1`), refresh `land_head`/`land_summary`, and
   > swap `needs-rebase` straight to `ready-for-land` yourself. Do not merge, close, or push trunk. On
   > a merge conflict: if both sides added independent, non-overlapping content (a **mechanical**
   > conflict), resolve it directly with `Edit` and continue, then finish the same way; if the two
   > sides genuinely **disagree**, abort the merge and escalate yourself (`land-escalated`, leave the
   > branch as it was) rather than guess — that stays a human decision.

   Merging `trunk` into the branch — rather than rebasing the branch onto `trunk` — is what keeps
   this whole cycle inside the one dispatched producer, start to finish: a merge commit appends to
   history, it never rewrites what `land/<id>` already carries on origin, so the push back is an
   ordinary fast-forward. Expect the merge to **conflict** — a branch only carries `needs-rebase`
   because it already failed `/land`'s clean-merge precheck — and resolving it keeps the push a
   fast-forward all the same. Full reasoning:
   [`docs/agents-workflow.md`](../../../docs/agents-workflow.md#the-step-0-pickup-merges-it-never-rebases-lode-cln).

   Dispatch every hit **concurrently** with each other and with any Phase 1 builds below
   (`run_in_background: true`), **subject to the concurrency cap** (`CODE_MAX_CONCURRENT_AGENTS`,
   computed above) — a rebase pickup and a fresh build never share a ticket, so they can't collide,
   but they draw from the same budget, so queue the overflow and dispatch it as slots free. **Do not
   dispatch a Phase 2 `code-reviewer` for a rebase pickup**: it lands directly at
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
   rtk bd show <id> --json | jq -r '.[0].metadata.review_head'   # must be non-empty
   ```

   If it's empty (this can only happen for a build-time escalation predating the coding.md fix for
   lode-t83's Gap 1), don't guess a head SHA — leave the label alone and surface it in the final
   report as needing a human to re-escalate or rebuild instead. Otherwise dispatch a `code-reviewer`
   exactly as Phase 2 does below (`subagent_type: "code-reviewer"`, **`isolation: "worktree"`**, same
   prompt shape: read `review_head`, fetch + check out `land/<id>` into its own launch worktree,
   `/code-review --fix` + `/simplify`, re-gate, re-push, swap to `ready-for-land` or escalate again).
   Dispatch every hit **concurrently** with each other, with any step-0 rebase pickups, and with this
   invocation's own Phase 1 builds, **subject to the same concurrency cap** — a stranded re-entry
   never shares a ticket with a fresh build or rebase pickup, so none of these collide, but all of
   them draw from the one shared budget; queue the overflow and dispatch it as slots free. If the
   sweep finds nothing, say so and move on; it's not an error.

2. **Resolve the task set** from the argument:
   - **No argument** (the **default**), or **`--all-ready`** → read the filtered `bd ready --json`
     frontier (callout below) and **fan out** across the **independent, unblocked** frontier (honoring
     the dependency graph and phase-a ordering).
     Don't dispatch a ticket whose blocker is also in the batch — surface that instead of guessing the
     order.
   - **`--single`** (no ID) → one producer; `/code` resolves the pick **itself** — build the same
     filtered `bd ready --json` frontier the callout below defines, take the **top** entry (`bd ready`
     is already priority-ordered, so no extra sort is needed), dispatch it as an explicitly-named id.
     `--single` collapses to "bare `/code`, limited to one ticket": same selection, same filter, same
     skip-reporting; only the fan-out width differs.
   - **One bd issue ID** (e.g. `lode-ai1`) → one producer; tell the agent to claim and implement it.
   - **Several bd issue IDs** → **fan-out**: one producer per ID. Only dispatch IDs that are
     genuinely **independent** (no unmet dependency between them); if two share a dependency, say so
     and let the human sequence them rather than racing.
   - **Free-text** (e.g. "add a --json flag to search") → one producer; tell the agent that is the
     task — it files the bd issue itself before coding, per its own rules.

   > **Auto-select paths only — exclude `human`-labeled tickets and epics (lode-8pqv).** `bd ready` is
   > a dependency-satisfaction query, not a build queue: nothing about it guarantees a ticket is
   > something a producer can actually build. Two categories reach it anyway and must never be
   > **auto**-selected, on the **no-argument**, **`--all-ready`**, and **`--single`** paths:
   >
   > - any ticket carrying the **`human`** label — it exists precisely because an agent cannot resolve
   >   it (that's what `/sweep` surfaces it for); dispatching a producer at one either invents the
   >   decision the label exists to prevent, or burns a build cycle re-discovering that a human was
   >   already asked.
   > - any ticket with **`issue_type == epic`** — a container with no implementable acceptance
   >   criteria of its own.
   >
   > **Read the frontier as JSON on every auto-select path, including `--single`; the `human` label is
   > invisible otherwise.** Plain `bd ready` prints an `[epic]` type marker but renders no labels at
   > all, so a `human` ticket is indistinguishable from a buildable one unless you ask for the fields:
   >
   > ```bash
   > rtk bd ready --json | jq -r '.[] | select((.labels // []) | index("human") | not) | select(.issue_type != "epic") | .id'
   > ```
   >
   > (`labels` is `null`, not `[]`, on a ticket with none — hence the `// []`.) `bd ready` is already
   > priority-ordered, so this list's first entry **is** the highest-priority buildable item — no extra
   > sort needed. On the no-argument and `--all-ready` paths, filter the frontier this way **before**
   > fanning out across all of it. On `--single`, filter the same way and dispatch just the **top**
   > entry. Either way, **report each ticket dropped** — id + reason
   > (`human`-labeled or epic) — per step 5: a skip is a signal to the operator, not noise to drop
   > silently.
   >
   > **If nothing survives the filter, dispatch nothing and say so** — never fall back to a
   > filtered-out ticket. A frontier of nothing but `human` tickets and epics is a real, reachable
   > state (both sit in `bd ready` indefinitely by nature: a decision ticket *is* its own blocker, and
   > bd won't let a task block an epic), and it means there is no buildable work right now — a signal
   > for `/sweep`, not a build target. That holds on all three auto-select paths: `--single` dispatches
   > no producer at all, and the fan-out paths fan out across nothing.
   >
   > This filter applies **only** to auto-selection. **Explicitly-named IDs are an operator override
   > and are never filtered** — `/code lode-wbv8` (a single named ID) or `/code lode-wbv8 lode-ai1`
   > (named among several) must keep dispatching exactly as before, `human` label or `epic` type
   > notwithstanding: the operator named it on purpose, so the ticket's kind is not this skill's call
   > to second-guess on that path.

3. **Phase 1 — dispatch one `coding` builder per task** via the Agent tool with
   `subagent_type: "coding"` **and `isolation: "worktree"`**. The isolation is required: a subagent is
   pinned at the repo root and **cannot** call `EnterWorktree` to *create* its own, so the harness must
   hand each builder a worktree at dispatch — `isolation: "worktree"` launches it already cwd'd inside
   `.claude/worktrees/agent-<hash>` on its own branch off `trunk` HEAD.

   - **Solo** (`/code <id>`, `/code --single`, free-text): dispatch **exactly one** builder in the
     foreground.
   - **Fan-out** (bare `/code`, `--all-ready`, `/code <id> <id> …`): dispatch **one builder per ticket,
     concurrently** (`run_in_background: true`), **up to the concurrency cap**
     (`CODE_MAX_CONCURRENT_AGENTS`, shared with steps 0/1's sweeps and Phase 2 below) — queue any
     remaining tickets and dispatch each as an in-flight agent completes and frees a slot. Collect each
     result as it finishes. Each builds, pushes `origin/land/<its-id>`, **keeps its worktree**, and
     marks its own ticket `ready-for-code-review` independently; one builder's escalation must **not**
     block its siblings.

   Pass the resolved task in each prompt, e.g.:

   > Implement lode-ai1 as a producer following your cycle (claim → worktree → gates → push
   > `origin/land/<id>` → mark `ready-for-code-review`, recording `review_worktree`/`review_head` →
   > keep the worktree → stop). Do **not** review your own work, merge, close, or push trunk. Stop and
   > escalate (revert to green, annotate `land-escalated`, don't hand off) if a clarifying decision is
   > needed during the build; stop and report if a gate fails.

   `--single` dispatches the same way, with the single id `/code` already resolved in step 2 — the
   builder never re-reads `bd ready` to pick its own ticket.

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
   never writes `trunk`. From there it fetches `origin/land/<id>` and checks the branch out **into that
   same launch worktree** — no `EnterWorktree`, no `git -C` into the builder's worktree; the builder's
   worktree is never opened by the reviewer under this architecture. Match the build cadence: one
   reviewer in the foreground for a solo build; one reviewer per ticket concurrently
   (`run_in_background: true`) for a fan-out, each dispatched as its builder's hand-off is verified —
   **subject to the same concurrency cap as everything else** (`CODE_MAX_CONCURRENT_AGENTS`): a
   reviewer that becomes dispatchable while the budget is already full from Phase 1 builders (or step
   0/1 sweeps still in flight) queues behind them and dispatches the moment a slot frees, rather than
   exceeding the cap.

   Pass the ticket id, e.g.:

   > Technically review lode-ai1 (it is `ready-for-code-review`): read `review_head` from bd, `git
   > fetch origin land/lode-ai1 trunk && git checkout -B land/lode-ai1 FETCH_HEAD` (`--detach` if
   > checked out elsewhere) into your own launch worktree, run `/code-review high --fix trunk...HEAD`
   > + `/simplify`, re-gate, commit, `git push origin HEAD:land/lode-ai1`, and swap the ticket to
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
   `code-reviewer` dispatched, and which were left alone for missing `review_head`. If the concurrency
   cap ever throttled dispatch this invocation (more dispatchable work than free slots at some point),
   say so — which cap value was in effect and roughly how the queue drained — so a fan-out that took
   longer than the ticket count alone would suggest isn't mistaken for a stall. On an auto-select run
   (no argument, `--all-ready`, or `--single`), also report **every ticket the `human`/epic filter
   passed over** — id + reason (`human`-labeled or epic). You did that filtering yourself in step 2 on
   all three paths, so report it directly. Say so explicitly **even when nothing was
   skipped** ("no `human`/epic tickets on the frontier"), so the operator can tell a filter that found
   nothing from a filter that never ran.

## Notes

- This skill is the **only** sanctioned way to spin up coding work from the main session, which is
  otherwise told not to spawn agents unprompted — invoking `/code` *is* the user asking. Fan-out is
  the *only* sanctioned way to run several producers at once (there is no `/code-parallel`) — and it
  is the *supported* way: **fan out within one invocation rather than starting a second concurrent
  `/code`.** See the topology note above [What to do when invoked](#what-to-do-when-invoked) — the
  step 0 / step 1 sweeps race across concurrent invocations (lode-pzr), because a ticket's
  `needs-rebase` / `ready-for-code-review` label is only cleared at the *end* of the agent dispatched
  at it, so a second invocation's sweep can still select it and double-dispatch onto the same
  worktree. Fan-out within a single invocation never hits this: each producer/reviewer pair is
  dispatched once, by that invocation's own Phase 1/2, for a ticket only that invocation resolved.
- **Two phases, in order: build then review.** Phase 1 (`coding`, Sonnet) builds and **keeps its
  worktree**; phase 2 (`code-reviewer`, Opus) fetches the pushed branch into its *own* launch worktree
  (never `EnterWorktree`, never the builder's worktree) and runs the technical review. The review must
  run on the *built* branch, so always dispatch the reviewer *after* its builder returns
  `ready-for-code-review` — never in parallel with its own build. Don't dispatch a reviewer for a
  ticket that escalated at build time.
- **Step 0's rebase pickup is a third mode, not a phase.** It reuses the `coding` subagent (its
  "Rebase pickup" cycle, distinct from its normal build cycle) but skips Phase 2 entirely — a
  `needs-rebase` ticket already passed technical review before `/land` kicked it back, so it goes
  straight to `ready-for-land` once current `trunk` is merged in. Never dispatch a
  `code-reviewer` for one. **Self-healing covers a clean merge and a *mechanical* conflict**
  (independent, non-overlapping additions the pickup resolves directly with `Edit`, since it works
  from its own checked-out worktree — lode-8k3); a conflict where the two sides *genuinely disagree*
  still escalates to a human. That's a deliberate judgment boundary, not a tooling gap — so this
  skill's own frontmatter claim of self-healing holds for the clean-merge and mechanical-conflict
  cases, but a real disagreement still needs a manual nudge, and always will. **The whole cycle —
  fetch, merge, re-gate, commit, push, label swap — is the dispatched `coding` producer's own job,
  start to finish (lode-cln):** it merges `origin/trunk` into the branch rather than rebasing onto
  it, so its push back to `land/<id>` is an ordinary fast-forward, never a rewrite — and that stays
  true after a conflict is resolved, since resolving changes the merge commit's tree, not its
  ancestry. Full reasoning:
  [`docs/agents-workflow.md`](../../../docs/agents-workflow.md#the-step-0-pickup-merges-it-never-rebases-lode-cln).
- **Step 1's stranded-review sweep is Phase 2 pulled forward, not a fourth mode.** It dispatches the
  exact same `code-reviewer` subagent, the same way, for the same reason — the only difference is the
  ticket was left `ready-for-code-review` by a *previous* invocation (a human's exit-(a) re-entry)
  rather than by this invocation's own Phase 1 (lode-t83). Its guard (`review_head` must be non-empty)
  exists because one setter of `land-escalated` — `coding`'s build-time escalation — used to skip
  recording that metadata entirely; that gap is fixed in `coding.md`, but the guard stays as
  defense-in-depth for tickets escalated before the fix landed. (The guard used to key on
  `review_worktree` under the earlier `git -C` architecture; the fetch-and-checkout architecture never
  opens that worktree at all, so `review_head` — the field the reviewer actually uses to check out and
  detect drift — is the correct thing to require instead, lode-k5e.)
- Producers do all repo mutation inside `isolation: "worktree"` worktrees and push to
  `origin/land/<id>`; **nothing merges and nothing the author wrote is reviewed by its author.** The
  main session stays on `trunk` and never edits files here. Landing those branches is `/land`'s job,
  not this skill's.
- If an argument is genuinely ambiguous (looks like it might be an ID but isn't one that exists, or a
  fan-out set with hidden dependencies), ask the user before dispatching rather than guessing.
