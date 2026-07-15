---
name: land
description: Drain the ready-for-land queue — the SINGLE owner of every write to `trunk`. Per pass: cheap-precheck each `ready-for-land` branch (drift + does it still merge onto `trunk` — a conflict is kicked back `needs-rebase`, no review spent); semantic-review the survivors (via the `land-review` skill) → accept | bounce | escalate; batch-merge the accepted set `--no-ff` into `trunk`, re-gate once, isolate the culprit on red; then push `trunk`, `bd close` the landed tickets, flag any epic whose last child this pass closed with `epic-ready-to-audit` (for `/epic-audit`), `bd dolt push`, and GC the merged `land/<id>` branches and the local builder worktrees. Bounces open a new linked ticket carrying the findings; escalations leave the branch for a human and land nothing. Run self-paced as `/loop 5m /land` on ONE machine; a local lockfile guard skips a tick that would overlap a still-running land. Producers (`/code`) never land their own work — this skill does. Examples — "/land", "/loop 5m /land", "drain the ready-for-land queue", "land the reviewed branches".
---

# land

I am lode's **lander** — the **single, sole owner of every write to `trunk`**. Producers (`/code` →
`coding`) build reviewed, green branches, push them to `origin/land/<id>`, and mark their ticket
`ready-for-land`; they **never** merge, close, or push `trunk`. I am the other half of that contract:
I drain the `ready-for-land` queue, and **nothing reaches `trunk` except through me.** The whole
design lives in
[`docs/agents-workflow.md` — the landing loop](../../../docs/agents-workflow.md#the-landing-loop--build-review-land-planned)
(read it; "The lander" and "Mechanics (decided)" are the source of truth) and the decided mechanics
in [`docs/decisions.md`](../../../docs/decisions.md). Where this skill and `CLAUDE.md` disagree,
**`CLAUDE.md` wins** — surface the drift instead of diverging.

I run on the **main checkout, on `trunk`** — I am the *one* agent allowed to. (Producers are the
inverse: they may never touch `trunk`. I never touch a producer's worktree.) I am typically invoked
self-paced as **`/loop 5m /land`** so I drain the queue while you work, with no daemon to manage.

**Run me from an Opus session.** I am a skill, not a subagent — I have no model of my own and inherit
the model of the session that runs the `/loop 5m /land`. My semantic review and combined re-gate are
exactly where Opus judgment earns its keep, so don't `/fast` the lander (the `coding` builder is the
one that runs cheaper on Sonnet; the `code-reviewer` and I stay on Opus).

## The merge decision belongs to the agent that didn't write the code

My **first task per branch is a semantic review I do not perform myself** — I dispatch the
[`land-review`](../land-review/SKILL.md) skill (the build-side twin of `challenge`). The independence is
the point: the producer already ran the *technical* review (`/code-review` + `simplify` = bugs &
cleanup) on its own branch with gates green; I add the *semantic* gate — *should this land?* — from
the outside. I do **not** re-run the technical review and I assume the branch is green until my
re-gate says otherwise.

---

## 0. Single-lander lock — acquire FIRST, every tick

Being the **single** lander is what serializes landing. v1 guarantees that with **(a)** a local
"skip if already running" lockfile guard and **(b)** the convention that the `/land` loop runs on
**one machine**. (The distributed `refs/locks/land` ref for true concurrent multi-machine landing is
a deferred upgrade, recorded in `docs/decisions.md` — **not** v1.)

Before doing anything else, take the local lock. If another `/land` is still running on this machine
(a long pass overrunning a `/loop` tick), **skip this tick cleanly and exit 0** — do not queue, do
not run in parallel:

```bash
LOCK="$(rtk git rev-parse --git-dir)/land.lock"   # under .git/ — per-machine, never committed
if ! ( set -o noclobber; printf '%s\n' "$$ $(hostname) $(date -u +%FT%TZ)" > "$LOCK" ) 2>/dev/null; then
  # Lock exists. Reclaim only if its owner PID is dead (a crashed prior land), else skip.
  OWNER_PID=$(awk '{print $1}' "$LOCK" 2>/dev/null)
  if [ -n "$OWNER_PID" ] && kill -0 "$OWNER_PID" 2>/dev/null; then
    echo "land: another /land (pid $OWNER_PID) is still running — skipping this tick."; exit 0
  fi
  echo "land: clearing stale lock from dead pid $OWNER_PID"; rm -f "$LOCK"
  ( set -o noclobber; printf '%s\n' "$$ $(hostname) $(date -u +%FT%TZ)" > "$LOCK" ) 2>/dev/null \
    || { echo "land: lost lock race — skipping this tick."; exit 0; }
fi
trap 'rm -f "$LOCK"' EXIT   # always release, including on error/escalate exit
```

`set -o noclobber` makes the `>` redirect fail atomically if the file already exists, so two ticks
can't both think they own it. The `trap` releases the lock on **every** exit path. **Convention:**
run the `/land` loop on **one machine only** — the local lock does not cross machines.

---

## 1. Setup the pass — Dolt-authoritative, fetch origin

I am the heaviest bd **writer** in the system (many closes, plus bounce-ticket creates, interleaved
with git merges and pushes), so I follow the bd-sync discipline strictly (see
[bd-sync discipline](#bd-sync-discipline-non-negotiable) below). At the start of each pass:

```bash
rtk bd dolt pull            # Dolt is authoritative; pull the latest claim/label/close state over refs/dolt/data
rtk git -C "$(rtk git rev-parse --show-toplevel)" checkout trunk   # I land ON trunk, in the main checkout
rtk git fetch origin        # I need origin/trunk and every origin/land/<id> fresh
rtk git pull --rebase       # bring local trunk current with origin/trunk before I merge into it
```

Then read the queue — every ticket carrying the **`ready-for-land`** label (it stays `in_progress`;
the label, not a status, is the queue):

```bash
rtk bd list --label ready-for-land --status in_progress --json
```

If the queue is empty, there is nothing to land: release the lock and stop. Otherwise process the
batch.

---

## 1a. Compute the stacked-branch graph — once per pass, from git, never from bd

A producer sometimes must build one `land/<id>` branch **on top of** another still-unlanded
`land/<base>` branch — merging it in — because its ticket only makes sense once the base's code
exists (OBSERVED: lode-6qh / lode-96t — lode-96t was the error-handling fix *for* a command lode-6qh
introduced that was not yet on `trunk`; its branch merged `land/lode-6qh` to have something to fix).
Full contract: [docs/agents-workflow.md — Stacked land
branches](../../../docs/agents-workflow.md#stacked-land-branches-lode-02v). Nothing about a stacked
branch's *content* announces this — I detect it purely from **git history**. `coding.md` records a
`builds_on` bd field as a breadcrumb when it builds this way, but that is redundancy and intent
only — I never trust it as the mechanism; a producer that forgets to write it, or writes it wrong,
must not silently break this section.

**Detection — shared history off `trunk`, NOT tip-ancestry.** Two land branches cut independently from
`trunk` have nothing but `trunk` in common. So the relation to test is: **does any of their merge-bases
lie off `trunk`?** If one does, the pair **shares non-trunk history** — and that shared commit is a
base's tip *at the moment a dependent merged it*.

**Shared history is necessary, not sufficient — the direction test is what decides.** An off-trunk
merge-base means one of *two* things, and only the first is a stack:

- **A stack** — one of the pair merged the other. The direction test below finds the shared commit on
  the base's first-parent spine but not the dependent's, and emits the edge.
- **Siblings** — *two dependents that each merged the same third base* also share that base's commits,
  so they too have an off-trunk merge-base with **each other**, while neither is stacked on the other.
  Here the shared commit is off *both* spines (each reached it through a merge), the direction test
  matches neither ordering, and **no edge is emitted — which is the correct answer.** Each sibling is
  still correctly detected as stacked on the *base* by its own pair. This is a normal, supported shape
  (two producers may stack on one base); a related pair with no direction between them is not, by
  itself, a symptom of anything wrong.

```bash
rtk git for-each-ref --format='%(refname:short)' 'refs/remotes/origin/land/*'
# for every ORDERED pair (X, Y) among the listed refs:
# ENUMERATE ALL merge-bases — a pair can have more than one (see below) — and keep only the
# off-trunk ones. A base branch that later takes a needs-rebase trunk-merge pickup (lode-cln)
# AFTER a dependent has already merged it acquires a SECOND merge-base: the dependent's own
# trunk cut point, which IS an ancestor of trunk. The single-result `git merge-base` picks one
# of the two ARBITRARILY, and when it happens to return the on-trunk one, the pair reads as
# unrelated and the stack goes undetected. `--all` sees every candidate; discarding the
# on-trunk ones and keeping any survivor is what makes this immune to that flow.
OFF_TRUNK=""
for mb in $(rtk git merge-base --all "origin/land/<X>" "origin/land/<Y>"); do
  rtk git merge-base --is-ancestor "$mb" origin/trunk || OFF_TRUNK="$OFF_TRUNK $mb"
done
[ -z "$OFF_TRUNK" ] && continue   # every merge-base is on trunk → unrelated
# At least one off-trunk merge-base → X and Y share non-trunk history. That is EITHER a stack OR two
# siblings on a common base; the direction test below is what tells them apart. Emitting no edge here
# is a normal outcome (siblings), not a failure.
# DIRECTION: the BASE is the one whose own first-parent spine contains an off-trunk MB — the
# dependent reached that commit through a merge (second parent), so it is not on its spine.
for mb in $OFF_TRUNK; do
  rtk git rev-list --first-parent origin/trunk..origin/land/<X> | grep -qx "$mb" \
    && ! rtk git rev-list --first-parent origin/trunk..origin/land/<Y> | grep -qx "$mb" \
    && echo "<Y> is stacked on <X>" && break
done
```

**Do NOT reduce this to `git merge-base --is-ancestor origin/land/<X> origin/land/<Y>`** (i.e. "is X's
current tip contained in Y"). That tests the base's **tip**, and a base's tip *moves after a dependent
merges it* — by an ordinary, entirely legitimate fast-forward: the code-reviewer pushes review fixes
onto `land/<base>` ([`code-reviewer.md`](../../agents/code-reviewer.md)), and a `needs-rebase` pickup
merges `trunk` into it (lode-cln). Both leave the dependent holding the base's *older* commits, so the
base's current tip is no longer an ancestor of the dependent and **the whole stack goes invisible** —
silently, with no error. That is not a corner case; it is the *normal* flow, because a producer stacks
on a base precisely while that base is still unlanded and therefore still moving. A detector that
misses the stack is worse than no detector, because the rest of this section trusts it and goes right
back to stranding dependents. But note the merge-base test above is immune to a fast-forward on either
side **only when every merge-base is considered, not one** — a single-result `git merge-base` walks
straight back into this same silent miss by a different route, for the reason spelled out in the
`--all` comment in the snippet above. Immunity comes from `--all` + the off-trunk filter, not from
using a merge-base per se.

Build this **once**, right here, as an in-memory map for the rest of the pass — never persisted, never
trusted from a prior pass (a branch can be bounced, dropped, or landed between passes, changing what's
live). Two shapes of the same relation get used below:

- **Full relation** (every base of Y, direct *or* transitive) — used by [Bounce](#bounce--clear-failure)
  and the [exit-(b)/(c) resolution paths](#resolving-a-land-escalated-branch) to ask "does deleting X
  strand a live descendant?" A transitively-stacked branch inherits X's content just as much as a
  directly-stacked one, so the strand check must not reduce to direct edges only.
- **Direct edges only** (X is Y's *nearest* base: no other base `B'` of Y has X as one of *its* bases —
  computed from the relation itself, never from tips, which move) — needed by
  [2c](#2c-run-the-semantic-gate) to pick the one base `land-review` diffs a stacked branch against:
  handing it a *transitive* base would reintroduce hole 2, because the diff would then carry the
  intermediate branch's work as if it were this branch's. [Section
  3a](#3a-order-the-accepted-set--base-before-dependent-hold-an-orphaned-dependent) uses the same view
  to order the merge set, though the topological sort would in fact run correctly off the full relation
  too (its extra transitive edges are already-implied constraints, not new ones) — the nearest-base
  pick is the reason this view has to exist.

**Known gap — documented, not claimed airtight.** The merge-base test survives any *append* to either
branch, but not a **rewrite**: if a base's history were force-pushed after a dependent merged it, the
shared commit is gone from the base's history entirely, the merge-base falls back to `trunk`, and the
pair reads as unrelated — the dependent still carries the base's old, now-orphaned commits. Nothing in
the current architecture force-pushes a `land/<id>` branch (every push on these branches is an ordinary
fast-forward), so this is a defense against a *future* change or a manual force-push, not a
live trigger today. There is no fully general fix short of every dependent re-checking after every base
push, which this ticket deliberately does not build (documented-YAGNI: this has happened once). **If a
`land/<id>` branch is ever force-pushed, the stacked-branch graph for that pass is not trustworthy** —
that is the honest limit of this mechanism, and it is stated here rather than papered over.

**A second known gap, same honest register: branched-from-base, not merged-base.** The direction test
assumes the dependent *merged* the base, so the shared commit sits on the base's first-parent spine
but not the dependent's. A producer that instead **branches directly off `land/<base>`** (rather than
branching from `trunk` and merging the base in, as `coding.md` instructs) puts that shared commit on
*both* branches' first-parent spines — the direction test finds it on both sides, matches neither
half of the `&&` condition, and emits no edge at all. Detection (the off-trunk-merge-base test) still
correctly flags the pair as related; only the *direction* is silently lost. `coding.md`'s sanctioned
build flow (branch from `trunk`, merge the base in) never produces this shape, so it is not a live
trigger under the current architecture — same status as the force-push gap above: a defense against a
future or off-process deviation, not something this ticket builds a general fix for.

**And note it is not distinguishable from a sibling pair by signature alone**: both show up as "related
(off-trunk merge-base) but no edge." That is exactly why this stays a *documented gap* rather than
something to detect and warn on — a warning keyed on that signature would fire on every perfectly
normal sibling pair. If a stack is ever suspected but no edge appears, the thing to check by hand is
whether the dependent's first-parent spine reaches `trunk` at all, or dead-ends in the base.

---

## 2. Vet each branch — cheap prechecks first, then the semantic review

For **each** `ready-for-land` ticket, in this order. Steps 2a and 2b are **cheap gates I run before
spending Opus on the semantic review** — a branch that has drifted or no longer merges cleanly is
disqualified on mechanics alone, so I don't burn a `land-review` verifying contents that are about to
be thrown back.

### 2a. Re-validate that beads and git haven't drifted

The landing context is **minimal by design** — `land_head` + a one-line `land_summary` in bd
metadata, read via `bd show <id> --json` (the branch name is *derived*, `land/<id>`, never stored).
`land_head`/`land_summary` is the one field-name convention across the whole loop: `code-reviewer`
writes it when swapping a ticket to `ready-for-land`, and a rebase pickup (`coding.md`) refreshes it
on every re-push. The SHA exists only to **detect drift**: a push onto the branch *after* the ticket
was marked ready.

```bash
rtk bd show <id> --json     # read metadata.land_head and metadata.land_summary
rtk git ls-remote origin "refs/heads/land/<id>"   # branch must still exist on origin...
# ...and origin/land/<id>'s tip SHA must equal metadata.land_head
```

A **missing branch** or a **SHA mismatch** is drift — treat it exactly like a review **bounce**
(below): I will not land a branch I can't verify is the reviewed one.

### 2b. Cheap conflict precheck — does it still merge onto `trunk`?

A branch that forked long ago is **not** stale-in-a-way-that-matters as long as it still merges
clean: `git merge --no-ff` integrates non-linear history fine, and the combined re-gate in
[Section 3](#3-batch-merge-the-accepted-set-re-gate-once-isolate-on-red) re-runs the tests on the
*merged* `trunk`. So a stale-but-clean branch needs no rebase and no special handling. What *does*
disqualify a branch is a **textual conflict** with current `trunk` — and discovering that only at
merge time (Section 3) means I've already paid for a full `land-review` on contents the rebase will
change. So I test it cheaply, up front, with a no-checkout trial merge against current `trunk`
(the pass already brought local `trunk` current with `origin/trunk` in Section 1, and I have not
merged anything yet this pass, so `origin/trunk` is the right base for every branch here):

```bash
# git merge-tree --write-tree exits 0 on a clean merge, non-zero on conflict — no working tree touched.
# (Requires git >= 2.38.)
if MT=$(rtk git merge-tree --write-tree --name-only origin/trunk "origin/land/<id>" 2>/dev/null); then
  :                                        # clean — proceed to the semantic gate (2c)
else
  CONFLICTS=$(printf '%s\n' "$MT" | tail -n +2)   # merge-tree lists the conflicting paths after the tree OID
  # → needs-rebase kick-back (see "Needs rebase — kick back"): skip land-review, leave the merge set.
fi
```

A conflict here is **neither a bounce nor an escalate** — the branch's *content* may be perfectly
fine, it simply can't replay onto where `trunk` now is. I handle it per
[Needs rebase — kick back](#needs-rebase--kick-back): remove `ready-for-land`, add `needs-rebase`,
**keep the branch and the build worktree**, and move on — **without dispatching `land-review`**. The
branch leaves this pass's merge set and the producer rebases it (this is where the noise went).

### 2c. Run the semantic gate

Dispatch the [`land-review`](../land-review/SKILL.md) skill with the ticket ID and its `land/<id>`
branch. **If [1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)'s
direct-edge map found this ticket stacked on exactly one live base**, also pass that base
(`land/<base-id>`) — land-review diffs against it instead of `trunk` (its own
[docs/agents-workflow.md#stacked-land-branches-lode-02v](../../../docs/agents-workflow.md#stacked-land-branches-lode-02v)-linked
handling). If it found *no* live base, or (the rare nested/multi-base case) more than one direct
base, hand land-review nothing extra and let it default to a `trunk` diff — but in the multi-base
case, note in the dispatch which other live land branches this one contains, so land-review doesn't
misread their content as scope creep even without a clean single base to diff against.

land-review reads both sides (ticket acceptance/design vs. the actual diff against the right base),
judges on acceptance / scope / design+invariants / approach, and returns exactly one verdict with
findings:

- **accept** → add the ticket to the **merge set** for this pass.
- **bounce** (a clear, confident failure) → handle per [Bounce](#bounce--clear-failure) below: open a
  new ticket carrying the findings, supersede the original, **drop the branch** — unless doing so
  would strand a live descendant (the bounce section's own descendant check), in which case it
  escalates instead. The ticket leaves the merge set either way.
- **escalate** (a genuine decision only a human can make) → handle per
  [Escalate](#escalate--genuine-decision) below: land **nothing** for it, **keep the branch**, label
  it, surface the question. It never enters the merge set.

Collect verdicts for the whole queue before merging — I want the full accepted set so I can
**batch**-merge.

---

## 3. Batch-merge the accepted set, re-gate once, isolate on red

Two branches each green *in isolation* can break when **combined** (a clean git merge with broken
behaviour). So I merge the whole accepted set, then re-gate the combined `trunk` **once**.

### 3a. Order the accepted set — base before dependent; hold an orphaned dependent

An unordered merge set is unsafe for a stacked branch: merging a dependent *before* its base drags
the base's unreviewed content onto `trunk` under the wrong ticket's name, and a dependent whose base
never made it into this pass's accepted set (bounced, escalated, kicked back `needs-rebase`, or
simply not yet at `ready-for-land`) must not land at all this pass — its base isn't on `trunk` yet.

Using [1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)'s **direct
edges**, restricted to `$ACCEPTED`:

- For each accepted `id` stacked on a direct live base `B`:
  - **`B` is also in `$ACCEPTED`** → `id` must merge *after* `B`. A plain topological sort (Kahn's
    algorithm — repeatedly take an accepted id whose bases are all already ordered) handles any depth
    of stacking from these direct edges alone; nothing deeper is needed.
  - **`B` is not in `$ACCEPTED`** → **hold** `id`: pull it out of this pass's merge set entirely (it
    does not merge, conflict-isolate, or bounce this pass), and leave a note so it's visible next
    pass:

    ```bash
    rtk bd update "$id" --append-notes "HELD (/land, stacked-branch ordering): land/$id is stacked on
    land/$B, which is not landing this pass ($B's own outcome: <bounced|escalated|needs-rebase|not yet
    ready-for-land>). Re-evaluated automatically once $B lands or its own outcome resolves — no action
    needed unless $B itself needs a human decision."
    ```

    (No `bd dolt push` needed here in isolation — this note rides along with the pass's other
    publishes.) `id` stays `ready-for-land`; it simply re-enters `land-review` next pass, by which
    point either `B` has landed (so `id`'s own trunk-diff now naturally excludes `B`'s content — see
    [docs/agents-workflow.md](../../../docs/agents-workflow.md#stacked-land-branches-lode-02v)) or the
    hold note explains why it's still waiting.

Once ordered (and any orphaned dependents pulled out), `$ACCEPTED` below refers to this **ordered,
possibly-reduced** set, not the raw land-review output.

**The invariant that outlives this step: a base that leaves the merge set takes its dependents with
it.** Ordering the set up-front is not sufficient, because a base can still drop *out* of it later in
Section 3 — by a real merge conflict (kicked back `needs-rebase`, the loop `continue`s) or by turning
the gate red during isolation (bounced). In both cases the loop would carry straight on to a dependent
that is still in `$ACCEPTED`, merge it, and land the departed base's un-landed, just-rejected content
onto `trunk` under the *dependent's* ticket name — hole 3 again, reached through the back door. So
whenever a branch leaves this pass's merge set **for any reason** — held above, conflict-kicked, or
bounced during isolation — **drop every dependent of it from `$ACCEPTED` too** (the full relation from
[1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd), so transitive
dependents go as well), and leave each one the same HELD note. They are not conflicted and not
rejected; they simply have no foundation this pass, and they re-enter `land-review` next pass exactly
as a held dependent does.

**Pre-compute every merge message before the first merge — no `bd` call inside the merge loop.** The
`<summary>` in each commit message comes from `bd show <id> --json` (`metadata.land_summary` / title).
**Reconciled (lode-bns3), and note what is and is not established.** The previous wording here —
"*any* `bd` read regenerates the passive export and leaves it **staged**" — is **not** what happens.
Measured three times now, independently (lode-h1vn's review, lode-bns3's build, lode-bns3's review),
each inside a live agent worktree: a bare `bd show` / `bd ready` (reads) **and** a real `bd update`
(a write) each leave `git status --porcelain` **empty**. bd writes go to Dolt; the tracked
`.beads/issues.jsonl` is regenerated and staged by the **pre-commit hook at commit time**, not at
`bd`-call time. So the per-iteration `bd show` this section hoists out of the loop is **not** what
re-dirties the tree, and no claim here should say it is.

**What has NOT been established is the positive cause.** [bd-sync
discipline](#bd-sync-discipline-non-negotiable) below names `bd dolt pull` as the suspected trigger —
but read it closely: it states that as an explicit *defensive assumption* ("on the assumption it may
be staged even when `git diff` says otherwise"), not as a measurement, and a direct attempt to
reproduce it during lode-bns3's review did **not** stage anything. Do not upgrade that hedge into a
settled fact — swapping one confidently-wrong cause for another is the failure this reconciliation
exists to end, and a wrong causal story about a destructive path is worse than an admitted gap.

**The restore below stays regardless, and its justification does not depend on knowing the cause.**
The staged-jsonl failure is real and observed (a merge refusing with "Your local changes … would be
overwritten"); the export is **by invariant never work** (`import.auto: false`, lode-6ra); so
restoring it unconditionally is free, correct whatever the trigger turns out to be, and precisely the
right move *because* the trigger is unestablished. Hoisting `bd show` out of the loop stays worth
doing on its own merits — one read pass beats N subprocess calls, and it costs nothing to avoid
depending on future `bd` versions behaving as measured today. (The Section 4 GC loop takes none of
this on faith either way — as of lode-bns3 it *excludes* the export from its cleanliness judgment
outright, so it never has to assume anything about what does or doesn't dirty it. It excludes rather
than restores because it only needs to **judge**, whereas this section must actually **clean** the
index or the merge below refuses to run — the same invariant, two different jobs.)

```bash
declare -A MSG
for id in $ACCEPTED; do
  SUMMARY=$(rtk bd show "$id" --json | jq -r '.[0].metadata.land_summary // .[0].title')
  MSG[$id]="Merge land/$id: $SUMMARY ($id)"
done
```

Before merging anything, unstage the passive jsonl export — unconditionally, without needing to know
what staged it (see the reconciliation above: it is *not* the reads above, and the `bd dolt pull`
suspicion is unverified). `.beads/issues.jsonl` staged means its index blob differs from
`HEAD` while the worktree matches the index — `git diff` / `git diff --quiet` read **clean** in that
state (they compare worktree to index, not index to `HEAD`), so the drift is invisible right up until
`git merge --no-ff` refuses with "Your local changes to the following files would be overwritten by
merge." A bare `git checkout --` does **not** fix this: it only overwrites the worktree, leaving the
staged index entry in place, so a naive retry loops. `git restore --staged --worktree` resets both
index and worktree back to `HEAD` in one shot:

```bash
rtk git restore --staged --worktree .beads/issues.jsonl 2>/dev/null || true   # unstage the passive export;
  # a STAGED jsonl aborts 'git merge' even though 'git diff' reads clean (staged != unstaged) — never
  # let the passive export block or enter a merge (import.auto: false; see bd-sync discipline below)
```

**A failed `git merge` is not automatically a textual conflict.** With the `bd` calls now out of the
loop this should not recur, but if the jsonl gets re-staged mid-loop by anything else, the failure
looks identical to a conflict and must not be classified as one on sight. Classify on the actual
failure: `would be overwritten by merge` in stderr *with* an **empty** `git ls-files -u` (no unmerged
index entries) is the passive-export trap, not a conflict — restore and retry the same merge once.
Only a genuinely unmerged index (`git ls-files -u` non-empty) is a real textual conflict:

```bash
merge_one() {   # $1 = id — merges "origin/land/$1" with its pre-computed message, retrying once past
                 # a re-staged jsonl; returns non-zero ONLY on a real textual conflict (or an unretried
                 # failure), never on the jsonl symptom. On a real conflict it sets $CONFLICTS (the
                 # unmerged paths) for the needs-rebase kick-back, then aborts to a clean tree.
  local id="$1" err
  err=$(rtk git merge --no-ff "origin/land/$id" -m "${MSG[$id]}" 2>&1) && return 0
  if printf '%s' "$err" | grep -q 'would be overwritten by merge' && [ -z "$(rtk git ls-files -u)" ]; then
    rtk git restore --staged --worktree .beads/issues.jsonl 2>/dev/null || true   # re-dirtied, not conflicted
    rtk git merge --no-ff "origin/land/$id" -m "${MSG[$id]}" && return 0
  fi
  printf '%s\n' "$err" >&2
  local unmerged; unmerged=$(rtk git ls-files -u)            # non-empty ONLY on a real textual conflict
  if [ -n "$unmerged" ]; then
    CONFLICTS=$(printf '%s\n' "$unmerged" | cut -f2- | sort -u)   # name the paths for the kick-back note...
    rtk git merge --abort                                    # ...before the abort clears the unmerged index
  fi
  return 1        # real conflict — caller runs the needs-rebase kick-back (with $CONFLICTS) below
}

# On trunk, accepted set = the IDs land-review accepted this pass.
for id in $ACCEPTED; do
  if ! merge_one "$id"; then
    # → real textual conflict with a branch already merged this pass: both passed the 2b precheck
    # against origin/trunk but conflict with *each other*. Needs-rebase kick-back (see below, with
    # $CONFLICTS), NOT a land — it leaves this pass's set, so it is excluded from the re-gate, and from
    # the $LANDED that Section 4 closes and GCs. Symmetric with the isolation loop below; without this
    # check merge_one's clean abort would silently drop it into $LANDED and close/delete unlanded work.
    #
    # 3a INVARIANT: this branch just LEFT the merge set — so drop its dependents from $ACCEPTED too
    # (1a's full relation, transitively) and leave each the HELD note. Otherwise the loop merges a
    # dependent whose base is no longer landing, putting this branch's un-landed content on trunk
    # under the dependent's name.
    continue
  fi
done
```

Re-gate the combined result (this is a Python-gated repo where code changed; a **docs-only** merge
set has no Python gate — skip nox, run `scripts/validate-mermaid.sh` only if a merged diff touched a
`docs/` diagram):

```bash
. ./venv/bin/activate
rtk nox -t fix && rtk nox -s tests     # if nox -t fix reformats merged code, commit that as part of the merge result
```

**`validate-mermaid.sh` exit 2 is NOT a red gate — it is a machine fault, and isolating on it bounces
an innocent branch.** Exit 2 means the *gate itself could not run*; only exit **1** means invalid
mermaid. The distinction exists precisely because a broken tool used to be indistinguishable from
broken content (lode-9i2p). On exit 2 I do **not** isolate, do **not** bounce, and do **not** land the
docs set with the diagram unverified: I stop the pass and surface the script's own exit-2 message
verbatim as a human decision — it names the cause and the remedy, and only a human can fix the
machine. A red gate is content; exit 2 is the machine.

- **Green** → proceed to [Land the survivors](#4-land-the-survivors).
- **Red** → **isolate**. The combined merge is bad but I don't yet know which branch. Reset `trunk`
  back to `origin/trunk` and replay the accepted set **one at a time** (in 3a's order), re-gating after
  each; keep every branch that stays green, and **bounce** the first that turns the gate red (→ new
  ticket, drop branch), then continue with the rest — **but if the branch I bounce here is a base, its
  dependents leave the set with it** ([3a's
  invariant](#3a-order-the-accepted-set--base-before-dependent-hold-an-orphaned-dependent)): don't
  replay them, hold them. Replaying a dependent whose base just failed the gate merges the failing
  content back in under a different ticket's name. (The bounce's own [descendant
  check](#bounce--clear-failure) fires here too — a live dependent means this bounce **escalates**
  rather than deleting the branch.)

  ```bash
  rtk git reset --hard origin/trunk
  for id in $ACCEPTED; do
    if ! merge_one "$id"; then
      # → real textual conflict against an earlier survivor merged this pass: needs-rebase kick-back
      # (see below), not a bounce — its content wasn't judged bad, it just needs to replay onto the
      # new trunk. Continue with the rest.
      continue
    fi
    if rtk nox -s tests; then
      :                          # survivor — keep it merged
    else
      rtk git reset --hard HEAD~1   # back the culprit out
      # → bounce <id> (Section "Bounce"); it does NOT land this pass
    fi
  done
  ```

  The survivors stay merged on local `trunk`; the culprit is bounced like any other failure. A branch
  `merge_one` reports as a real conflict here — one that passed the 2b precheck against `origin/trunk`
  but can't cleanly combine with an *earlier survivor* merged this pass — is handled as a
  [needs-rebase kick-back](#needs-rebase--kick-back), not a bounce.

---

## 4. Land the survivors

Only now — combined `trunk` is green — do I write the world. Order matters (see
[bd-sync discipline](#bd-sync-discipline-non-negotiable)): push `trunk` first, then close, then
publish bd state, then GC branches **and the local builder worktrees**.

First, check whether the re-gate's `nox -t fix` (above) actually changed anything:

```bash
rtk git status --short
```

- **Empty** → `nox -t fix` touched nothing. Skip the commit entirely — there's nothing to commit.
- **Non-empty** → stage **only** the explicitly-named reformatted source paths shown by that
  `git status`. Never `-A`, and never rely on a `':!.beads'` pathspec exclude to keep the passive
  jsonl export out — that exclude does not reliably survive the `rtk` proxy (it once let
  `.beads/issues.jsonl` through, and separately `-A` swept in an unrelated pre-existing untracked
  directory under a misleading `style:` message — both hit landing `lode-0wj.1`). Beads' own
  pre-commit hook (`.beads/hooks/pre-commit`) re-exports and re-stages `.beads/issues.jsonl` on
  *every* commit regardless of what was `git add`-ed (see CLAUDE.md's workflow gotchas), so the
  commit itself must skip hooks too:

  ```bash
  rtk git add <path> <path> ...                                          # explicit reformatted source paths only, e.g. rtk git add src/foo.py src/bar.py
  rtk git commit --no-verify -q -m "style: nox -t fix on merged trunk"   # --no-verify: skip the beads pre-commit hook so it can't re-stage .beads/issues.jsonl
  rtk git show --stat HEAD                                               # confirm only the intended paths rode along — no jsonl, nothing else
  ```

```bash
rtk git push origin trunk
rtk git status                 # MUST show trunk up to date with origin

for id in $LANDED; do
  rtk bd close "$id" --reason "Landed on trunk via /land (merge <sha>)"
done

# Closing the last child of an epic completes it — flag it for the closing-side review.
# I only NOTICE completion here (I am the one that closed it); the review itself is the
# separate `/epic-audit` skill. For each just-closed ticket, walk to its parent epic and,
# if that epic is now fully child-complete and not already flagged/audited, label it.
for id in $LANDED; do
  PARENT=$(rtk bd show "$id" --json | jq -r '.[0].dependencies[]? | select(.dependency_type=="parent-child") | .id' | head -1)
  [ -z "$PARENT" ] && continue
  READY=$(rtk bd show "$PARENT" --json | jq -r '
    .[0] as $e |
    (($e.dependents // []) | map(select(.dependency_type=="parent-child"))) as $kids |
    ($e.labels // []) as $lbl |
    if ($e.issue_type=="epic") and ($e.status!="closed")
       and (($kids|length)>0) and (all($kids[]; .status=="closed"))
       and (($lbl | index("epic-audited")) | not)
       and (($lbl | index("epic-ready-to-audit")) | not)
    then "READY" else "" end')
  [ "$READY" = "READY" ] && rtk bd label add "$PARENT" epic-ready-to-audit   # /epic-audit picks it up
done

rtk scripts/bd-dolt-push.sh               # publish the closes, epic-ready-to-audit labels, and any bounce tickets over refs/dolt/data — durable, cross-machine

for id in $LANDED; do
  rtk git push origin --delete "land/$id"   # GC the merged remote branch — a bare ref delete, not a
                                             # worktree/uncommitted-work risk, so this stays per-ticket
                                             # regardless of the local worktree-GC decision below.
done

# Local worktree + branch GC is NOT done per-ticket (lode-h1vn). There used to be a loop here that read
# metadata.review_worktree/review_branch off each just-landed ticket and ran `git worktree remove
# --force` unconditionally — no `locked` check, no dirty-tree check. It is DELETED; the backstop sweep
# below is now the only local worktree/branch reclaim, and it catches every just-landed builder worktree
# on the same pass (this pass's `--no-ff` merge is what makes each one's HEAD an ancestor of trunk, a
# few lines above). Discovering worktrees live from `git worktree list --porcelain` also beats trusting
# per-ticket metadata that can drift.
#
# WHAT THIS COSTS, because "the backstop subsumes it" is true of the CANDIDATE set but NOT the RECLAIMED
# set: the backstop gates on `locked` + clean-tree + HEAD-ancestry, none of which the old loop had, so it
# reclaims strictly LESS. A landed builder worktree that is DIRTY, LOCKED, or carries commits that never
# reached origin is now KEPT where the old loop force-removed it — the dirty case being a PERMANENT leak
# (it stays dirty, so every later pass skips it too; a human must clear it). That is deliberate and is
# the trade lode-9hgu already made: leak a directory rather than destroy uncommitted work. Measured to be
# rare — real post-build/post-review worktrees read clean.
#
# ONE UNENFORCED COUPLING keeps this loop reclaiming anything at all; if it breaks it silently
# reclaims NOTHING, and since the per-ticket loop is gone there is no second net and no alarm:
#   1. `.gitignore` (lode-9hgu) — a finished worktree is full of untracked build junk (`venv/`, `.nox/`,
#      `__pycache__/`); it reads clean ONLY because those are ignored. Un-ignore one and every worktree
#      reads dirty.
# (CLOSED, lode-bns3) bd export churn used to be a SECOND unenforced coupling — this comment used to
#   say the gate ASSUMES a `bd` write never dirties the tree, contradicted by Section 3 (~line 354,
#   now reconciled) asserting the opposite for its own merge path. Measured three times (lode-h1vn's
#   review, lode-bns3's build, lode-bns3's review): neither a bare `bd` read nor a `bd` write dirties a
#   worktree by itself. The positive cause of the staged export is NOT established — `bd dolt pull` is
#   the suspected trigger, but bd-sync discipline below states that as a defensive assumption, not a
#   measurement, and it did not reproduce when tried (see Section 3's reconciled note; do not restate
#   the suspicion as fact). None of that matters to this loop, which no longer needs the premise
#   settled in EITHER direction — the whole point is that the gate is correct whatever the trigger
#   turns out to be, since the export is by invariant never work. It now EXCLUDES
#   `.beads/issues.jsonl` / `.beads/interactions.jsonl` from the cleanliness judgment outright, via
#   `:(exclude)` pathspecs on the dirty-tree guard below — so a staged or modified export, from
#   whatever cause, present or future, can never zero out this sweep on its own.
# If you touch `.gitignore`, re-check that this loop still reclaims.
#
# Full record — the three options, the measurement, why deletion beat guarding: docs/decisions.md,
# lode-h1vn entry.

# Backstop: now the ONLY local worktree/branch reclaim in this pass — catches every just-landed
# builder worktree (per the reasoning above) plus whatever it always caught: a stale/missing
# review_worktree pointer, a build that never got GC'd on its own machine, a reviewer/rebase-pickup
# worktree from a multi-cycle review that no ticket's single review_worktree field can point at
# (lode-r78 — the reviewer and a rebase pickup each check `land/<id>` out into their OWN fresh
# worktree per lode-k5e/lode-8k3, so a ticket reviewed more than once leaves extra land/<id>-branched
# worktrees a per-ticket net could never see anyway), or (historically) this section's own
# rtk-mangled-porcelain bug. Walks the raw porcelain blocks directly, so a worktree with no matching
# ticket, or a ticket with stale/wrong metadata, still gets reclaimed.
#
# NOTE (lode-vs7g): `/code`'s own orchestrating session now reclaims a reviewer's or rebase-pickup's
# launch worktree proactively, right after that subagent returns (either outcome — ready-for-land or
# land-escalated), deriving it from the ticket id via the `land/<id>--<worktree-dir>` branch name — see
# `.claude/skills/code/SKILL.md` and docs/decisions.md's lode-vs7g entry. This backstop is UNCHANGED
# and stays exactly as it was, but it is a PARTIAL net, not a total one: it only ever reclaims a
# worktree whose branch is already merged into trunk, so it cannot cover an escalated ticket (whose
# branch never merges) — that case is closed by /code's reclaim, not here. Expect this to fire far less
# often now: mostly for a /code session that died mid-fan-out, before it could reclaim.
#
# ONE loop covers BOTH branch-attached and DETACHED worktrees (lode-jiyk unifies what were formerly
# two separate WORKTREE sweeps here: a branch-NAME-keyed one, lode-r78, and a later HEAD-sha-keyed
# one, lode-mxeu, added because the name-keyed one structurally cannot see a detached worktree).
# Both tested the literally identical predicate — "this worktree's tip is already merged
# into trunk" — by two different routes: a branch-name lookup against a `git branch --merged trunk`
# list (branch NAMES, so it can only ever match a worktree that HAS a branch), or a direct
# `git merge-base --is-ancestor <HEAD-sha> trunk` (needs no branch name at all). The SHA form is
# strictly more general — it subsumes the branch-attached case too — so ONE loop, keyed on HEAD-sha
# ancestry rather than a branch-name pattern, replaces both nets: this sweep no longer cares whether
# a candidate worktree's branch (if any) is named `worktree-agent-*`, `land/*`, or something else
# entirely — every worktree under `.claude/worktrees/` is a candidate, so a new worktree-BRANCH-naming
# convention cannot leak past THIS loop.
#
# That name-independence is scoped to worktrees and does NOT extend to the bare-ref backstops below:
# the second and third still enumerate `refs/heads/land/*` and `refs/heads/worktree-agent-*` by name,
# because `refs/heads/*` is shared with human branches and a name-blind "delete any merged local ref"
# would eat them. A new BARE-REF namespace can therefore still leak exactly as lode-j5i0's did — so
# if you add one, audit those two, not this one. (lode-j5i0's sweep is the THIRD backstop below; it is
# alive and untouched — it was never one of the two unified here.)
#
# CONTRACT (lode-9hgu closed the zero-divergence residual this paragraph used to describe; lode-amif
# widened the ancestry predicate itself, and the dirty-tree guard below gates BOTH arms): ANY
# worktree under `.claude/worktrees/` that is unlocked, AND EITHER has not diverged from `trunk` OR —
# for a branch-attached worktree — has not diverged from its own branch's origin counterpart, AND is
# clean is reclaimable by this loop, whoever made it (lode-amif: the ancestry predicate widened from
# "merged into trunk" alone to "captured on origin," so an escalated ticket's reviewer/rebase-pickup
# worktree, whose branch never merges into trunk by definition, is reclaimable too, once its content
# is safely on `origin/land/<id>`). A worktree freshly branched off `trunk` HEAD (or freshly checked
# out at its origin branch's current tip) is trivially "merged"/"captured" by zero divergence — that
# proxy alone would read TRUE for a live, uncommitted build/review the instant its worktree is created
# — so the loop below also tests the ACTUAL invariant directly ("is this work captured anywhere
# else"): `git -C "$WT" status --porcelain`. A dirty tree is never reclaimed, regardless of lock
# state, ancestry, or who made the worktree — this is what actually protects the worktree classes that
# hold NO lock by the time this sweep sees them: an interactive `EnterWorktree` session, a human's
# hand-made worktree (CLAUDE.md mandates one for all work), and an exited agent's leftover scratch.
# There are exactly TWO lock sources in this system, and neither covers those three:
#   1. The Claude Code HARNESS locks every `isolation: worktree` launch worktree for the LIFETIME of
#      the agent standing in it (reason: `claude agent <name> (pid <n> start <n>)`), released when
#      the agent exits. Neither `.claude/agents/code-reviewer.md` nor `coding`'s rebase pickup calls
#      `git worktree lock` itself, but both run in such a worktree — so a LIVE reviewer/pickup
#      worktree is `locked` and this loop skips it outright, never reaching the predicate. Verified
#      against a running reviewer (lode-amif's own).
#   2. `.claude/agents/coding.md` ALSO locks its producer build worktree explicitly (lode-oqr),
#      because it unlocks again at its first commit — earlier than the harness would.
# So the zero-divergence residual bites only an EXITED agent's worktree, and only its UNCOMMITTED
# scratch — and even there, the dirty-tree guard below is the real backstop, not `locked`: `git -C
# "$WT" status --porcelain` prints nothing both when the tree is CLEAN and when the command itself
# errors (missing dir, corrupt worktree admin, …), so the guard distinguishes "clean" (proceed) from
# "could not tell" (skip) rather than treating empty output as always meaning clean. On the trunk arm
# an unguarded zero-divergence read was once a real hazard — it destroyed two builds' uncommitted work
# outright before the dirty guard existed (lode-oqr's explicit lock closed the narrower pre-first-
# commit window; lode-9hgu's dirty guard closed the rest). On the origin arm the same guard is what
# keeps a captured-but-dirty reviewer/rebase-pickup worktree from being reclaimed too: content merely
# pushed to `origin/land/<id>` is captured, but a worktree with additional uncommitted changes on top
# of that push is not, and must still be KEPT — otherwise this widened arm reopens exactly the hole
# lode-9hgu just closed. Accepted residual: a CLEAN worktree at zero divergence (trunk arm) or clean at
# its origin counterpart's tip (origin arm) that raises no lock — a human's hand-made worktree they
# happen to be sitting in, or an exited agent's clean leftovers — is still reclaimable; nothing is
# destroyed (the tree is clean), the directory just vanishes out from under whoever is standing in it.
# A LIVE harness agent's worktree is NOT in that set — its harness lock (above) drops it in the
# `locked` check below, before either predicate is ever evaluated. The trade is intentional: the
# failure direction is now "remove an empty checkout," never "destroy uncommitted work," on either arm.
#
# Skip anything `locked` — that's the git-native in-use signal, and it's load-bearing here: a
# currently-running sibling worktree whose branch hasn't diverged from trunk yet is trivially
# "merged" into trunk by content identity, so `locked` must gate this even though `merged` alone
# looks sufficient. `merged` is the same safety invariant that justified the old per-ticket loop's own
# unconditional `--force` ("the build artifact is on trunk now — force is safe") before that loop was
# deleted in favor of this backstop owning all local worktree/branch reclaim (lode-h1vn) — for a
# **builder's own** `worktree-agent-*` worktree specifically, `merged` is what proves the ticket already
# landed: its branch is never pushed anywhere, so the origin arm added by lode-amif is always false for
# it, and an in-flight `ready-for-code-review`/`ready-for-land`/`land-escalated` ticket's builder
# worktree is excluded regardless of lock state, exactly as before. A `land/<id>`-branched
# **reviewer/rebase-pickup** worktree is different since lode-amif: once its branch is pushed to
# `origin/land/<id>`, the origin arm can make it reclaimable even though its branch has not (and, if
# escalated, never will) merge into trunk — that is the gap lode-amif exists to close. This `locked`
# check used to be a no-op in practice:
# nothing on the producer side ever raised it, so every producer build was "merged" (trivially, by
# zero divergence) and reclaimable from the moment its worktree was created until its first commit --
# this destroyed two builds' uncommitted work outright (branch and all, not just the checkout) before
# the gap was understood (lode-oqr). `.claude/agents/coding.md` now locks the worktree as the
# producer's first action and unlocks it right after its first commit, closing that window; this
# loop's `locked` filter needed no change. `locked` is still checked first (cheapest, and the
# git-native in-use signal), but as of lode-9hgu it is no longer the ONLY thing standing between a
# live, uncommitted tree and `--force` removal — the dirty-tree guard below is the backstop for every
# worktree class that never raises `locked` at all (see the CONTRACT paragraph above). The one
# accepted trade-off specific to `locked`: a crash strictly between lock and first commit leaves a
# locked worktree this sweep won't auto-reclaim -- rare (a normal build commits within minutes) and
# resolved by a manual `git worktree unlock` (or a future cleanup ticket), not by this loop, since
# correctness (never destroy a live build) matters more here than eagerness.
#
# Scoped to paths under .claude/worktrees/ so this can never touch the main checkout (its tip is
# always merged into itself, so the predicate alone wouldn't exclude it — the path guard is what
# does, and it costs nothing). If the worktree has a branch, delete it too (`git branch -D`); a
# detached worktree has none, so worktree removal alone is the entire reclaim.
# lode-bns3 (observability): count each candidate into exactly one bucket — reclaimed, or skipped for
# locked / not-merged / dirty — so the summary line after the loop can tell "reclaimed 0 of 0, nothing
# to do" apart from "reclaimed 0 of N, everything was skipped" (a regression that zeroes out GC must be
# visible here, not silent). `locked` is counted in the loop body now rather than filtered inside awk,
# so every candidate under .claude/worktrees/ reaches the summary, whichever bucket it lands in.
#
# FIELD ORDER IS LOAD-BEARING — DO NOT REORDER (`$BR` must stay LAST). Tab is IFS *whitespace*, so
# `read` collapses adjacent tabs into a single delimiter and does NOT preserve an empty MIDDLE field.
# `branch` is the one field that can be empty (a DETACHED worktree — a case this loop explicitly
# supports, see the `git branch -D` note above). With `branch` in the middle, a detached worktree's
# line (`path\thead\t\tlocked`) shifts every later field left: `$BR` swallows the locked flag and
# `$LOCKED` reads EMPTY, so `[ "$LOCKED" = "1" ]` is FALSE and a LOCKED, LIVE agent's worktree sails
# past the locked gate into the `--force` below — precisely the "rip a worktree out from under a
# running agent" harm the gate exists to prevent (the pre-lode-oqr disaster). Keeping `branch` last
# makes its empty case a TRAILING delimiter, which `read` discards harmlessly ($BR="").
RECLAIMED=0; SKIP_LOCKED=0; SKIP_NOTMERGED=0; SKIP_DIRTY=0; FAILED=0
while IFS=$'\t' read -r WT SHA LOCKED BR; do
  if [ "$LOCKED" = "1" ]; then
    SKIP_LOCKED=$((SKIP_LOCKED + 1))
    continue
  fi
  # WIDENED PREDICATE (lode-amif): "merged into trunk" is not the real safety invariant — it is a
  # PROXY for "this worktree's content is already captured elsewhere, so removing it loses nothing."
  # An ESCALATED branch never merges into trunk (by definition — it's held for a human decision), so
  # the trunk-only test could never reclaim its reviewer/rebase-pickup worktree even after the /code
  # session that would otherwise eagerly reclaim it (lode-vs7g) has itself died mid-fan-out — an
  # indefinite leak one level up from the gap lode-vs7g closed. The real invariant is "captured on
  # origin," which every reviewer/rebase-pickup worktree satisfies by construction: both push to
  # origin/land/<id> before returning (lode-k5e/lode-8k3), on EITHER outcome (ready-for-land or
  # land-escalated). So: reclaim if the worktree's HEAD is an ancestor of trunk, OR — for a
  # branch-attached worktree only — an ancestor of that branch's origin counterpart. `${BR%%--*}`
  # strips the lode-em6v worktree-uniqueness suffix (`land/<id>--<worktree-dir>` -> `land/<id>`) the
  # same way backstop 2 already does, so this reaches origin/land/<id> regardless of which local name
  # the reviewer/pickup checked the branch out under. A detached worktree (empty $BR) or a builder's
  # own worktree-agent-* branch (never pushed to origin, so origin/worktree-agent-* doesn't exist and
  # the ancestor test fails) fall through to `continue` exactly as before — this arm is simply false
  # for them, so builder worktrees are unaffected.
  #
  # ZERO-DIVERGENCE RESIDUAL, AND WHY IT IS BENIGN ON THIS ARM (lode-9hgu, not re-litigated here):
  # like the trunk arm, this ancestor test is a proxy that reads TRUE at zero divergence — a
  # reviewer/pickup worktree freshly checked out at origin/land/<id>'s tip is trivially "an ancestor
  # of" that same tip until its first local commit. But a LIVE reviewer/pickup worktree is `locked`
  # for the duration of its agent's run (the harness locks every `isolation: worktree` launch
  # worktree — see the CONTRACT above), so the `locked` check above drops it before this predicate ever runs:
  # this arm CANNOT sweep a running agent. It can only reach an EXITED one, whose worktree at zero
  # divergence holds nothing but uncommitted, ungated scratch from a run that never finished — the
  # authoritative content is on origin and the ticket is re-reviewed from there. That is exactly the
  # worktree this widening exists to reclaim, so the residual is benign HERE, unlike on the trunk arm
  # (where it once destroyed two live builds, pre-lode-oqr). The dirty-tree guard immediately below
  # (lode-9hgu) is what makes this arm safe even for that exited-agent case: it still keeps a worktree
  # that has additional uncommitted content on top of a pushed tip, rather than reclaiming it.
  git merge-base --is-ancestor "$SHA" trunk \
    || { [ -n "$BR" ] && git merge-base --is-ancestor "$SHA" "origin/${BR%%--*}" 2>/dev/null; } \
    || { SKIP_NOTMERGED=$((SKIP_NOTMERGED + 1)); continue; }
  # lode-9hgu dirty-tree guard — the ACTUAL invariant, not either ancestry proxy above (see CONTRACT).
  # Gates BOTH arms: a worktree captured on trunk or captured on origin but left dirty must still be
  # KEPT, otherwise either arm reopens exactly the hole lode-9hgu closed. Success and emptiness are
  # tested SEPARATELY, on purpose: `status --porcelain` prints nothing both when the tree is CLEAN and
  # when the command ERRORS, and an assignment inherits its command substitution's exit code — so a
  # failure (missing dir, corrupt worktree admin, …) skips exactly like a dirty tree instead of failing
  # OPEN into `--force`. Skipping on error costs little: the `git worktree prune` below still drops a
  # vanished worktree's admin entry, and any leftover branch ref falls to the bare-ref backstops.
  #
  # lode-bns3: the passive bd export is EXCLUDED from this judgment via `:(exclude)` pathspecs, so a
  # staged/modified `.beads/*.jsonl` can never read as "dirty" and zero out the sweep. It is BY
  # INVARIANT never real work (import.auto: false, lode-6ra), and this gate must not depend on
  # something ELSE having scrubbed it first (the Stop hook in .claude/settings.json, an explicit
  # `bd export`, or the now-reconciled "bd reads never dirty it" premise — see the coupling note above
  # and Section 3's reconciled note). A worktree with any OTHER dirt still lands in the dirty bucket
  # below and is KEPT, untouched — exclusion narrows what counts as dirt, nothing else.
  #
  # EXCLUDE, DON'T RESTORE — the difference matters here even though Section 3 restores. Section 3 must
  # genuinely CLEAN the index (its `git merge --no-ff` refuses to run otherwise); this loop only has to
  # JUDGE. Restoring would make a read-only judgment WRITE into candidate worktrees — including the
  # dirty ones it then decides to KEEP, silently discarding their export churn as a side effect of
  # merely looking at them. Excluding has zero blast radius, which is the right posture for a loop that
  # ends in `--force`. It also sidesteps a real trap: `git restore` aborts WHOLESALE on an unmatched
  # pathspec and would restore NEITHER file (silently, under `|| true`) for a candidate sitting at a
  # commit that predates one of these paths — a stale leftover worktree being exactly what this backstop
  # exists to reclaim. `git status` with `:(exclude)` simply exits 0 in that case.
  #
  # COUNT THE REMOVE'S ACTUAL EXIT STATUS, not merely the fact that we attempted it. An unconditional
  # `RECLAIMED=$((RECLAIMED + 1))` here would let the summary line report "reclaimed N" when every
  # `worktree remove` FAILED and nothing was reclaimed at all — the observability half of lode-bns3
  # lying in exactly the direction it exists to expose (a total GC failure reading as a healthy sweep).
  # `failed` is its own bucket so the buckets still partition the candidates exactly.
  if STATUS=$(git -C "$WT" status --porcelain -- . \
       ':(exclude).beads/issues.jsonl' ':(exclude).beads/interactions.jsonl' 2>&1) && [ -z "$STATUS" ]; then
    if git worktree remove --force "$WT"; then
      [ -n "$BR" ] && git branch -D "$BR" 2>/dev/null || true
      RECLAIMED=$((RECLAIMED + 1))
    else
      FAILED=$((FAILED + 1))    # git printed its own error; surface it in the summary too
    fi
  else
    SKIP_DIRTY=$((SKIP_DIRTY + 1))
  fi
done < <(git worktree list --porcelain | awk '
  /^worktree / { path=$2; head=""; branch=""; locked=0 }
  /^HEAD / { head=$2 }
  /^branch refs\/heads\// { branch=substr($0,19) }
  /^locked/ { locked=1 }
  /^$/ { if (path!="" && path ~ /\/\.claude\/worktrees\//) print path"\t"head"\t"locked"\t"branch; path="" }
')
git worktree prune          # drop any now-stale worktree admin entries
# lode-bns3 (observability): always emit one line. "reclaimed 0 of 0" (nothing to do) reads differently
# from "reclaimed 0 of N" (everything was skipped — worth investigating), and the reason breakdown
# makes a regression that silently zeroes out GC visible here instead of indistinguishable from idle.
TOTAL=$((RECLAIMED + SKIP_LOCKED + SKIP_NOTMERGED + SKIP_DIRTY + FAILED))
echo "worktree GC: reclaimed $RECLAIMED of $TOTAL candidate(s) under .claude/worktrees/ (skipped: locked=$SKIP_LOCKED, not-merged=$SKIP_NOTMERGED, dirty=$SKIP_DIRTY; failed=$FAILED)"

# Second backstop: dangling local land/<id> refs with no worktree attached at all (so the
# worktree-GC loop above never even considered them) and no remote counterpart left (lode-r78). That
# loop only runs `git branch -D` when it also found a matching
# worktree; a local land/<id> branch that already lost its worktree by some other path (or
# never had one materialize beyond the fetch+checkout in coding.md's rebase pickup /
# code-reviewer.md) is invisible to it. "Remote gone" is sufficient signal on its own: an
# in-flight ticket's origin/land/<id> always exists (the producer pushed it in build step 8
# and nothing deletes it until /land lands, bounces, or drops the ticket), so a missing
# remote means this local ref is already stale. No extra locked/merged check is needed here
# — `git branch -D` itself refuses harmlessly if the branch is still checked out in some
# worktree, which is exactly the case the loop above would have just finished reclaiming.
# List origin's land refs ONCE (a single round-trip, not one `ls-remote` per local ref — a
# machine can accumulate dozens of stale land refs) and only sweep if that listing SUCCEEDED:
# an unreachable origin makes `ls-remote` exit non-zero, and reading that as "every remote
# land branch is gone" would force-delete every local land ref on a transient network blip.
# A failed listing therefore skips the sweep; an empty-but-successful one correctly means
# every local land ref is stale (grep against the empty set matches nothing → all deleted).
# STRIP THE WORKTREE SUFFIX BEFORE COMPARING (lode-em6v): since lode-em6v the reviewer and
# the rebase pickup check the branch out under `land/<id>--<their-own-worktree-dir>`, never
# the bare `land/<id>`, and that suffixed name can NEVER byte-match origin's `land/<id>`.
# Comparing it raw would make the "remote still exists — keep" arm dead code for every ref
# this sweep now sees, silently demoting the backstop to "delete every land/* ref not
# currently checked out" and force-deleting an in-flight ticket's ref (its unpushed commits
# with it) the moment its worktree goes away by any route. `${BR%%--*}` maps the local name
# back to the remote one it corresponds to (`land/x--agent-ab12` → `land/x`) and leaves a
# bare `land/x` untouched, so the ORIGINAL "remote gone ⇒ stale" semantics hold for both
# shapes. Safe because a bd id never contains `--` (ids are `lode-<slug>`, single hyphens),
# so the first `--` is always the worktree-suffix delimiter.
if REMOTE_LAND=$(git ls-remote --heads origin 'land/*' 2>/dev/null); then
  REMOTE_LAND=$(printf '%s\n' "$REMOTE_LAND" | sed 's#^.*refs/heads/##')
  git for-each-ref --format='%(refname:short)' 'refs/heads/land/*' | while read -r BR; do
    printf '%s\n' "$REMOTE_LAND" | grep -qxF "${BR%%--*}" && continue   # remote still exists — keep
    git branch -D "$BR" 2>/dev/null || true
  done
fi

# Third backstop: dangling local worktree-agent-* refs with no worktree attached at all
# (lode-j5i0 — the same bug as lode-r78, but the OTHER ref namespace: the second backstop
# above only ever swept refs/heads/land/*, so a worktree-agent-* ref orphaned by any route other
# than the worktree sweep above — which can only ever reclaim a ref that still HAS a worktree to
# remove — was swept by nothing and accumulated without bound — 17 confirmed on the landing
# machine). This namespace needs a
# DIFFERENT guard than land/*: a worktree-agent-* branch is never pushed to origin, so it
# has no remote counterpart, ever — "remote gone" is meaningless here and would delete a
# LIVE, still-building branch (every producer branch would read as "remote gone" the
# instant it's created). The correct guard is the same PREDICATE the worktree sweep above
# applies — merged into `trunk` (the work is safely captured elsewhere) — but reached by a
# branch-NAME lookup rather than that loop's HEAD-sha ancestry test, because a bare ref has no
# worktree and therefore no porcelain `HEAD ` line to test; plus not currently checked out in
# any worktree. $MERGED is computed right HERE, immediately above its ONLY consumer (the worktree
# sweep keys on HEAD-sha ancestry and needs no branch name, so nothing else reads it). Computing it
# after the sweeps above is safe: `trunk` has not moved, and every ref they deleted is gone from
# `for-each-ref` too, so the set is identical for every ref still listed. `git branch -D` itself also
# refuses harmlessly if the branch is still checked out somewhere, but that alone is not the guard
# being relied on — the explicit merged check is what keeps an in-flight, not-yet-merged build ref
# from ever being a candidate in the first place.
MERGED=$(git branch --merged trunk --format='%(refname:short)')
CHECKED_OUT=$(git worktree list --porcelain | awk '/^branch refs\/heads\//{print substr($0,19)}')
git for-each-ref --format='%(refname:short)' 'refs/heads/worktree-agent-*' | while read -r BR; do
  printf '%s\n' "$CHECKED_OUT" | grep -qxF "$BR" && continue   # still checked out somewhere — keep
  printf '%s\n' "$MERGED" | grep -qxF "$BR" || continue        # not merged into trunk — keep (in-flight)
  git branch -D "$BR" 2>/dev/null || true
done
```

`bd close` unblocks dependents — that is *why* the lander closes (the producer never does): a closed
ticket frees the next layer of `bd ready`. Closing is mine because the merge decision is mine.

The worktree GC is **best-effort and machine-local**, and (since **lode-h1vn**) entirely the
end-of-pass backstop sweep's job — there is no separate per-ticket removal step any more. **Nothing in
`/land` reads `review_worktree`/`review_branch`**, and as of **lode-2m89** nothing writes them either:
the deleted loop was their only GC consumer, the backstop discovers worktrees directly off
`git worktree list --porcelain`, and `/code`'s own reclaim *derives* its target from the ticket id
rather than trusting a recorded path (lode-vs7g) — so the fields were pure dead weight and `coding.md`
stopped recording them (see lode-2m89's `docs/decisions.md` entry). Discovering worktrees live instead
of trusting recorded paths is strictly better anyway: there is no bookkeeping to drift. Builds can
happen on several machines, and a worktree on another machine simply isn't in this machine's
`git worktree list`, so it's invisible to this sweep and that other machine's
own `/land` (or a later sweep there) reclaims it. The sweep only reclaims a worktree that is
`merged`+`unlocked`+clean, which for a just-landed ticket's builder worktree is true precisely *because*
this pass just `--no-ff` merged it into trunk a few lines above. Its **HEAD-ancestry** gate is what
holds the tree through the other outcomes: on a **bounce** the branch is dropped but the rebuild ticket
may still want the tree, and on an **escalate** the work is held for a human — in both cases the
*builder's* worktree HEAD never merged into `trunk`, so the predicate excludes it and it is kept. (Scope
that claim to the **builder's** worktree deliberately: a reviewer's or rebase-pickup's *own* launch
worktree is a different thing — `/code` reclaims that one proactively on an escalation, lode-vs7g, and
**lode-amif** widens this loop's predicate to reclaim it via *origin*-ancestry precisely *because* an
escalated branch never merges. So "an escalate reclaims nothing" is true of the builder's worktree only,
and is not a guarantee about the sweep as a whole.) This backstop sweep
is now the **only** net over the same machine's worktrees: it doesn't consult any ticket's metadata, so
it reclaims **any** worktree under `.claude/worktrees/` — branch-attached (`worktree-agent-*`,
`land/<id>--<worktree-dir>`, or any other name) or **detached** alike — regardless of whether any
ticket ever pointed at it (no ticket does, since lode-2m89). lode-jiyk unified what were originally
two separate **worktree** sweeps here: an early one keyed on branch **name** (`lode-r78`), and a
later one keyed directly on
**HEAD-sha ancestry** (`lode-mxeu`) added because a detached worktree has no branch name for the first
sweep to match. Both tested the identical predicate — "this worktree's tip is already merged into
trunk" — so now there is one loop: it requires the worktree to be **unlocked** (no in-flight agent owns
it) and its **HEAD commit** an ancestor of `trunk` (`git merge-base --is-ancestor <HEAD-sha> trunk` —
the work is safely captured elsewhere), with no branch-name pattern to keep in sync as new
worktree-branch-naming conventions are added. That name-independence is **scoped to this loop**: the
bare-ref backstops below still enumerate `land/*` and `worktree-agent-*` by name (they must —
`refs/heads/*` is shared with human branches), so a new *bare-ref* namespace can still leak exactly as
`lode-j5i0`'s did. (`lode-j5i0`'s own sweep is the **third** backstop below — alive and untouched; it
was never one of the two unified here.) "Unlocked" is a real signal now, not a formality: a producer
(`.claude/agents/coding.md`) locks its worktree the instant it starts building and unlocks it right
after its first commit, so this sweep only ever finds a worktree unlocked once its build has either not
started or already diverged from `trunk` — never mid-build with uncommitted, unreclaimed-elsewhere work
sitting in it (lode-oqr). Dropping the branch-name filter makes the sweep's **contract** explicit where
it used to be incidental: *any* worktree under `.claude/worktrees/` that is unlocked and has not
diverged from `trunk` is reclaimable, whoever created it — and a tree freshly branched off `trunk` HEAD
is trivially "merged" by zero divergence, so uncommitted work in one is **not** protected by the merged
check. At the time this loop was written, `locked` was the only thing holding it off — and a hand-made
worktree raises no lock (both lock sources are agent-side: see the CONTRACT paragraph above), so one
under `.claude/worktrees/` had to be committed or locked by hand to survive. This residual is
**pre-existing, not new** (both unified sweeps already had it: the old name-keyed one matched
`worktree-agent-*`, which is exactly what an interactive session gets, and the old detached one was
already name-blind); dropping the name filter just makes it impossible to overlook. **lode-9hgu** has
since closed the *destructive* half of this (see below): it **added** a real guard on the actual
invariant (is the tree dirty?) *alongside* the ancestry checks — which still run and still decide
candidacy — rather than relying solely on the zero-divergence-vulnerable "merged" proxy. So today a
**dirty** hand-made worktree survives even unlocked and merged-by-zero-divergence; a **clean** one at
zero divergence is still reclaimable, which is the accepted residual the CONTRACT paragraph above
records. A branch-attached candidate here is typically the reviewer's or a rebase
pickup's *own* launch worktree, per the lode-k5e/lode-8k3 architecture (they `git fetch origin
land/<id>` and check it out into a locally **uniquely-named** branch — `land/<id>--<their-own-worktree-dir>`
since **lode-em6v**, plain `land/<id>` before it. Before lode-em6v this reused the bare `land/<id>` name,
which git permits in only one worktree at a time, so whenever `land/<id>` was already checked out
elsewhere (a leaked worktree from an earlier cycle, since neither agent ever removed its own launch
worktree) the agent fell back to `git checkout --detach FETCH_HEAD`, leaving a **detached**, branchless
worktree — and the leak was **self-compounding**: a stale worktree from one cycle was precisely what
forced the next cycle onto the detaching path. **lode-em6v closed this at the source** by making the
local name unique by construction, so the collision — and the detach fallback it forced — can no longer
arise in **normal operation**; this backstop keeps catching a detached worktree regardless, as a
crash-safety net for a killed process, not because the steady-state leak it was originally built to
catch still occurs) — a ticket reviewed across more than one cycle leaves *extra* such worktrees
(branch-attached or detached) that no single `review_worktree` field can name, so this backstop is the
only net that ever reclaims them (lode-r78, lode-mxeu); `merged`+`unlocked` excludes an in-flight one
regardless of whether it has a branch. If the worktree has a branch, this backstop deletes it too
(`git branch -D`); a detached worktree has none, so worktree removal alone is the entire reclaim. A
**separate** pass right after the worktree sweep (see the script above) deletes any local `land/<id>`
**branch ref** whose `origin/land/<id>` counterpart no longer exists — the worktree sweep above only
deletes a local branch when it also found an attached worktree, so a bare ref with no worktree (e.g.
`git worktree remove`d by some other path) would otherwise linger forever once its remote is gone. That
pass is the one place the **lode-em6v** renaming *does* reach: it keys on an **exact** name match
against origin's listing, which a suffixed `land/<id>--<worktree-dir>` can never satisfy, so it strips
the suffix (`${BR%%--*}`) before comparing — the comment above the sweep has the full reasoning and why
skipping the strip would silently turn this backstop into a ref shredder, force-deleting an in-flight
ticket's ref (and its unpushed commits) the moment its worktree goes away by any route. A **third** pass
sweeps the mirror-image gap in the *other* namespace (lode-j5i0): a bare `worktree-agent-*` ref with no
worktree attached at all is invisible to both nets above (the first only matches refs that still have a
worktree; the second only matches `land/*`), so it was swept by nothing and accumulated without bound —
17 confirmed orphans on the landing machine, all already merged. Unlike `land/<id>`, a `worktree-agent-*`
branch is never pushed to origin, so "remote gone" can't be the guard here (it would fire on every
branch, live or not); the guard is `merged`-into-`trunk` plus not currently checked out anywhere — the
same safety *predicate* the worktree sweep applies, reached by a branch-**name** lookup (`git branch
--merged trunk`) rather than that sweep's HEAD-sha ancestry test, since a bare ref has no worktree and
so no HEAD sha to test.

**Update (lode-amif): the worktree sweep's predicate widened from "merged into trunk" alone to
"merged into trunk OR captured on origin."** Everything above describes the sweep as it stood after
lode-jiyk: `unlocked` + `HEAD-sha is-ancestor-of trunk`. That predicate structurally cannot reclaim an
**escalated** ticket's reviewer/rebase-pickup worktree — an escalated branch is, by definition, held
for a human decision and never merges into `trunk`, so if the `/code` session that would otherwise
eagerly reclaim it (lode-vs7g) itself dies mid-fan-out before running that reclaim, the worktree leaks
**indefinitely**: the trunk-ancestry test is never satisfied, so backstop 1 never even considers it a
candidate. The gap is one level up from what lode-vs7g closed (lode-vs7g handles the normal-exit case,
including a clean escalation return; this is the crash-before-that-point case).

The fix widens the loop's predicate with a second arm: `git merge-base --is-ancestor "$SHA" trunk ||
{ [ -n "$BR" ] && git merge-base --is-ancestor "$SHA" "origin/${BR%%--*}"; } || continue` (see the
script above and its inline comment). The real safety invariant was never "merged into trunk" — that
was always a stand-in for "this worktree's content already exists safely elsewhere." A
reviewer/rebase-pickup worktree satisfies that invariant by construction the moment it has pushed to
`origin/land/<id>` (lode-k5e/lode-8k3), on **either** outcome, `ready-for-land` or `land-escalated` — so
testing ancestry against that origin ref directly reaches exactly the cases the trunk-only test missed.
`${BR%%--*}` strips the lode-em6v worktree-uniqueness suffix the same way backstop 2 already does, so
the new arm resolves to `origin/land/<id>` regardless of which locally-suffixed name the branch was
checked out under. A detached worktree (no `$BR`) and a builder's own `worktree-agent-*` worktree (never
pushed to origin, so its origin counterpart doesn't exist and the ancestor test simply fails) are
unaffected — the new arm is false for both, so they fall through to the unchanged `trunk`-only
behavior. The new arm carries the *same* zero-divergence residual the trunk arm already has (see the
CONTRACT paragraph above and lode-9hgu) rather than introducing a new one — and on this arm it is
**benign**. A freshly-checked-out reviewer/pickup worktree is trivially "captured on origin"
(identical to origin's current tip) until its first local commit, but for that entire window it is
`locked`: the harness locks every `isolation: worktree` launch worktree for the lifetime of the agent
standing in it, so the sweep's existing `locked` filter drops a **live** reviewer/pickup worktree
before the predicate is ever evaluated. What the arm can reach is an **exited** agent's worktree,
which at zero divergence holds only uncommitted, ungated scratch from a run that never finished —
authoritative content is on `origin/land/<id>` and the ticket is re-reviewed from there. That is
precisely the worktree this widening exists to reclaim. lode-9hgu has since landed (6591ba9). It did
not *remove* the ancestry proxies — both arms still run, and still decide candidacy — it added a real
dirty-tree guard (`git -C "$WT" status --porcelain`) immediately below them, gating **both**: a worktree
captured on `trunk` or on `origin` but left dirty is now KEPT, not reclaimed. See the CONTRACT paragraph
above and the guard itself in the loop.

---

## Needs rebase — kick back

A **needs-rebase** is the outcome of the [2b precheck](#2b-cheap-conflict-precheck--does-it-still-merge-onto-trunk)
(or a Section-3 textual conflict): the branch **can't merge onto current `trunk`**, but its content
was never judged bad — I never ran `land-review` on it. It is a **third outcome, distinct from bounce
and escalate**: not a rebuild (nothing is wrong with the work), not a human decision (there's nothing
to decide — it just needs to replay onto where `trunk` moved). So I keep everything and hand it
straight back to the producer:

```bash
rtk bd update <id> --remove-label ready-for-land --add-label needs-rebase \
  --append-notes "NEEDS REBASE (/land): origin/land/<id> no longer merges cleanly onto trunk @ $(rtk git rev-parse --short origin/trunk).
Conflicting paths:
$CONFLICTS
/code's step-0 pickup merges current trunk into land/<id>, re-gates, commits, and pushes the result
itself (an ordinary, non-force push), then swaps needs-rebase back to ready-for-land (lode-cln)."
rtk scripts/bd-dolt-push.sh       # publish the label swap + note over refs/dolt/data
# The branch is KEPT (no delete). The build worktree is KEPT. No supersede, no new ticket, no close.
```

Unlike a bounce, I **do not** supersede, create a rebuild ticket, or drop the branch, and unlike an
escalate there's no question for a human — the producer just replays its own already-reviewed work
onto the new `trunk`. The ticket stays `in_progress`; the `needs-rebase` label (not `ready-for-land`)
is now its state. **`/code` picks this up automatically** (lode-wfl): every invocation sweeps
`bd list --label needs-rebase --status in_progress` first and dispatches a `coding` producer to
merge current `trunk` into `land/<id>`, re-gate, commit, and push the result itself — an ordinary,
non-force push, since the merge only appends and never rewrites what's already on `land/<id>` — then
swap the label straight back to `ready-for-land` itself (lode-cln; full mechanics in
[`docs/agents-workflow.md`](../../../docs/agents-workflow.md#the-step-0-pickup-merges-it-never-rebases-lode-cln))
— no human nudge needed unless the merge itself conflicts and the two sides genuinely disagree (that
escalates, `land-escalated`, same as any other genuine decision).

## Bounce — clear failure

A **bounce** is a confident "this branch should not land as-is" (an unmet acceptance criterion,
silent scope creep, a violated invariant, a wrong approach — or drift/conflict from Sections 2a/3).
The original ticket is **superseded** by a fresh ticket that carries the `land-review` findings, so a
producer can rebuild from a clean brief.

**Before doing anything else: check for live descendants (lode-02v).** A bounce **deletes**
`land/<id>`, and a prior pass did that with no idea another live branch had already merged `land/<id>`
in. Deleting the branch does not delete its commits from the dependent: the dependent went on carrying
the rejected content — including the very defect the bounce was rejecting — on a foundation that no
longer existed and would never land (OBSERVED: lode-6qh / lode-96t). So: **read the descendants of
`<id>` straight out of [1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)'s
map** (the *full* relation — a transitive dependent is stranded exactly as badly as a direct one). The
map is already built for this pass; do **not** re-derive it here with an ad-hoc `--contains` probe
against `<id>`'s tip — that is the tip test 1a exists to avoid.

- **No descendants** → proceed with the bounce exactly as below.
- **A live `land/<dep>` descendant found** → I do **not** silently drop `land/<id>`. I escalate
  instead — the lander does not decide this; the escalate exit already exists for exactly this shape
  of question:

  ```bash
  rtk bd update <id> --remove-label ready-for-land --add-label land-escalated \
    --append-notes "ESCALATION (/land bounce, lode-02v): land-review bounced this branch (findings
  below), but land/<dep> is a LIVE branch that already merged land/<id>'s commits — deleting land/<id>
  now would silently strand land/<dep>, which would carry the very defect this bounce is rejecting.
  Needs a human decision: FOLD (supersede both <id> and <dep> into one combined rebuild ticket — see
  the disposition rule below for which branch, if either, is worth keeping to lift from), SEQUENCE
  (rebuild <id> alone; <dep> stays blocked/parked until the rebuild lands, then rebases normally onto
  it), or DROP (neither is wanted anymore — close both).

  LAND-REVIEW FINDINGS: <the bounce findings, verbatim>"
  rtk scripts/bd-dolt-push.sh
  # BOTH land/<id> and land/<dep> are KEPT (no delete) until the human resolves it.
  ```

  This is a superset of an ordinary escalate: it names the specific dependent and the specific
  question (fold/sequence/drop), so the human resolving it (see [Resolving a
  `land-escalated` branch](#resolving-a-land-escalated-branch)) has everything needed without
  re-deriving the stack relationship. `<dep>` itself is left exactly as it is — still whatever label
  it currently carries (it may not even be at `ready-for-land` yet) — this bounce escalation doesn't
  touch it.

**No descendants — the ordinary bounce.** I create the rebuild ticket first, then mark the original
superseded with **`bd supersede`** (the dedicated command — `supersedes` is **not** a `--deps` type):

```bash
NEW=$(rtk bd create --type=<same-type-as-original> \
  --title="<original title> (rebuild after land bounce)" \
  --description="Rebuild of <id>, bounced by /land semantic review.

REBUILD BRIEF (from land-review):
<the findings + what the rebuild must satisfy that the bounced branch did not>" \
  --json | jq -r '.id')

# Preserve epic parentage. If the bounced ticket was an epic's child, re-parent the rebuild onto
# the same epic BEFORE superseding — otherwise supersede closes the child and the epic loses it,
# reading falsely "complete" while the real work sits in an unlinked ticket. Re-parenting keeps the
# epic's completion accounting honest: the superseded child closes, but NEW is an open child, so the
# epic stays incomplete until the rebuild lands (and /epic-audit sees the real work).
PARENT=$(rtk bd show <id> --json | jq -r '.[0].dependencies[]? | select(.dependency_type=="parent-child") | .id' | head -1)
[ -n "$PARENT" ] && rtk bd dep add "$NEW" "$PARENT" --type=parent-child   # NEW becomes a child of the epic

# Re-point non-parent dependents. If OTHER tickets depend on <id> via a `blocks` edge (e.g. a
# diagnosis spike that gates its follow-ups), supersede CLOSES <id> — so bd treats that blocker as
# satisfied and those dependents unblock PREMATURELY, while the real work still sits unbuilt in NEW.
# Re-point each dependent onto NEW so the graph stays honest: they remain blocked until the rebuild
# lands. Same principle as the epic re-parent above — keep the dependency graph accurate across a
# supersede, not just the parentage.
for DEP in $(rtk bd show <id> --json | jq -r '.[0].dependents[]? | select(.dependency_type=="blocks") | .id'); do
  rtk bd dep add "$DEP" "$NEW"   # DEP now depends on the rebuild, not the superseded original
done

rtk bd supersede <id> --with "$NEW"   # links <id> -> NEW and AUTO-CLOSES <id> as superseded
rtk bd update <id> --remove-label ready-for-land   # tidy the queue label off the (now closed) original

rtk git push origin --delete "land/<id>"    # drop the rejected branch (a rebuild gets a fresh land/<new-id>)
rtk scripts/bd-dolt-push.sh                            # publish the new ticket + supersede over refs/dolt/data
```

`bd supersede` **closes** the original (with a reference to `NEW`) — superseded means *replaced*, and
`NEW` is the live work. That is the right outcome for a bounce: the bounced attempt is done-as-replaced,
not lingering open. (It is the one case where landing-side closes an `in_progress` producer ticket; a
normal **accept**/land closes via Section 4, an **escalate** never closes.)

### Branch disposition on a bounce — drop (default) vs. keep-for-lift (lode-02v)

Every bounce above deletes `land/<id>`. That's right whenever the finding is that the *content
itself* is what's wrong — nothing there is worth carrying forward. But it is not the only disposition
this skill has actually used: the lode-6qh/lode-96t resolution **kept** lode-96t's branch
(undocumented, until now) so the combined rebuild (lode-og3) could lift its error-handling
implementation verbatim rather than re-deriving it, because land-review had already judged that
content **sound on its own** — the branch just couldn't land *because of* something external to it
(its foundation, lode-6qh, was bounced out from under it).

**The rule:**

- **DROP (default)** — the bounce finding is about the branch's *own* content: an unmet acceptance
  criterion, a wrong approach, a violated invariant, silent scope creep. Nothing there survives review
  unchanged, so nothing is worth keeping. This is every ordinary bounce above.
- **KEEP-FOR-LIFT** — reserved for the fold resolution of a [strand escalation](#bounce--clear-failure)
  above: the *dependent's* branch (not the bounced base's) is kept when land-review — or the human
  resolving the escalation — judges its content independently sound, and the combined rebuild ticket
  says explicitly **"lift verbatim from `land/<dep>` @ `<sha>`"** rather than re-describing the same
  design from scratch (mirrors lode-og3's own FOLD-IN note:
  `git show <sha>:<path>` / `git diff <a>..<b>` pointers, not prose re-derivation). The **base's**
  branch (the one actually bounced) is still dropped — it was rejected for a reason, and folding
  doesn't rescue it.

**A kept branch is not GC'd for free — say so in the rebuild ticket.** Section 4's GC deletes
`land/$id` for each id in `$LANDED`, and a kept `land/<dep>` belongs to a ticket that was *superseded*,
not landed: it will never appear in `$LANDED`, so nothing deletes it automatically. The rebuild ticket
must therefore carry the disposal instruction alongside the lift pointers — **"lift verbatim from
`land/<dep>` @ `<sha>`; delete `land/<dep>` once this ticket lands"** — which is exactly what lode-og3's
FOLD-IN note does. Until then the branch is inert for landing (its ticket is closed, so it never
re-enters the queue) but it *does* stay live in `refs/remotes/origin/land/*` and therefore visible to
[1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd) — which is correct and
intended: a kept branch really does still contain its base's commits, and 1a enumerating **refs, not
tickets** is what keeps that visible.

## Escalate — genuine decision

An **escalate** is a real question only a human can answer (the ticket is ambiguous about "done";
acceptance is arguably met depending on an unrecorded decision; the branch took a
defensible-but-different approach; the ticket/branch is unidentifiable). I land **nothing** for it,
**keep its branch** for the human to pick up, mark it, and surface the question — **without blocking
the rest of the batch** (the accepted set still merges):

```bash
rtk bd update <id> --add-label land-escalated --remove-label ready-for-land \
  --append-notes "ESCALATION (/land semantic review): <the decision needed, with options as land-review framed them>"
rtk scripts/bd-dolt-push.sh
# origin/land/<id> is KEPT (no delete) until the human resolves it.
```

(A **stale-escalation sweep** — **surfacing**, not GC'ing, a `land-escalated` branch that has sat
unresolved unusually long — is a deferred refinement in `docs/decisions.md`, not part of v1. `/sweep`
already surfaces every open `land-escalated` item every pass regardless of age; a `land-escalated`
branch is never touched by an automated sweep — only the three human-driven resolution exits below
remove the label and let the branch go.)

## Resolving a `land-escalated` branch

`land-escalated` is **not terminal** — a human resolves it, and every resolution **removes the
label**, so `bd list --label land-escalated` can reach empty. Resolution is a human action taken
outside a `/land` pass — typically at `bd show <id>` time; `/land` only ever *sets* the label
(above), never clears it. There are exactly three exits:

### (a) Land as-is — materialize the decision first, then re-enter the queue

A `/land` escalation means `land-review` hit a genuine ambiguity it couldn't settle on its own (an
arguable "done", an unclear acceptance criterion). If the human decides the branch **should** land,
**the branch itself needs no change** — this exit is exactly the "it's fine as-is" case. What changes
is the *ticket*: swapping the label back to `ready-for-land` with nothing else touched is **not a
complete transition**, because `/land`'s next pass re-dispatches `land-review`, which hits the *same*
ambiguity and escalates again — an infinite escalate↔ready loop.

So the swap is valid only once the human has **written the decision into the ticket** — edit the
acceptance criteria / description so the ambiguity `land-review` flagged no longer exists — *then*
swap the label. `land-review` stays authoritative on re-review; there is deliberately **no
"human-blessed" bypass label** that skips it (forcing a land past a `land-review` objection is an
out-of-band manual act, not a designed fast-path):

```bash
rtk bd update <id> --acceptance="<revised, unambiguous acceptance criteria>"   # land-review reads
  # acceptance_criteria as the contract — this is the field that must change; add --description too
  # if the narrative text also needs updating. The BRANCH is untouched.
rtk bd update <id> --remove-label land-escalated --add-label ready-for-land
rtk scripts/bd-dolt-push.sh
# /land's NEXT pass re-runs land-review against the now-unambiguous ticket — same gate, no bypass.
```

### (b) Rebuild — supersede into a fresh ticket, drop the branch

If the human decides the branch must be rebuilt (the escalated approach was wrong, or the real
answer is "this needs a different design"), resolve it exactly like a `land-review`
[bounce](#bounce--clear-failure): open a new ticket carrying the decision, `bd supersede` the
original onto it, and drop the rejected branch (same epic re-parent / dependent re-point care as a
bounce applies here too).

**Before dropping the branch, run the same descendant check as Bounce (lode-02v):** a `land-escalated`
branch can have picked up a live stacked dependent while it sat waiting for a human, exactly like an
ordinary bounce candidate can — and a *stale* graph from the pass that escalated it is no use here, so
recompute [1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)'s relation
against the live refs and look `land/<id>` up in it. If it finds a live descendant, do **not** delete —
apply the same [fold/sequence/drop framing](#bounce--clear-failure) instead of proceeding blind, and
use the [keep-for-lift disposition](#branch-disposition-on-a-bounce--drop-default-vs-keep-for-lift-lode-02v)
rule if folding. No descendant found → proceed exactly as below.

```bash
NEW=$(rtk bd create --type=<same-type-as-original> \
  --title="<original title> (rebuild after land-escalated)" \
  --description="Rebuild of <id>. Human resolution of the land-escalated decision:
<the decision + what the rebuild must satisfy that the escalated branch did not>" \
  --json | jq -r '.id')
# re-parent onto the same epic / re-point blocking dependents — see Bounce above for why.
rtk bd supersede <id> --with "$NEW"          # closes <id> as superseded, links to $NEW
rtk bd update <id> --remove-label land-escalated
rtk git push origin --delete "land/<id>"     # drop the escalated branch — the rebuild gets a fresh land/<new-id>
rtk scripts/bd-dolt-push.sh
```

### (c) Drop — close with reason, GC the branch

If the human decides the work simply shouldn't happen (overtaken by events, no longer wanted),
close the ticket directly and GC the branch.

**Same descendant check before deleting (lode-02v)** — a dropped branch's commits are just as live
inside a dependent as a bounced branch's would be; recompute [1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)'s
relation against the live refs before the delete below. A live descendant found means dropping this branch also
strands that dependent's foundation — surface that to the human as part of this same decision (does
the dependent get dropped too, or does it need to be re-founded on something else?) rather than
deleting silently.

```bash
rtk bd close <id> --reason "<why this is dropped>"
rtk bd update <id> --remove-label land-escalated
rtk git push origin --delete "land/<id>"     # GC the branch — nothing will land it
rtk scripts/bd-dolt-push.sh
```

All three end the same way: **`land-escalated` is gone**, so a surfacer's queue — the forthcoming
`/sweep` (`lode-nps.1`) — can actually drain rather than growing monotonically.

**Scope.** The three exits above resolve the label as **`/land`** sets it (semantic-review escalation,
exit (a) re-entry = `ready-for-land`, as shown above). `/code`'s producers set the *same* label from
three other places — a `coding` build-time clarifying decision, a `code-reviewer` technical-review
escalation, and a `coding` rebase-pickup conflict. Exits **(b)** rebuild and **(c)** drop apply to all
four sources unchanged — they only close the ticket and GC the branch, and neither cares which gate
escalated. Exit **(a)** does **not** generalize to a single label: a build-time escalation never
reached `ready-for-code-review` (it never had its technical review), and a rebase-conflict escalation
still does not merge onto `trunk` — re-entering either blindly at `ready-for-land` would skip a gate
that has never actually run.

### Exit (a) per source — re-enter at the gate that escalated

DECISION (human, 2026-07-08, `lode-08g`): whichever gate could not resolve the ambiguity is the gate
that re-runs once the ambiguity is resolved — the same gate, against the now-unambiguous ticket, never
a later gate taking the resolution on faith.

| escalated by                             | exit (a) re-entry label |
|------------------------------------------|-------------------------|
| `/land` semantic review (`land-review`)  | `ready-for-land`        |
| `code-reviewer` technical review         | `ready-for-code-review` |
| `coding` rebase-pickup conflict          | `needs-rebase`          |
| `coding` build-time clarification        | `ready-for-code-review` |

The first row is exit (a) as defined above; the other three follow the same shape — write the decision
into the ticket first, then swap `land-escalated` for the row's label and publish:

```bash
rtk bd update <id> --append-notes "RESOLVED (human): <the decision>"
rtk bd update <id> --remove-label land-escalated --add-label <ready-for-code-review|needs-rebase>
rtk scripts/bd-dolt-push.sh
```

**The build-time case is the deliberately arguable one, decided rather than left implicit.** A
build-time escalation means the producer stopped mid-build, so its branch is green-but-possibly
**incomplete** and never reached `ready-for-code-review` on its own. Re-entering it there hands the
`code-reviewer` a branch that may not implement the whole ticket. That trade-off is accepted anyway —
routing every trivially-answerable build question through a full exit-(b) rebuild would over-charge a
question the branch itself may already answer correctly — under three conditions that make it safe
rather than silently risky:

1. The human writes the resolved answer into the ticket (`--append-notes`) **before** flipping the
   label, so the `code-reviewer` reads the resolved ambiguity rather than rediscovering it.
2. The `code-reviewer` is not obliged to pass a half-built branch: **escalate** is its standing
   non-pass outcome (revert to green, re-apply `land-escalated`), so a build-time re-entry asserts only
   that the *ambiguity* is resolved, not that the branch is *finished*. A still-incomplete branch
   escalates again on technical review — and `land-review`'s **bounce** verdict is the backstop if it
   slips past that too.
3. Re-entry at `ready-for-code-review` for the build-time source means "the decision is made, re-run
   the pipeline from technical review" — **not** "this branch is done."

---

## bd-sync discipline (non-negotiable)

I am the system's heaviest bd writer, and the repo runs **`import.auto: false`** (see `CLAUDE.md` /
`.beads/config.yaml`, fixed in lode-6ra): **Dolt is authoritative; `.beads/issues.jsonl` is an
export-only passive artifact, never a sync wire.** I honor that exactly:

- **Pull at the start, push after writes.** `bd dolt pull` opens the pass; `scripts/bd-dolt-push.sh`
  (a retry-on-reject wrapper around `bd dolt push` — backoff + re-pull between attempts, since a
  concurrent `/code` producer's write can transiently reject or lock-contend the push, lode-83d)
  follows *every* batch of bd writes (closes, bounce-creates, label changes). State travels
  cross-machine via `refs/dolt/data`, **never** via committed jsonl.
- **Never commit `.beads/issues.jsonl`, never `bd import` it.** I do not `git add` the jsonl export,
  and I never substitute `bd import` for `bd dolt pull` (import only upserts and silently misses
  deletions). When I merge a `land/<id>` branch, if it carried a jsonl diff I do **not** let it
  become a committed source of truth — Dolt + `bd dolt push` is the wire. (`import.auto: false`
  already stops the post-merge hook from re-importing a stale jsonl and reverting a close — the
  failure that bit lode-8bh / lode-wvf / lode-bxz; I do not re-enable that path.)
- **Never let the passive export block or enter a merge.** `bd dolt pull` can leave
  `.beads/issues.jsonl` *staged* (index != `HEAD`, worktree == index) — `git diff` reads clean in that
  state, so the drift is invisible until `git merge --no-ff` refuses it outright. I unstage it with
  `git restore --staged --worktree .beads/issues.jsonl` right before the Section 3 merge loop, every
  pass, on the assumption it may be staged even when `git diff` says otherwise.
- **Order so a close can't be reverted by a stale jsonl.** Push `trunk` and `bd close` the landed
  tickets, then `bd dolt push` to publish — the authoritative close lives in Dolt and is pushed
  immediately, never left to be overwritten by an intermediate committed jsonl on a later pull.

---

## What I never do

- **Land work I can't verify.** Drift (missing branch / SHA mismatch), a textual merge conflict (→
  `needs-rebase` kick-back), a red re-gate, or a `bounce` verdict all stop a branch from landing this
  pass.
- **Rebase a producer's branch myself, or run `land-review` on a branch that won't merge.** A branch
  that fails the 2b precheck is kicked back `needs-rebase` for the *producer* to rebase in its own
  worktree — I never touch a producer's worktree or rewrite its branch, and I don't spend a semantic
  review on contents a rebase will change.
- **Land on a `bounce`/`escalate`, or skip the semantic review.** The review is the *first* task per
  branch; only an `accept` enters the merge set.
- **Run two landers at once**, or run the loop on more than one machine — the local lock + one-machine
  convention is the v1 serialization guarantee.
- **Commit the passive `.beads/*.jsonl` export, or `bd import` it** in place of `bd dolt pull`.
- **Touch a producer's worktree, or record a design decision in a bd note** instead of `docs/` (that
  forks the record).
- **Delete a `land/<id>` branch (bounce, or exit-(b)/(c) of a `land-escalated` resolution) without
  first checking for a live descendant** (lode-02v). A branch that another live `land/<dep>` already
  merged in must not be silently dropped — see [Bounce](#bounce--clear-failure)'s descendant check.
- **Merge a stacked dependent before its base, or land a dependent whose base isn't in this pass's
  accepted set.** [Section 3a](#3a-order-the-accepted-set--base-before-dependent-hold-an-orphaned-dependent)
  orders the merge set and holds an orphaned dependent rather than merging it out of order.
- **Trust `builds_on` bd metadata as the mechanism for detecting a stacked branch.** It's a producer
  breadcrumb only; I always derive the stacked-branch graph from git containment
  ([1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)).

## Stop and report

When the pass ends I release the lock (the `trap`) and report: how many branches I reviewed; which
**landed** (with the `trunk` merge SHA, in merge order); which I **kicked back `needs-rebase`** (they
never reached `land-review`); which I **bounced** (and the new superseding ticket IDs); which I
**escalated** (and the decision each owes a human — including a bounce that turned into a strand
escalation, per [1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)/[Bounce](#bounce--clear-failure));
which I **held** as an orphaned stacked dependent (Section 3a) and what base it's waiting on; any
**epic** I flagged `epic-ready-to-audit` because this pass closed its last child; and anything that
**drifted**. On any
genuine ambiguity in the landing mechanics themselves — not a per-branch verdict, which `land-review`
owns — I stop and surface it rather than guess.
