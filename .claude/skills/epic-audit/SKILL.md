---
name: epic-audit
description: Closing-side completion gate for an epic — the mirror of `debate`. When an epic's children have all closed, review the delivered set against the epic's goals and acceptance criteria: are there gaps, dropped scope, or child tickets that closed inconsistent with the epic's intent? Actionable gaps are filed as child tickets (they flow into `/code`); judgment calls are escalated to a human. Runs once per completed epic, or as a `/loop` sweep. Examples — "/epic-audit", "/epic-audit lode-mkc", "audit the completed epics", "/loop 30m /epic-audit".
---

# epic-audit

I am lode's **closing-side epic gate** — the mirror of [`debate`](../debate/SKILL.md). Where `debate`
stress-tests an epic *before* it is built, I review it *after* every child has closed: did the delivered
set actually satisfy the epic's goals, or did something quietly fall through the cracks? This is the check
you have been doing by hand with `/debate` on finished epics, made autonomous.

Unlike `debate` (which only reports to you), I am allowed to **write bd** — but narrowly and by an explicit
disposition rule: a **clear, actionable gap** becomes a child ticket that flows into `/code`; a **genuine
judgment call** is **escalated to a human**, never guessed into speculative work. On a clean bill I simply
mark the epic reviewed and move on.

I run once per pass and stop. I do **not** merge, close product tickets, touch `trunk`, or dispatch other
agents.

## How I'm triggered

Detection and review are split (the decided design). `/land` is the single writer that closes the last
child, so it is the one that *notices* completion: when it closes an epic's final `parent-child` child it
labels the epic **`epic-ready-to-audit`**. That label is my fast-path work signal — but it is only a hint.
My real gate is the **live child-completion state**, so I also catch epics the label missed (a child closed
by hand, or an epic that completed before this mechanism existed).

- **Bare `/epic-audit`** — sweep: audit every epic that is *auditable* (below), preferring `epic-ready-to-audit`
  ones. If none are auditable, that's a clean no-op — say so and stop.
- **`/epic-audit <epic-id>`** — audit exactly that epic now, regardless of label (manual invocation).

## 1. Setup — Dolt-authoritative

I write bd (gap tickets, escalations, label changes), so I follow the same sync discipline as `/land`:
**Dolt is authoritative; `.beads/issues.jsonl` is an export-only artifact, never a sync wire** (see
`CLAUDE.md` / lode-6ra). Pull at the start, push after every batch of writes.

```bash
rtk bd dolt pull
```

## 2. Select the auditable epics

An epic is **auditable** when ALL hold:

- `issue_type == "epic"` and `status != "closed"` (I never re-open or audit a closed epic), and
- it has **≥1** `parent-child` child, and **every** such child is `closed`, and
- it is **not** already labeled **`epic-audited`** (idempotency — I never re-review or file duplicate gaps).

```bash
# Fast path — epics /land flagged:
rtk bd list --type=epic --label epic-ready-to-audit --status open --json
# Safety net — any open epic whose children are all closed but which was never flagged:
rtk bd list --type=epic --status open --exclude-label epic-audited --json
```

For each candidate, confirm the child-completion gate from live state (the label is not trusted on its own):

```bash
rtk bd show <epic> --json | jq -r '
  .[0] as $e |
  (($e.dependents // []) | map(select(.dependency_type=="parent-child"))) as $kids |
  if ($e.issue_type=="epic") and ($e.status!="closed")
     and (($kids|length)>0) and (all($kids[]; .status=="closed"))
  then "AUDITABLE" else "SKIP" end'
```

## 3. Read the whole epic before forming an opinion

Same discipline as `debate` §1 — no verdict until I've read all of it:

- The epic's `description`, `acceptance_criteria`, `design`, and `notes` — this is the **intent** I judge against.
- **Every** child: `bd show <child> --json`. Read what it actually delivered — its own acceptance, its
  `close_reason` (why `/land` closed it), any supersede/bounce history. A child that closed via `bd supersede`
  during a land bounce handed its real work to a rebuild ticket — `/land` re-parents that rebuild onto this
  epic, so it normally shows up as an open child and correctly keeps the epic incomplete. Still worth a glance:
  if a superseded child's rebuild is *not* a child here, the epic can read "complete" while work is still open.
- Cross-check against the source of truth in `docs/` (start with `docs/design.md`) — an epic whose delivery
  drifted from a settled decision is a finding.

## 4. Review — did the delivered set complete the epic?

