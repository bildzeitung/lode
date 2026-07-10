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
[`land-review`](../land-review/SKILL.md) skill (the build-side twin of `debate`). The independence is
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
branch. It reads both sides (ticket acceptance/design vs. the actual diff against the merge-base),
judges on acceptance / scope / design+invariants / approach, and returns exactly one verdict with
findings:

- **accept** → add the ticket to the **merge set** for this pass.
- **bounce** (a clear, confident failure) → handle per [Bounce](#bounce--clear-failure) below: open a
  new ticket carrying the findings, supersede the original, **drop the branch**. The ticket leaves
  the merge set.
- **escalate** (a genuine decision only a human can make) → handle per
  [Escalate](#escalate--genuine-decision) below: land **nothing** for it, **keep the branch**, label
  it, surface the question. It never enters the merge set.

Collect verdicts for the whole queue before merging — I want the full accepted set so I can
**batch**-merge.

---

## 3. Batch-merge the accepted set, re-gate once, isolate on red

Two branches each green *in isolation* can break when **combined** (a clean git merge with broken
behaviour). So I merge the whole accepted set, then re-gate the combined `trunk` **once**:

**Pre-compute every merge message before the first merge — no `bd` call inside the merge loop.** The
`<summary>` in each commit message comes from `bd show <id> --json` (`metadata.land_summary` / title),
and *any* `bd` read regenerates the passive `.beads/issues.jsonl` export and leaves it **staged** — so
a per-iteration `bd show` re-dirties the tree after the very first merge, and every merge from the
second one on hits the same staged-jsonl failure the pre-loop restore below exists to prevent. One
`bd` read pass over the accepted set, cached, keeps every `bd` call **outside** the loop that follows
(the same rule already applies to the Section 4 GC loop's per-iteration `bd show`):

```bash
declare -A MSG
for id in $ACCEPTED; do
  SUMMARY=$(rtk bd show "$id" --json | jq -r '.[0].metadata.land_summary // .[0].title')
  MSG[$id]="Merge land/$id: $SUMMARY ($id)"
done
```

Before merging anything, unstage the passive jsonl export — the reads above can re-dirty it exactly
like `bd dolt pull` (Section 1) does. `.beads/issues.jsonl` staged means its index blob differs from
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

- **Green** → proceed to [Land the survivors](#4-land-the-survivors).
- **Red** → **isolate**. The combined merge is bad but I don't yet know which branch. Reset `trunk`
  back to `origin/trunk` and replay the accepted set **one at a time**, re-gating after each; keep
  every branch that stays green, and **bounce** the first that turns the gate red (→ new ticket, drop
  branch), then continue with the rest:

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
  rtk git push origin --delete "land/$id"   # GC the merged remote branch

  # GC the local builder worktree (best-effort — only on the machine that built it).
  # The builder records review_worktree/review_branch; once the work is on trunk the
  # worktree and its branch are dead weight (this is the accumulation cleanup).
  # NOTE: plain git here, not rtk — rtk reformats `worktree list --porcelain`, so an
  # rtk-piped guard never byte-matches "worktree $WT" and silently no-ops forever (lode-9j7).
  WT=$(rtk bd show "$id" --json | jq -r '.[0].metadata.review_worktree // empty')
  if [ -n "$WT" ] && git worktree list --porcelain | grep -qxF "worktree $WT"; then
    BR=$(rtk bd show "$id" --json | jq -r '.[0].metadata.review_branch // empty')
    git worktree remove --force "$WT"            # the build artifact is on trunk now — force is safe
    [ -n "$BR" ] && git branch -D "$BR" 2>/dev/null || true
  fi
done

# Backstop: catch any dangling agent-* OR land/<id> worktree the per-ticket loop above
# missed — a stale/missing review_worktree pointer, a build that never got GC'd on its own
# machine, a reviewer/rebase-pickup worktree from a multi-cycle review that no ticket's
# single review_worktree field can point at (lode-r78 — the reviewer and a rebase pickup
# each check `land/<id>` out into their OWN fresh worktree per lode-k5e/lode-8k3, so a
# ticket reviewed more than once leaves extra land/<id>-branched worktrees the per-ticket
# net never sees), or (historically) this section's own rtk-mangled-porcelain bug. Walk the
# raw porcelain blocks directly (not the per-ticket review_worktree path), so a worktree
# with no matching ticket, or a ticket with wrong metadata, still gets reclaimed. Skip
# anything `locked` — that's the git-native in-use signal, and it's load-bearing here: a
# currently-running sibling worktree whose branch hasn't diverged from trunk yet is
# trivially "merged" into trunk by content identity, so `locked` must gate this even
# though `merged` alone looks sufficient. `merged` is the same safety invariant the
# per-ticket removal above already relies on ("the build artifact is on trunk now —
# force is safe") — for a `land/<id>` worktree specifically, `merged` is what proves the
# ticket already landed (an in-flight `ready-for-code-review`/`ready-for-land` ticket's
# branch has not merged into trunk yet, so its worktree is excluded regardless of lock
# state). This `locked` check used to be a no-op in practice: nothing on the
# producer side ever raised it, so every producer build was "merged" (trivially, by zero
# divergence) and reclaimable from the moment its worktree was created until its first
# commit -- this destroyed two builds' uncommitted work outright (branch and all, not
# just the checkout) before the gap was understood (lode-oqr). `.claude/agents/coding.md`
# now locks the worktree as the producer's first action and unlocks it right after its
# first commit, closing that window; this loop's `locked` filter needed no change. The
# one accepted trade-off: a crash strictly between lock and first commit leaves a locked
# worktree this sweep won't auto-reclaim -- rare (a normal build commits within minutes)
# and resolved by a manual `git worktree unlock` (or a future cleanup ticket), not by this
# loop, since correctness (never destroy a live build) matters more here than eagerness.
MERGED=$(git branch --merged trunk --format='%(refname:short)')
git worktree list --porcelain | awk '
  /^worktree / { path=$2; branch=""; locked=0 }
  /^branch refs\/heads\// { branch=substr($0,19) }
  /^locked/ { locked=1 }
  /^$/ { if (path!="" && (branch ~ /^worktree-agent-/ || branch ~ /^land\//) && !locked) print path"\t"branch; path="" }
' | while IFS=$'\t' read -r WT BR; do
  printf '%s\n' "$MERGED" | grep -qxF "$BR" || continue
  git worktree remove --force "$WT"
  git branch -D "$BR" 2>/dev/null || true
done
git worktree prune          # drop any now-stale worktree admin entries

# Second backstop: dangling local land/<id> refs with no worktree attached at all (so the
# loop above never even considered them) and no remote counterpart left (lode-r78). The
# per-ticket removal above only runs `git branch -D` when it also found a matching
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
if REMOTE_LAND=$(git ls-remote --heads origin 'land/*' 2>/dev/null); then
  REMOTE_LAND=$(printf '%s\n' "$REMOTE_LAND" | sed 's#^.*refs/heads/##')
  git for-each-ref --format='%(refname:short)' 'refs/heads/land/*' | while read -r BR; do
    printf '%s\n' "$REMOTE_LAND" | grep -qxF "$BR" && continue   # remote still exists — keep
    git branch -D "$BR" 2>/dev/null || true
  done
fi
```

`bd close` unblocks dependents — that is *why* the lander closes (the producer never does): a closed
ticket frees the next layer of `bd ready`. Closing is mine because the merge decision is mine.

The worktree GC is **best-effort and machine-local**: builds can happen on several machines, but
`review_worktree` is an absolute path on the *build* machine, so the `git worktree list` guard simply
skips any ticket whose worktree isn't registered here — the lander never errors on a worktree it can't
see, and the build machine's own `/land` (or a later sweep there) reclaims it. I GC a worktree only on
a clean **land**; a **bounce** drops the branch but the rebuild ticket may still want the tree, and an
**escalate** keeps everything until the human resolves it. The end-of-pass backstop sweep is a second,
independent net over the same machine's worktrees: it doesn't consult any ticket's metadata, so it
also reclaims a `worktree-agent-*` **or `land/<id>`** worktree whose `review_worktree` pointer went
stale or was never recorded — it only requires the worktree to be **unlocked** (no in-flight agent owns
it) and its branch already **merged into trunk** (the work is safely captured elsewhere). "Unlocked" is
a real signal now, not a formality: a producer (`.claude/agents/coding.md`) locks its worktree the
instant it starts building and unlocks it right after its first commit, so this sweep only ever finds a
`worktree-agent-*` worktree unlocked once its build has either not started or already diverged from
`trunk` — never mid-build with uncommitted, unreclaimed-elsewhere work sitting in it (lode-oqr). The
`land/<id>` half of the match is the reviewer's and a rebase pickup's *own* launch worktree, per the
lode-k5e/lode-8k3 architecture (they `git fetch origin land/<id> && git checkout -B land/<id>
FETCH_HEAD` instead of driving the builder's worktree) — a ticket reviewed across more than one cycle
leaves *extra* such worktrees no single `review_worktree` field can name, so the backstop is the only
net that ever reclaims them (lode-r78); `merged`+`unlocked` excludes an in-flight one exactly as it
excludes an in-flight `worktree-agent-*` one. A **separate** pass right after the worktree sweep (see
the script above) deletes any local `land/<id>` **branch ref** whose `origin/land/<id>` counterpart no
longer exists — the per-ticket removal only deletes a local branch when it also found an attached
worktree, so a bare ref with no worktree (e.g. `git worktree remove`d by some other path) would
otherwise linger forever once its remote is gone.

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
Rebase land/<id> onto current trunk in the build worktree, re-gate, force-push the branch, refresh
metadata.land_head, then swap needs-rebase back to ready-for-land."
rtk scripts/bd-dolt-push.sh       # publish the label swap + note over refs/dolt/data
# The branch is KEPT (no delete). The build worktree is KEPT. No supersede, no new ticket, no close.
```

Unlike a bounce, I **do not** supersede, create a rebuild ticket, or drop the branch, and unlike an
escalate there's no question for a human — the producer just replays its own already-reviewed work
onto the new `trunk`. The ticket stays `in_progress`; the `needs-rebase` label (not `ready-for-land`)
is now its state. **`/code` picks this up automatically** (lode-wfl): every invocation sweeps
`bd list --label needs-rebase --status in_progress` first and dispatches a `coding` producer to rebase
`land/<id>` onto current `trunk`, re-gate, force-push, and swap the label straight back to
`ready-for-land` — no human nudge needed unless the rebase itself conflicts (that escalates,
`land-escalated`, same as any other genuine decision).

## Bounce — clear failure

A **bounce** is a confident "this branch should not land as-is" (an unmet acceptance criterion,
silent scope creep, a violated invariant, a wrong approach — or drift/conflict from Sections 2a/3).
The original ticket is **superseded** by a fresh ticket that carries the `land-review` findings, so a
producer can rebuild from a clean brief. I create the rebuild ticket first, then mark the original
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
bounce applies here too):

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
close the ticket directly and GC the branch:

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

## Stop and report

When the pass ends I release the lock (the `trap`) and report: how many branches I reviewed; which
**landed** (with the `trunk` merge SHA); which I **kicked back `needs-rebase`** (they never reached
`land-review`); which I **bounced** (and the new superseding ticket IDs); which I **escalated** (and
the decision each owes a human); any **epic** I flagged `epic-ready-to-audit` because this pass closed
its last child; and anything that **drifted**. On any
genuine ambiguity in the landing mechanics themselves — not a per-branch verdict, which `land-review`
owns — I stop and surface it rather than guess.
