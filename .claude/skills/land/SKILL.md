---
name: land
description: Drain the ready-for-land queue — the SINGLE owner of every write to `trunk`. Per pass: semantic-review each `ready-for-land` branch (via the `land-review` skill) → accept | bounce | escalate; batch-merge the accepted set `--no-ff` into `trunk`, re-gate once, isolate the culprit on red; then push `trunk`, `bd close` the landed tickets, `bd dolt push`, and GC the merged `land/<id>` branches. Bounces open a new linked ticket carrying the findings; escalations leave the branch for a human and land nothing. Run self-paced as `/loop 5m /land` on ONE machine; a local lockfile guard skips a tick that would overlap a still-running land. Producers (`/code`) never land their own work — this skill does. Examples — "/land", "/loop 5m /land", "drain the ready-for-land queue", "land the reviewed branches".
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

## 2. Semantic review first — per branch, accept | bounce | escalate

For **each** `ready-for-land` ticket, in this order:

### 2a. Re-validate that beads and git haven't drifted

The landing context is **minimal by design** — `head_sha` + a one-line `summary` in bd metadata,
read via `bd show <id> --json` (the branch name is *derived*, `land/<id>`, never stored). The SHA
exists only to **detect drift**: a push onto the branch *after* the ticket was marked ready.

```bash
rtk bd show <id> --json     # read metadata.head_sha and metadata.summary
rtk git ls-remote origin "refs/heads/land/<id>"   # branch must still exist on origin...
# ...and origin/land/<id>'s tip SHA must equal metadata.head_sha
```

A **missing branch** or a **SHA mismatch** is drift — treat it exactly like a review **bounce**
(below): I will not land a branch I can't verify is the reviewed one.

### 2b. Run the semantic gate

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

```bash
# On trunk, accepted set = the IDs land-review accepted this pass.
for id in $ACCEPTED; do
  rtk git merge --no-ff "origin/land/$id" -m "Merge land/$id: <summary> ($id)"
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
    rtk git merge --no-ff "origin/land/$id" -m "Merge land/$id: <summary> ($id)"
    if rtk nox -s tests; then
      :                          # survivor — keep it merged
    else
      rtk git reset --hard HEAD~1   # back the culprit out
      # → bounce <id> (Section "Bounce"); it does NOT land this pass
    fi
  done
  ```

  The survivors stay merged on local `trunk`; the culprit is bounced like any other failure. (If a
  merge raises a **textual conflict**, that branch can't cleanly combine — `git merge --abort` and
  bounce it.)

---

## 4. Land the survivors

Only now — combined `trunk` is green — do I write the world. Order matters (see
[bd-sync discipline](#bd-sync-discipline-non-negotiable)): push `trunk` first, then close, then
publish bd state, then GC branches.

```bash
rtk git add -A -- ':!.beads' && rtk git commit -q -m "style: nox -t fix on merged trunk" || true   # commit any re-gate reformat (skip if clean); the ':!.beads' pathspec keeps the passive jsonl export OUT of the commit
rtk git push origin trunk
rtk git status                 # MUST show trunk up to date with origin

for id in $LANDED; do
  rtk bd close "$id" --reason "Landed on trunk via /land (merge <sha>)"
done

rtk bd dolt push               # publish the closes (and any bounce tickets) over refs/dolt/data — durable, cross-machine

for id in $LANDED; do
  rtk git push origin --delete "land/$id"   # GC the merged branch
done
```

`bd close` unblocks dependents — that is *why* the lander closes (the producer never does): a closed
ticket frees the next layer of `bd ready`. Closing is mine because the merge decision is mine.

---

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

rtk bd supersede <id> --with "$NEW"   # links <id> -> NEW and AUTO-CLOSES <id> as superseded
rtk bd update <id> --remove-label ready-for-land   # tidy the queue label off the (now closed) original

rtk git push origin --delete "land/<id>"    # drop the rejected branch (a rebuild gets a fresh land/<new-id>)
rtk bd dolt push                            # publish the new ticket + supersede over refs/dolt/data
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
rtk bd dolt push
# origin/land/<id> is KEPT (no delete) until the human resolves it.
```

(A stale-escalation sweep to GC long-abandoned `land/<id>` branches is a deferred hygiene task in
`docs/decisions.md`, not part of v1.)

---

## bd-sync discipline (non-negotiable)

I am the system's heaviest bd writer, and the repo runs **`import.auto: false`** (see `CLAUDE.md` /
`.beads/config.yaml`, fixed in lode-6ra): **Dolt is authoritative; `.beads/issues.jsonl` is an
export-only passive artifact, never a sync wire.** I honor that exactly:

- **Pull at the start, push after writes.** `bd dolt pull` opens the pass; `bd dolt push` follows
  *every* batch of bd writes (closes, bounce-creates, label changes). State travels cross-machine via
  `refs/dolt/data`, **never** via committed jsonl.
- **Never commit `.beads/issues.jsonl`, never `bd import` it.** I do not `git add` the jsonl export,
  and I never substitute `bd import` for `bd dolt pull` (import only upserts and silently misses
  deletions). When I merge a `land/<id>` branch, if it carried a jsonl diff I do **not** let it
  become a committed source of truth — Dolt + `bd dolt push` is the wire. (`import.auto: false`
  already stops the post-merge hook from re-importing a stale jsonl and reverting a close — the
  failure that bit lode-8bh / lode-wvf / lode-bxz; I do not re-enable that path.)
- **Order so a close can't be reverted by a stale jsonl.** Push `trunk` and `bd close` the landed
  tickets, then `bd dolt push` to publish — the authoritative close lives in Dolt and is pushed
  immediately, never left to be overwritten by an intermediate committed jsonl on a later pull.

---

## What I never do

- **Land work I can't verify.** Drift (missing branch / SHA mismatch), a textual merge conflict, a
  red re-gate, or a `bounce` verdict all stop a branch from landing this pass.
- **Land on a `bounce`/`escalate`, or skip the semantic review.** The review is the *first* task per
  branch; only an `accept` enters the merge set.
- **Run two landers at once**, or run the loop on more than one machine — the local lock + one-machine
  convention is the v1 serialization guarantee.
- **Commit the passive `.beads/*.jsonl` export, or `bd import` it** in place of `bd dolt pull`.
- **Touch a producer's worktree, or record a design decision in a bd note** instead of `docs/` (that
  forks the record).

## Stop and report

When the pass ends I release the lock (the `trap`) and report: how many branches I reviewed; which
**landed** (with the `trunk` merge SHA); which I **bounced** (and the new superseding ticket IDs);
which I **escalated** (and the decision each owes a human); and anything that **drifted**. On any
genuine ambiguity in the landing mechanics themselves — not a per-branch verdict, which `land-review`
owns — I stop and surface it rather than guess.