I judge on the completion axes (the closing-side of `debate`'s lens):

- **Acceptance met?** Walk each clause of the epic's `acceptance_criteria`. Is each one actually satisfied by
  a closed child's delivered work — not merely "all children closed"? An unmet clause with no ticket covering
  it is the primary thing I file.
- **Dropped / silently narrowed scope?** Did the epic's `description` promise something no child delivered?
  Was a child re-scoped or superseded in a way that dropped part of the goal without a replacement?
- **Consistency with intent.** Did any child close in a way that *contradicts* the epic (a stub/refusal left
  in, a decision recorded in a bd note instead of `docs/`, an invariant the epic depended on now violated)?
- **Coherence of the whole.** The children each passed their own gate; do they add up to the working capability
  the epic describes, or are there seams between them that nobody owns?

Be precise, skip the clean parts, and don't manufacture findings to look thorough — a clean bill is a valid,
common outcome.

## 5. Disposition — file, escalate, or nothing

For each finding, apply the rule you set:

### Actionable gap → file a child ticket (flows into `/code`)

When the fix is *clear enough to hand a builder* — a good bug report: what's missing, why, and what "done"
looks like. File it as a child of the epic so it surfaces in `bd ready` and flows through the normal pipeline.
Use `--no-inherit-labels` so it does **not** pick up `epic-audited`/`epic-ready-to-audit` from the parent:

```bash
rtk bd create --type=<task|bug|feature> --parent=<epic> --no-inherit-labels --label=epic-audit-gap \
  --title="<what's missing> (epic-audit gap on <epic>)" \
  --description="Found by /epic-audit closing-side review of <epic>.

GAP: <what the epic promised / an acceptance clause requires that no closed child delivered>
EVIDENCE: <the acceptance clause + which children were expected to cover it>
DONE WHEN: <the observable condition that closes this gap>" \
  --acceptance="<measurable success condition>"
```

### Needs a human call → escalate (do not guess work)

When the finding is a *judgment call* — is this acceptance clause even in scope? was this re-scope intended?
does "done" depend on an unrecorded decision? — I do **not** invent a ticket. I raise a `decision` ticket
carrying the **`human`** label, which is lode's escalation mechanism (`bd human list` surfaces it, `bd human
respond`/`dismiss` resolves it):

```bash
rtk bd create --type=decision --parent=<epic> --no-inherit-labels --label=human,epic-audit-gap \
  --title="DECISION: <the question> (epic-audit on <epic>)" \
  --description="/epic-audit needs a human call before it can file work for <epic>.

QUESTION: <the ambiguity — stated as a decision, with the options as I see them>
WHY IT'S NOT AUTO-FILEABLE: <what makes this judgment, not a clear bug report>"
```

This matches the project's escalation norm: **self-correct autonomously; surface only a real decision or a
'making it worse'** — I escalate the genuine decision and auto-file only the unambiguous work.

### Clean bill → nothing to file

If the epic is fully delivered, I file nothing. I still mark it reviewed (§6) so it isn't swept again.

## 6. Mark the epic reviewed (idempotency) and publish

However the review came out, retire the work signal and stamp the epic so no future pass re-audits it or
re-files the same gaps:

```bash
rtk bd label add <epic> epic-audited
rtk bd label remove <epic> epic-ready-to-audit   # drop the work signal (no-op if it wasn't set)
rtk bd dolt push                                 # publish gap tickets + escalations + label changes over refs/dolt/data
```

`epic-audited` is terminal for me: an audited epic is out of my sweep for good. (Re-auditing after the filed
gaps are themselves built is a fresh, explicit `/epic-audit <epic>` — I don't re-arm automatically.) The epic
itself stays **open** — closing it is the human's call once the audit's gaps are resolved; I never close an epic.

## What I never do

- **Close an epic, close a child, merge, or write `trunk`.** I file and escalate; landing/closing stays with
  `/land` and you.
- **Auto-file a judgment call.** Ambiguous → `human`-labeled `decision` ticket, never speculative work.
- **Re-audit an `epic-audited` epic** in a sweep, or file duplicate gaps.
- **Commit or `bd import` the passive `.beads/*.jsonl`** in place of `bd dolt push` — Dolt is the wire.
- **Record a design decision in a bd note** instead of `docs/` (that forks the record — a gap that is really
  a design question is an escalation, and its resolution lands in `docs/`).

## Stop and report

When the pass ends I report, per epic audited: **clean** (nothing filed), or the gap tickets I **filed** (IDs)
and the questions I **escalated** (IDs, each owing a human a decision). If a sweep found nothing auditable, I
say so plainly and stop.
