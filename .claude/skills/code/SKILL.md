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
> **Before step 0**, compute the cap once and hold it for the rest of the invocation. The derivation
> itself lives in `scripts/code-concurrency-cap.sh` (lode-54mo) — extracted out of this file so it is
> testable (`tests/test_code_concurrency_cap.py`); ungated inline shell embedded in a SKILL.md is
> exactly where this repo already shipped a silent, undetected-for-months bug once before (lode-mh9g's
> `merge-tree` snippet). This file just calls it:
>
> ```bash
> CODE_MAX_CONCURRENT_AGENTS="$(scripts/code-concurrency-cap.sh)"
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
>   without reading that section. This paragraph is explanation and may lag; `scripts/code-concurrency-cap.sh`
>   is the executable source of truth and wins on any disagreement.
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
   > **merge current `trunk` in** (do not rebase): `git fetch origin land/lode-ai1 trunk`, then
   > `TOP=$(git rev-parse --show-toplevel)` and `git checkout -B "land/lode-ai1--${TOP##*/}"
   > FETCH_HEAD` (a local name suffixed with your own launch worktree's directory — unique by
   > construction, so it never collides with a leftover checkout and the old `--detach` fallback is
   > never needed), `git merge
   > origin/trunk`, re-gate (`nox -t fix` / `nox -s tests`), commit anything the gate loop produced,
   > then `git push origin HEAD:land/lode-ai1` (an ordinary push by explicit refspec — the merge only
   > appends, it never rewrites what's already on `land/lode-ai1`), refresh `land_head`/`land_summary`,
   > and swap `needs-rebase` straight to `ready-for-land` yourself. Do not merge, close, or push trunk.
   > On a merge conflict: if both sides added independent, non-overlapping content (a **mechanical**
   > conflict), resolve it directly with `Edit` and continue, then finish the same way; if the two
   > sides genuinely **disagree**, abort the merge and escalate yourself (`land-escalated`, leave the
   > branch as it was) rather than guess — that stays a human decision. Don't try to remove your own
   > launch worktree on the way out — you can't remove the one you're standing in, and I reclaim it for
   > you after you return.

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

   <a id="reclaim"></a>
   **Reclaim its launch worktree the moment it returns — either outcome (lode-vs7g).** A subagent
   cannot `git worktree remove` the worktree it is standing in, so **I** do it, from my own (repo-root)
   context, immediately after collecting its result — not batched to the end of the fan-out, and not
   left for the ticket to land. **This same block is the reclaim referenced by step 1 and Phase 2
   below; it is the only copy.** I don't need the agent to tell me *which* worktree was its own — since
   lode-em6v every reviewer / rebase pickup checks the branch out as `land/<id>--<its-own-worktree-dir>`,
   so the ticket id alone **derives** both the path and the branch:

   ```bash
   ID=lode-ai1   # the ticket I just dispatched at
   git worktree list --porcelain | awk '
     /^worktree /{p=$2} /^branch /{sub("refs/heads/","",$2); print p"\t"$2}' \
   | while IFS="$(printf '\t')" read -r WT BR; do
       case "$BR" in "land/$ID--"*)
         git worktree remove --force "$WT" 2>/dev/null   # single -f: fails SAFE if still locked
         git branch -D "$BR" >/dev/null 2>&1 || true ;;  # worktree first — git won't delete a
       esac                                              # branch that's still checked out
     done
   ```

   Deriving rather than trusting a reported string is what makes this **actually** close the leak: it
   needs no cooperation from the agent, so it works even when the agent crashed, escalated, or returned
   a garbled path — and it reclaims **every** worktree that ticket accumulated (a ticket reviewed or
   picked up across N cycles leaves N of them), not just the last one. It cannot touch the **builder's**
   worktree: that one is branch-named `worktree-agent-*`, never `land/<id>--*`, so it stays for `/land`
   to reclaim on a clean land — via `/land`'s end-of-pass backstop sweep, which since **lode-h1vn** owns
   *all* local worktree GC (it discovers worktrees live off `git worktree list --porcelain`; the old
   per-ticket loop that keyed off `review_worktree` is gone).

   Two details that are load-bearing, both verified against live `git` behaviour:
   - **Plain `git`, not `rtk`** — `rtk` reformats `worktree list --porcelain`, which breaks the field
     parse, the same way it did for `/land`'s own GC (lode-9j7).
   - **A single `--force`, never `-f -f`.** The harness *locks* a launch worktree while its agent runs
     (`locked claude agent <name> (pid …)`) and unlocks it on exit. A single `--force` therefore removes
     a finished agent's worktree but **refuses** a still-locked one — it fails safe. `-f -f` would
     override the lock and rip the worktree out from under a live agent; if a reclaim ever looks like a
     no-op, the agent is still running, and the answer is to wait, not to escalate the flag.

   Safe on **both** outcomes: by the time the agent returns, everything in its worktree is already on
   `origin/land/<id>` — a clean pickup pushes first, and an escalation's aborted merge leaves the
   checkout an exact mirror of what was fetched. Reclaiming here rather than leaving it to `/land`
   matters most on an **escalation**: that branch never merges into `trunk`, so backstop 1 — which only
   reclaims a *merged*-into-`trunk` worktree — can **never** reach it, and it would otherwise leak until
   a human resolves the escalation and the branch eventually lands. `/land`'s backstops stay in place,
   unchanged; note they are a *partial* net, not a total one — they catch a crashed agent whose branch
   does eventually merge, but a crashed *escalation* is reachable only by the derived reclaim above,
   which is the other reason it must not depend on the agent reporting anything.

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
   sweep finds nothing, say so and move on; it's not an error. **Reclaim each reviewer's launch
   worktree the moment it returns** — [step 0's reclaim block](#reclaim), `ID` set to that ticket
   (lode-vs7g). This sweep dispatches the identical subagent, so the identical reclaim applies.

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

   > **Auto-select paths only — also exclude children of an un-debated epic (lode-bw5k).** `/challenge`
   > is the intended stress-test gate a plan/epic should pass **before** its children get built, but
   > nothing enforced that — `lode-olmi`'s children were built and landed without the epic ever having
   > been debated, caught only by a human noticing after the fact. So, for **every candidate that
   > survives the `human`/epic filter above** (same three auto-select paths, same exclusion of
   > explicitly-named IDs — an operator naming a ticket is never second-guessed here either), run:
   >
   > ```bash
   > scripts/epic-debate-gate.sh <candidate-id>
   > ```
   >
   > It prints `BUILD <id>` (no parent epic, or the parent epic already carries the `epic-debated`
   > label `/challenge` stamps when it debates an epic — `.claude/skills/challenge/SKILL.md`) or `SKIP <id>
   > epic not debated (<epic-id>)`. Keep every `BUILD` in the buildable set; **report every `SKIP`** —
   > id + reason (`epic not debated (<epic-id>)`) — in step 5's skip list, right alongside the
   > `human`/epic skips. The script only reads (`bd show`, twice at most per candidate — the ticket,
   > then its epic if it has one); it never writes bd state, and it derives the parent epic from the
   > candidate's `dependencies[]` (a `parent-child` entry whose target has `issue_type: epic`) —
   > because it needs the epic's own `issue_type`/labels, which that entry embeds. The top-level
   > `.parent` scalar is also populated and equally reliable (lode-v4rk), and is what
   > `scripts/epic-completion-check.sh` uses; only `parent_id`/`epic_id` are null. See
   > [docs/agents-workflow.md](../../../docs/agents-workflow.md) — "Two derivations".
   >
   > **No new escape-hatch flag.** The unblock is to actually debate the epic (`/challenge <epic-id>`,
   > cheap) or hand-apply the `epic-debated` label to acknowledge it was debated informally — both
   > leave the same durable marker this gate reads, so there is nothing else to build.
   >
   > **Scope: this gate runs only inside step 2's auto-select filtering, exactly like the `human`/epic
   > filter above.** It never applies to step 0's `needs-rebase` pickups or step 1's stranded
   > `ready-for-code-review` re-entries — both pick up tickets already mid-flight, past this gate, and
   > re-gating them here would strand in-flight work behind a retroactively-applied check.

3. **Phase 1 — dispatch one `coding` builder per task** via the Agent tool with
   `subagent_type: "coding"` **and `isolation: "worktree"`**. The isolation is required: a subagent is
   pinned at the repo root and **cannot** call `EnterWorktree` to *create* its own, so the harness must
   hand each builder a worktree at dispatch — `isolation: "worktree"` launches it already cwd'd inside
   `.claude/worktrees/agent-<hash>` on its own branch off `trunk` HEAD.

   > **Claim each resolved ticket from *here*, before dispatch — don't rely on the builder to do it
   > (lode-xr8v).** `coding.md` step 2 also runs `bd update <id> --claim`, but that is an unverified
   > soft instruction a builder under load can skip — and nothing downstream catches it: Phase 2's
   > hand-off verification (below) checks *labels* and the *remote branch*, never `status`. A skipped
   > claim was observed to carry a ticket all the way to `ready-for-land` while it stayed `open` with a
   > `null` assignee (lode-gpzn.2), which means it sat in `bd ready` for its whole build (the claim's
   > "atomic, prevents double-work" protection lost) **and** step 1's stranded-review sweep — which
   > filters on `--status in_progress` — would be blind to it on an escalation. So for **every dispatch
   > path where the id is known at dispatch** — an explicitly-named id, several named ids, an
   > auto-selected id (bare `/code` / `--all-ready` / `--single`), each ticket in a fan-out — claim it
   > from the orchestrator's own (repo-root) context **before** the Agent dispatch:
   >
   > ```bash
   > rtk bd update <id> --claim     # sets in_progress + assignee; deterministic here, one controlled flow
   > ```
   >
   > This is the same local Dolt DB the builder sees, so the claim is visible to it immediately — no
   > `bd dolt push` needed here (the builder's hand-off push carries it onward). The builder's own
   > step-2 claim then becomes an **idempotent backstop** (a second `--claim` is a verified no-op) and
   > stays the *primary* claim on the **one path with no id at dispatch — free-text**, where `/code`
   > names a task, not a ticket, and the builder files the issue and claims it itself. Claiming here is
   > what actually makes the `in_progress` invariant that steps 0/1 and Phase 2 assume hold true, rather
   > than assumed.

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
   > `origin/land/<id>` → mark `ready-for-code-review`, recording `review_head` →
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
   > fetch origin land/lode-ai1 trunk`, then `TOP=$(git rev-parse --show-toplevel)` and `git checkout
   > -B "land/lode-ai1--${TOP##*/}" FETCH_HEAD` (a local name suffixed with your own launch
   > worktree's directory — unique by construction, so no `--detach` fallback is ever needed) into your
   > own launch worktree, run `/code-review high --fix trunk...HEAD` + `/simplify`, re-gate, commit,
   > `git push origin HEAD:land/lode-ai1`, and swap the ticket to `ready-for-land`. Do **not** merge,
   > close, or push trunk. Escalate (revert to green, swap to `land-escalated`, don't mark ready) only
   > on a clarifying decision or "making it worse."

   **Reclaim its launch worktree the moment it returns — either outcome (lode-vs7g).** Run [step 0's
   reclaim block](#reclaim) with `ID` set to this ticket, right after collecting the reviewer's result
   (per ticket, not batched to the end of the fan-out). Nothing needs to be passed back for this: the
   reviewer's branch is `land/<id>--<its-own-worktree-dir>`, so the ticket id derives both the worktree
   and the branch. Everything in that worktree is already on `origin/land/<id>` by the time the reviewer
   **returns** — a clean pass pushes at step 7 of `code-reviewer.md`, and an escalation re-pushes its
   reverted-to-green commit too, so nothing local is ever lost. It matters most on an escalation: that
   branch never merges into `trunk`, so `/land`'s backstop 1 can never reclaim this worktree.

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
   nothing from a filter that never ran. Mention the launch-worktree reclaims (lode-vs7g) only as a
   one-line tally ("reclaimed N reviewer/pickup worktrees") — it's routine housekeeping, not a caveat;
   report it *individually* only where one failed to reclaim, which means an agent is somehow still
   running and is worth surfacing.

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
- **A reviewer's or rebase-pickup's own launch worktree is reclaimed by me, right after it returns —
  not left for `/land`'s backstops (lode-vs7g).** Mechanism and rationale live in one place: [step 0's
  reclaim block](#reclaim). The one thing worth repeating here is what it must **not** touch — a *fresh
  build*'s worktree, which is branch-named `worktree-agent-*` (never `land/<id>--*`, so the derived
  reclaim skips it by construction) and is deliberately kept through the whole build → review → land
  lifecycle (`docs/decisions.md`), since `/land` reclaims it on a clean land — via its end-of-pass
  backstop sweep, which since **lode-h1vn** owns all local worktree GC (the per-ticket `review_worktree`
  loop is deleted; a landed builder worktree's HEAD is an ancestor of `trunk`, so the backstop catches
  it). `/land`'s backstops stay exactly as they were.
