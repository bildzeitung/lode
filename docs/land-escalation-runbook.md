# lode — resolving a `land-escalated` branch (runbook)

A human runbook, not an instruction a `/land` tick executes: how a person resolves a ticket that
`/land` (or a build-time escalation) parked with the `land-escalated` label. The `/land` skill
([`land/SKILL.md`](../.claude/skills/land/SKILL.md)) only ever *sets* that label; this page is the
procedure for clearing it. The section below was moved here verbatim from `land/SKILL.md`; only its
links into `land/SKILL.md` were repointed to that file's path.

## Resolving a `land-escalated` branch

`land-escalated` is **not terminal** — a human resolves it, and every resolution **removes the
label**, so `bd list --label land-escalated` can reach empty. Resolution is a human action taken
outside a `/land` pass — typically at `bd show <id>` time; `/land` only ever *sets* the label
(above), never clears it. There are exactly four exits:

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
bd update <id> --acceptance="<revised, unambiguous acceptance criteria>"   # land-review reads
  # acceptance_criteria as the contract — this is the field that must change; add --description too
  # if the narrative text also needs updating. The BRANCH is untouched.
# If resolving this escalation required a COMMIT to land/<id> — which it does whenever the decision
# belongs in docs/ rather than a bd field — refresh land_head, or Section 2a reads the commit you
# just made as DRIFT and prescribes a bounce next pass (OBSERVED: lode-y3dw, 2026-08-05). This exit
# is the exposed one because it re-enters at ready-for-land with no reviewer in between; exits (b)
# and (d) route through a code-reviewer, which refreshes land_head itself.
# --set-metadata (upsert), NOT --metadata (which takes a whole JSON blob and would drop
# land_summary/review_head — verified 2026-08-05).
# Omit this whole block if nothing was committed. Derived into a variable and shape-checked
# before the write (lode-uvjr) — a malformed value here reads as drift on a later pass.
LAND_HEAD="$(git rev-parse origin/land/<id>)"
scripts/validate-sha40.sh land_head "$LAND_HEAD" || exit $?
bd update <id> --set-metadata land_head="$LAND_HEAD"
bd update <id> --remove-label land-escalated --add-label ready-for-land
scripts/bd-dolt-push.sh
# /land's NEXT pass re-runs land-review against the now-unambiguous ticket — same gate, no bypass.
```

### (b) Rebuild — supersede into a fresh ticket, drop the branch

If the human decides the branch must be rebuilt (the escalated approach was wrong, or the real
answer is "this needs a different design"), resolve it exactly like a `land-review`
[bounce](../.claude/skills/land/SKILL.md#bounce--clear-failure): open a new ticket carrying the decision, `bd supersede` the
original onto it, and drop the rejected branch (same epic re-parent / dependent re-point care as a
bounce applies here too).

**Before dropping the branch, run the same descendant check as Bounce (lode-02v):** a `land-escalated`
branch can have picked up a live stacked dependent while it sat waiting for a human, exactly like an
ordinary bounce candidate can — and a *stale* graph from the pass that escalated it is no use here, so
recompute [1a](../.claude/skills/land/SKILL.md#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)'s relation
against the live refs and look `land/<id>` up in it. If it finds a live descendant, do **not** delete —
apply the same [fold/sequence/drop framing](../.claude/skills/land/SKILL.md#bounce--clear-failure) instead of proceeding blind, and
use the [keep-for-lift disposition](../.claude/skills/land/SKILL.md#branch-disposition-on-a-bounce--drop-default-vs-keep-for-lift-lode-02v)
rule if folding. No descendant found → proceed exactly as below.

```bash
NEW=$(bd create --type=<same-type-as-original> \
  --title="<original title> (rebuild after land-escalated)" \
  --description="Rebuild of <id>. Human resolution of the land-escalated decision:
<the decision + what the rebuild must satisfy that the escalated branch did not>" \
  --json | jq -r '.id')
# re-parent onto the same epic / re-point blocking dependents — see Bounce above for why.
bd supersede <id> --with "$NEW"          # closes <id> as superseded, links to $NEW
bd update <id> --remove-label land-escalated
git push origin --delete "land/<id>"     # drop the escalated branch — the rebuild gets a fresh land/<new-id>
scripts/bd-dolt-push.sh
```

### (c) Drop — close with reason, GC the branch

If the human decides the work simply shouldn't happen (overtaken by events, no longer wanted),
close the ticket directly and GC the branch.

**Same descendant check before deleting (lode-02v)** — a dropped branch's commits are just as live
inside a dependent as a bounced branch's would be; recompute [1a](../.claude/skills/land/SKILL.md#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)'s
relation against the live refs before the delete below. A live descendant found means dropping this branch also
strands that dependent's foundation — surface that to the human as part of this same decision (does
the dependent get dropped too, or does it need to be re-founded on something else?) rather than
deleting silently.

```bash
bd close <id> --reason "<why this is dropped>"
bd update <id> --remove-label land-escalated
git push origin --delete "land/<id>"     # GC the branch — nothing will land it
scripts/bd-dolt-push.sh
```

### (d) Amend and re-gate — fix the already-landed defect, keep the branch and ticket (`lode-wp2r`)

This exit has **two triggers** (widened by `lode-2m93`; `lode-wp2r`'s original trigger and rationale
are preserved below as the first of the two, unchanged):

**Trigger 1 — `/land`'s combined re-gate (`lode-wp2r`, the original trigger).** Applies when the
escalation was raised by `/land`'s **combined re-gate** (Section 3 of this skill), not by
`land-review` or a producer gate, and all of the following hold:

- `land-review` **accepted** this branch (no semantic objection — the escalation happened *after*
  review, at re-gate time).
- The merge precheck (2b) was clean — the branch merges onto `trunk` without conflict.
- The re-gate failure is traceable to code **already on `trunk`**, not to anything this branch
  introduces — i.e. the branch would have gated green before whatever landed the defect, and gates red
  now only because it is the first thing to exercise the defective landed code.

Under those conditions, neither of the other exits fits: "land as-is" is defined for a branch that
needs no change, and this one does; "rebuild" discards a branch that `land-review` already judged
sound, which is the wrong instrument for a defect that isn't the branch's fault.

**Trigger 2 — a `land-review` semantic-review escalation whose resolution requires a scoped on-branch
edit (`lode-2m93`).** The three combined-re-gate conditions above attach to trigger 1 only — they are
meaningless here, since there is no re-gate failure to trace. This trigger's sole condition is that
the human, resolving a `land-review` escalation, decides the fix requires editing the branch, rather
than landing it as-is, rebuilding it, or dropping it. See [Re-entry per escalating
source](#re-entry-per-escalating-source--re-enter-at-the-gate-that-escalated) for the discriminator
this draws against the unwidened `land-review` row, which still re-enters at exit (a).

Either way, the human amends the branch itself — a small, scoped fix — and sends it back through the
pipeline **one gate earlier than a normal "land as-is,"** at `ready-for-code-review` rather than
`ready-for-land`: the amendment is new, ungated content that `land-review`'s original accept never
saw, so it needs its own technical review (`code-reviewer`) before a semantic re-review is worth
spending. Its rows are in [Re-entry per escalating
source](#re-entry-per-escalating-source--re-enter-at-the-gate-that-escalated), alongside exit (a)'s.

**Write the added scope into the ticket's acceptance criteria, not only into a note** — exit (a)'s
"materialize the decision first" rule, for the same reason, and applying to both triggers. The
re-entered branch still has to clear `ready-for-land`, where `/land`'s next pass re-runs
`land-review`, which reads `acceptance_criteria` as the contract; an amendment recorded only in the
notes reads to that re-review as scope creep on a branch it already accepted.

```bash
bd update <id> --acceptance="<original criteria + what the amendment must satisfy>"
bd update <id> --remove-label land-escalated --add-label ready-for-code-review \
  --append-notes "RESOLVED (human, amend-and-re-gate): <the landed defect + the fix>"
scripts/bd-dolt-push.sh
# The human may amend land/<id> before the swap, or leave it to the code-reviewer that /code's
# stranded-review sweep dispatches. Either way it re-gates and re-pushes land/<id>.
```

First observed and resolved this way in `lode-pcee` (2026-07-28): `land-review` had accepted the
branch and the 2b precheck was clean, but the combined re-gate failed on a whole-file substring
predicate bug in `tests/test_gate_lib.py::_consumers()` that had landed separately (`lode-bss5`). The
human's resolution tightened that predicate directly on `land/lode-pcee`, re-entered at
`ready-for-code-review`, and the branch landed from there — the precedent this exit formalizes.

Every exit ends the same way: **`land-escalated` is gone**, so a surfacer's queue — the forthcoming
`/sweep` (`lode-nps.1`) — can actually drain rather than growing monotonically.

**Scope.** The exits above resolve the label as **`/land`** sets it — from `land-review`'s
semantic-review escalation (exit (a) re-entry = `ready-for-land` when the branch needs no change,
exit (d) re-entry = `ready-for-code-review` when the human's resolution requires a scoped on-branch
edit — `lode-2m93`) and, for exit (d), also from `/land`'s own combined re-gate. `/code`'s producers
set the *same* label from three other places — a `coding` build-time clarifying decision, a
`code-reviewer` technical-review escalation, and a `coding` rebase-pickup conflict. Exits **(b)**
rebuild and **(c)** drop apply to every source unchanged — they only close the ticket and GC the
branch, and neither cares which gate escalated. Exit **(a)** does **not** generalize to a single
label: a build-time escalation never reached `ready-for-code-review` (it never had its technical
review), and a rebase-conflict escalation still does not merge onto `trunk` — re-entering either
blindly at `ready-for-land` would skip a gate that has never actually run. Exit **(d)** does not apply
to those three producer sources either — not because a producer's gate can never go red on a defect
inherited from `trunk` (it can), but because a producer-side branch still has a live `coding` or
`code-reviewer` agent free to fix what it finds in-band. Exit (d) is for the positions where no agent
is left holding the branch: `/land`'s own combined re-gate, and a `land-review` escalation whose
resolution the human decides needs an on-branch edit.

### Re-entry per escalating source — re-enter at the gate that escalated

DECISION (human, 2026-07-08, `lode-08g`): whichever gate could not resolve the ambiguity is the gate
that re-runs once the ambiguity is resolved — the same gate, against the now-unambiguous ticket, never
a later gate taking the resolution on faith.

| escalated by                                                                 | exit | re-entry label          |
|------------------------------------------------------------------------------|------|-------------------------|
| `/land` semantic review (`land-review`), resolution needs **no** branch edit | (a)  | `ready-for-land`        |
| `/land` semantic review (`land-review`), resolution needs **a** branch edit  | (d)  | `ready-for-code-review` |
| `code-reviewer` technical review                                             | (a)  | `ready-for-code-review` |
| `coding` rebase-pickup conflict                                              | (a)  | `needs-rebase`          |
| `coding` build-time clarification                                            | (a)  | `ready-for-code-review` |
| `/land` combined re-gate (defect already on `trunk`)                         | (d)  | `ready-for-code-review` |
| `/land` §2a malformed `land_head` metadata (`lode-xdg3`)                     | (a)  | `ready-for-land`        |

The two `land-review` rows are written as an explicit **no branch edit / a branch edit** pair so they
cannot both match one escalation: every `land-review` escalation matches exactly one of them, decided
by the human's resolution.

The exit column names which resolution applies (`lode-08g` for the exit (a) rows, `lode-wp2r` for the
unwidened exit (d) row, `lode-2m93` for the new `land-review` + on-branch-edit exit (d) row). What
separates the two `land-review` rows is **not** the escalation, but a property of the human's
*resolution*: whether it requires editing the branch. That is the one judgement call this table asks
a human to make — `land-review` objecting is common to both rows, so the discriminator has to live in
what the human decides to do about it, not in which gate raised the escalation. (This is consistent
with the combined-re-gate row's own precedent: branch-immutability does not separate rows either — a
`needs-rebase` pickup merges `trunk` in, and a build-time re-entry hands a deliberately unfinished
branch to `code-reviewer`, so exit (a) rows change the branch too.) Every
row follows the same mechanical shape — write the decision into the ticket first, then swap
`land-escalated` for the row's label and publish:

```bash
bd update <id> --append-notes "RESOLVED (human): <the decision>"
bd update <id> --remove-label land-escalated --add-label <ready-for-code-review|needs-rebase>
scripts/bd-dolt-push.sh
```

**Re-entering at `ready-for-code-review` (the `code-reviewer` and `coding` build-time rows above)
MUST also (re)write `metadata.review_head` as part of this same resolution, before publishing
(lode-uomo).** This hand-edit happens outside any `/code` run, and nothing else on this path forces
that field to exist — `/code` step 1's stranded-review sweep will not dispatch a `code-reviewer`
until a non-empty `review_head` can be established (deliberately: it will not guess a head to
review), so omitting this step used to strand the ticket at `ready-for-code-review` forever,
`in_progress` and unreturned by `bd ready`, invisible to everything except a repeated "needs a human"
line in `/code`'s own report every pass. (OBSERVED: `lode-1fzq` — resolved by hand exactly like this,
after the omission had already stranded it.) Step 1 now derives the field itself from the live
`origin/land/<id>` tip when it's missing, but that is a **backstop**, not a substitute: set it here,
at resolution time, same as the build-time escalation path already does on the agent side
(`coding.md`, `lode-t83` Gap 1). Validate before writing, exactly as the backstop does — an
`ls-remote` that resolves nothing prints nothing, and an unguarded write would put an *empty*
`review_head` on the ticket, re-creating the very state this step exists to prevent:

```bash
SHA="$(git ls-remote origin "refs/heads/land/<id>" | cut -f1)"
scripts/validate-sha40.sh review_head "$SHA" && bd update <id> --set-metadata review_head="$SHA"
  # --set-metadata (upsert), NOT --metadata (a full-blob replace that silently drops
  # land_head/land_summary/other keys already on the ticket)
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

