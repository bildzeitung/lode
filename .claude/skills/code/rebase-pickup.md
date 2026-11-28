# Rebase pickup — the `coding` agent's second job

The `coding` agent (`.claude/agents/coding.md`) runs this cycle when `/code` dispatches it at a
`needs-rebase` ticket. Below, "the fresh-build cycle", "the build cycle" and "above" in a reference
to either mean the producer cycle in [`coding.md`](../../agents/coding.md#the-producer-cycle). That
file's Non-negotiables, its "Bash call shapes" rule (the fenced blocks here are plain calls too) and
its other anti-patterns, some of them written for a rebase pickup, still bind one.

## Rebase pickup — needs-rebase kick-backs

I run a **second, distinct cycle** when `/code` dispatches me at a ticket that already carries
**`needs-rebase`** instead of a fresh `bd ready` claim. `/land`'s cheap conflict precheck (lode-bg3)
can kick a `ready-for-land` branch back: it strips `ready-for-land`, adds `needs-rebase`, and **keeps**
the same `land/<id>` branch and build worktree — the ticket stays `in_progress`, so it never surfaces
in `bd ready` and nothing else consumes the label. `/code` sweeps for it on every invocation (see its
`SKILL.md`) and hands me the ticket id, telling me this is a rebase pickup, not a build. When that's
my dispatch, I run this instead of ["The producer cycle"](../../agents/coding.md#the-producer-cycle) above:

### 1. Read the hand-off

```bash
bd show <id> --json     # confirm needs-rebase label; metadata.review_head is informational only;
                        # note metadata.land_summary -- step 5's one exception needs it
```

**Guard:** the ticket **must** carry `needs-rebase`. If it doesn't (already rebased, escalated, or
never kicked back), I stop and report — nothing to pick up.

### 2. Fetch `land/<id>` and check it out into my own launch worktree — never `EnterWorktree`, never the old build worktree

**Isolation guard (lode-ska2 / lode-jk44) — before even the recycled-worktree guard.** The same
harness `isolation: "worktree"` hand-off this cycle's own launch worktree came through has been
observed handing a dispatched agent NO worktree at all — cwd pinned to the main checkout at the repo
root, on `trunk` — a distinct, more severe failure than the recycled-worktree case below. This is the
first executable action of this cycle too, via `scripts/start-guards.sh` (lode-fg7w7.4), which runs
`scripts/isolation-guard.sh` (lode-ska2) and then, only if it passes, the recycled-worktree guard
described after this block, stopping at the first nonzero exit:

```bash
scripts/start-guards.sh "before my own fetch+checkout"
```

A nonzero exit is a **stop**. Exit 3 = the isolation guard exited nonzero (the recycled guard did not
run), exit 4 = isolation passed and the recycled guard exited nonzero. I report the guard's own
diagnostic and the wrapper's line below it, which names the guard's own status ([how to read
it](../../../docs/agents-workflow.md#precondition-guards-the-012-family-lode-t6ni)). An exit 127 /
"No such file or directory" is the **bootstrap gap** — this checkout may predate the script landing
on trunk — and is a stop too: report it and do not proceed.

On a failure here I stop — full stop, same as the fresh-build cycle: no `EnterWorktree` retry, no
`git worktree add` self-rescue, no fetch. See the fresh-build cycle's write-up of this same script for
the full rationale (lode-ska2); it doesn't repeat here.

**Recycled-worktree guard (lode-nt98) — run by that same wrapper call, before the fetch+checkout script below.** The same harness
`isolation: "worktree"` hand-off this cycle's own launch worktree came through has been observed
handing a dispatched agent a **recycled** worktree still checked out on a *previous* ticket's build
branch, carrying that ticket's commits, instead of a fresh branch off `origin/trunk` HEAD — confirmed in
production for both a fresh-build producer and a `code-reviewer` (lode-eshl's technical review; full
account in
[`docs/agents-workflow.md`](../../../docs/agents-workflow.md#recycled-worktree-guard-lode-nt98)). The `git checkout -B` that
`scripts/checkout-land-branch.sh` runs below *will* land me on the correct `land/<id>` regardless, so this guard is **not** what
makes the checkout correct. What it buys is a clean tree to work in: `checkout -B` carries **untracked**
leftovers from a recycled worktree straight through, and those go on to pollute my `git status` and the
`nox` run I gate on. So, before the fetch+checkout script, I assert this launch worktree actually started clean — via
`scripts/recycled-worktree-guard.sh` (lode-ivth), the same script the fresh-build cycle above uses,
which the wrapper call above already ran; there is no second call here.

**Both preconditions inside the script are load-bearing.** The `case` is what keeps
`reset --hard`/`clean -fd` off the user's main checkout if isolation ever fails to take — belt and
suspenders alongside the `isolation-guard.sh` run that the wrapper does first, which already refuses to let this cycle
get this far if cwd weren't inside a worktree at all. The `rescue/` branch
keeps another ticket's unpushed commits recoverable, since the ref being rewound is *theirs*, not mine
(see the fresh-build cycle above). The script's `git clean -fd` now runs unconditionally right after
the `case`/ancestor check, not just on a failed one (lode-3v1p) — so a recycled worktree whose HEAD
*is* an ancestor of `origin/trunk` (e.g. recycled onto a `land/<other-id>` that has since landed) still gets
its untracked leftovers swept before they can pollute my `git status --short` assertions and the `nox`
run; full reasoning in the script's own header and [docs/decisions.md](../../../docs/decisions.md)
(`grep -rn "lode-3v1p" docs/`). Exit 127 (missing) or 126 (not
executable) distinguishes a genuinely
missing/non-executable script (bootstrap gap — report and stop) from the wrapper reporting a guard's
nonzero exit (3 or 4, already explained by the guard itself).

This never conflicts with what step 2 does next — checking out `land/<id>` on purpose is exactly
this cycle's job, and this guard only cleans up the *starting* state before that intentional checkout
happens. If it fires, I report it explicitly in my final hand-off as live evidence of the harness bug,
not a routine hiccup.

The original build worktree (a leftover of an earlier `git -C` architecture, `docs/decisions.md`) is
not something I need or open — no metadata points at it any more since lode-2m89 retired
`review_worktree`/`review_branch`. I bring the branch to *my own* launch worktree instead, exactly like
the code-reviewer now does, so `Edit`/`Write`/`nox` all work natively and the whole guard question —
the isolation guard refuses to run any command resolved into a *path-entered* worktree (`"commands from
a worktree-isolated agent must run inside its worktree"`) — never comes up.

**Local branch name is always unique to this launch worktree — never the bare `land/<id>`**
(lode-em6v). Reusing `land/<id>` as the local name meant a second cycle on the same ticket (or a
leftover worktree from an earlier one that never cleaned itself up) collided with an already-checked-
out `land/<id>` elsewhere, forcing a `git checkout --detach` fallback — and a detached worktree owns no
branch ref, so back when `/land`'s worktree GC was still branch-name-keyed it structurally missed such a
worktree, and each leak made the next cycle more likely to hit the same fallback (self-compounding).
That GC is HEAD-sha-keyed now (lode-jiyk) and would reclaim a detached worktree too, but the collision is
still worth designing out at the source: suffixing the local name with this worktree's own directory name
makes it structurally impossible, so there is nothing left to guard for and the detaching fallback is
removed outright:

```bash
scripts/checkout-land-branch.sh <id>
```

The script derives the suffix from this launch worktree's own directory name (lode-qh1m), runs
`git fetch origin land/<id> trunk` itself (lode-n7g4, so no stale `FETCH_HEAD` can be checked out —
I run no separate `git fetch` before it), and takes the tip from `FETCH_HEAD`'s own `land/<id>` line,
not from whichever ref the fetch listed first; a nonzero exit is a stop (1 = cwd is not a
`.claude/worktrees/` launch worktree, 2 = a broken call, 3 = `FETCH_HEAD` has no `land/<id>` entry
after a successful fetch, 4 = the fetch failed, 5 = `git checkout -B` refused, which is not an
isolation failure). On exit 4 git's own error is printed above the
script's and nothing was checked out, so when that error is transient (e.g. a network failure) I
re-run the script once before treating it as a stop.

```bash
git rev-parse --abbrev-ref HEAD
```

The last call confirms I am off `trunk`, on `land/<id>--<worktree-suffix>`.

The suffixed name still starts with `land/`, but `/land`'s worktree-GC sweep doesn't look at the name at
all — it reclaims any worktree under `.claude/worktrees/` that is **unlocked** and whose **HEAD commit**
is already an ancestor of `trunk` (`git merge-base --is-ancestor`), so this worktree is reclaimed exactly
as it always was, once my build lands. That name-independence is scoped to the worktree loop only: `/land`'s
dangling-**ref** backstops still match `land/*` and `worktree-agent-*` by name (they must — `refs/heads/*`
is shared with human branches), and the `land/*` one strips this suffix before comparing against the
exact remote name — see `.claude/skills/land/SKILL.md`; nothing for me to do either way.

### 3. Merge current trunk in

```bash
git merge origin/trunk
```

A merge **appends** to history — it never rewrites a commit already pushed to `land/<id>` — which is
exactly why my push back in step 5 can be an ordinary, non-force push (lode-cln).

- **Clean merge** → the marker check below, then gates (step 4).
- **Conflict** → classify it before touching anything:
  - **Mechanical** (both sides add independent, non-overlapping content at the same anchor — e.g. two
    branches each appended a distinct section to the same doc, or added an unrelated function to the
    same file) → resolve it directly with `Edit` — I'm in my own worktree now, so the write goes
    through normally, no `bash` workaround needed. Re-read the resolved file to confirm the merge is
    what it looks like, `git add` it, and `git commit` to complete the merge. This is a genuine
    capability now, not a tool-guard consequence: I can write the fix, so I do, the same way I'd
    resolve any other conflict in my own worktree.
  - **Genuine disagreement** (the two sides changed the *same* content in incompatible ways, and
    picking one discards the other's intent) → `git merge --abort` and escalate (below). This
    stays a deliberate judgment boundary (lode-8k3) — a decision only a human should make, not a
    tooling limitation this fix removes.

After **any** merge here (clean or resolved) that brings a `**Update (` line into
`docs/decisions.md`, I confirm each such marker still follows the entry it corrects (lode-l8b7).
The `merge=union` driver can detach one when this branch holds an EOF append and trunk's marker is
on the entry above it: it writes this branch's lines first, then trunk's (`docs/decisions.md`,
search `lode-tikr`). When this branch has not changed that file there is nothing to check: the
merged file is trunk's. A marker's lead-in names the correcting ticket, not the entry, so the
merged file alone cannot show which entry it belongs on. I compare with trunk: the entry the
marker sits under in trunk's copy (`git show origin/trunk:docs/decisions.md`, searched for the
lead-in, never read whole) is the entry it must sit under in the merged file.

When one has been detached, I move **this branch's own added lines** (the ones the merge placed
between the entry and the marker) below trunk's added lines, changing no trunk line's wording or
order. I re-read that stretch of the file to confirm the order and run
`scripts/check-decisions-no-silent-rewrite.sh origin/trunk`: a `REMOVED:` line my move caused
means it disturbed a trunk line. Then I commit the move as an ordinary commit on top of the merge
and name it in step 5's `REBASE PICKUP` note.

### 4. Re-run the quality gates (must be green)

This cycle swaps the ticket straight to `ready-for-land` (step 5) with no further technical
review, so it runs the **reviewer/`/land`-level gate set — all three buckets** (lode-6ldh), not
the builder-level fast subset step 7 of the fresh-build cycle uses. Run directly in my own
worktree — no `-C`/`-f` needed, `cwd` already *is* the target tree — and the same FOREGROUND-only
rule from the non-negotiables applies here too: no `run_in_background`, no `Monitor`, read the
output in this turn.

```bash
uv run --frozen nox -t fix            # ruff format + lint (fixes in place)
uv run --frozen nox -s tests          # the tests bucket's FULL view (not `unit` -- this cycle
                                       # is the last gate before trunk, same bar as the reviewer)
uv run --frozen nox -t everything-else   # shellcheck + linkcheck + docstringcheck + decisions_entries + docs
scripts/validate-mermaid.sh           # only if a docs/ diagram is in the branch
```

Same `uv run --frozen` form as the fresh-build cycle above — guard-friendly, with nothing to set up
first (a fresh worktree builds its own `./.venv` on first use) and nothing to activate by hand.

If the `fix` session reformats anything, commit it — step 3 already completed the merge commit, so this is
an ordinary commit on top of it, not something folded into the merge. **Gates must be green before I
re-mark the ticket** — same bar as a fresh build.

### 5. Push and swap the label myself, then STOP

Because I merged `trunk` into the branch instead of rebasing onto it, my push back to `land/<id>`
never rewrites a commit that's already on origin — it's an ordinary fast-forward, so the whole cycle
stays mine to finish, start to end (lode-cln, full reasoning in
[`docs/agents-workflow.md`](../../../docs/agents-workflow.md#the-step-0-pickup-merges-it-never-rebases-lode-cln)).
This holds even when the merge conflicted and I resolved it: resolving changes the merge commit's
*tree*, never its ancestry, so the branch's already-pushed tip is still an ancestor and the push is
still a fast-forward.

**Before pushing, assert my worktree is clean — same rule as the build cycle's hand-off (lode-tpt):**

```bash
git status --short          # MUST be empty before pushing
```

If step 4's `nox -t fix` (or step 3's conflict resolution) left anything dirty, commit it now — a
gate that ran green against a tree I then push uncommitted content on top of proves nothing (lode-tpt).

Push straight to the ref that already exists on origin (no new branch, unlike a fresh build's `-u
origin HEAD:land/<id>` push to a ref that doesn't exist yet):

```bash
git push origin HEAD:land/<id>
```

An ordinary push — `HEAD` works regardless of what my local branch is named. Then refresh the
hand-off metadata and swap the label myself, with the same one-script derive → validate → write as the
fresh-build hand-off:

```bash
scripts/handoff-sha.sh <id> land_head --push --remove-label needs-rebase --add-label ready-for-land --append-notes "REBASE PICKUP: merged trunk into the branch; <clean merge, or each conflicting file and which side's content survived; plus any docs/decisions.md lines moved in step 3>"
```

Exit 0 is done; nonzero writes nothing (1 = the derived value is malformed, 2 = this CALL is broken,
3 = `bd update` failed, 4 = origin's `land/<id>` is not `HEAD`, 5 = `ls-remote` failed), with one
exception: 6 = the write WAS done but `--push`'s publish failed, so I run `scripts/bd-dolt-push.sh`
alone and never re-run the hand-off, which would append the note a second time. `--push` runs
`scripts/bd-dolt-push.sh` after the write (lode-fg7w7.5). The note is the only prose record of my
merge, and `land-review` reads it.

The push publishes the label swap + refreshed SHA over `refs/dolt/data`.

`land_head`/`land_summary` is the one field-name convention the whole loop uses — the same keys
`code-reviewer` sets when it first marks a ticket `ready-for-land`, and what `/land`'s 2a drift
precheck reads. I refresh `land_head` and **leave `land_summary` as the reviewer wrote it**: `/land`
builds the merge commit message from it (`Merge land/<id>: <land_summary> (<id>)`), so it must stay
a description of what the branch does, never a note about my trunk merge — that goes in the
`REBASE PICKUP` note above (lode-jb2q). **One exception:** if resolving a conflict removed or changed
something `land_summary` states (I kept trunk's deletion of a function the summary names, say), the
summary is now false and nothing downstream checks it, so I add `--set-metadata
land_summary="<the reviewer's summary, corrected to what the branch still does>"` to the same call.
I leave `review_head` untouched — it still correctly describes the original build, and
it's live, but its readers are *not* `/land`: they are the **code-reviewer** (which checks it out and
diffs it for drift) and **`/code`'s step-1 stranded-review guard** (which refuses a ticket whose
`review_head` is empty). `/land`'s 2a precheck reads `land_head`, the field I just refreshed above.
(`review_worktree`/`review_branch` no longer exist as of lode-2m89 — nothing writes or reads them.)

**I still do not remove the original build worktree.** It was never mine to remove, and I never even
opened it this cycle — `/land` GCs it on a clean land, same as always.

**My own launch worktree: I neither remove it nor report it (lode-vs7g).** This one *is* mine, but I
can't `git worktree remove` the worktree I'm standing in, so I don't try. `/code`'s orchestrating
session reclaims it right after I return — on **either** outcome (`ready-for-land` or
`land-escalated`) — and it *derives* which worktree was mine from the ticket id alone, since my branch
is `land/<id>--<my-own-worktree-dir>` (step 2). Nothing has to be handed back, which is the point: the
reclaim still works if I crash or escalate. By then this worktree holds nothing `origin/land/<id>`
doesn't already have — a clean pass pushed first (above), and an escalation's aborted merge leaves the
checkout an exact mirror of what was fetched — so removing it can never lose work.

I **stop** and report: which ticket, whether the merge was clean or which conflicts I resolved and
how, that the gates are green, the refreshed head SHA, and that it's back at `ready-for-land` — or, on
an escalation, which kind of conflict it was and why.

An **escalation** is different and stays mine: on a genuine-disagreement conflict I abort, leave the
branch exactly as it was (no push — nothing changed to push), and set `land-escalated` myself (step
3). That is a bd write, not a destructive git op. It's also exactly why `/code` must reclaim my
worktree without my help: an escalated branch never merges into `trunk`, so `/land`'s backstop 1 can
never reach it (lode-vs7g).

### Escalation — only a genuine conflict, not a mechanical one

A **mechanical** conflict (independent, non-overlapping additions) is resolved directly in step 3 —
that's a genuine capability under this architecture, not a tool-guard limitation, so it's no longer an
automatic escalation either. Only a **genuine disagreement** (the two sides changed the same content
in incompatible ways) escalates — that stays a deliberate policy choice (lode-8k3), not a consequence
of what `Edit` can reach. When it does, the branch is left exactly as it was (aborted, no push — never
stranded half-merged):

```bash
bd update <id> --remove-label needs-rebase --add-label land-escalated \
  --append-notes "ESCALATION (rebase pickup): git merge origin/trunk into land/<id> conflicts, and
the two sides genuinely disagree (not a mechanical, independent-addition conflict I can resolve
directly). Resolve manually and either re-push + reapply needs-rebase, or hand this to a human to
finish the merge."
scripts/bd-dolt-push.sh
```


### Anti-patterns (rebase pickup)

- **Rebasing instead of merging during a rebase pickup.** `git rebase origin/trunk` rewrites commits
  already pushed to `land/<id>`, which would need a force-push to land; merging instead keeps the
  push an ordinary fast-forward (lode-cln).
- **Force-pushing during a rebase pickup, or reaching for `--force`/`--force-with-lease` when a plain
  push is rejected.** A rejection means the remote moved since I fetched — re-fetch, re-merge, and
  retry; the merge design's entire point is that the push back is an ordinary fast-forward (lode-cln).
