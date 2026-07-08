---
name: sweep
description: The third `/loop` leg — a SURFACE-ONLY human-decision surfacer. Scans bd for work that has stopped waiting on a human and nothing else consumes (`land-escalated` branches, `human`-labeled decision tickets, epics ready for a human close-decision), dedups against a durable cross-machine digest issue, and surfaces new items. Writes no `trunk`, makes no decisions, dispatches no builders/landers/auditors. Run self-paced as `/loop 30m /sweep`. Examples — "/sweep", "/loop 30m /sweep", "what needs a human decision right now?", "sweep the human-decision queue".
---

# sweep

I am lode's **human-decision surfacer** — the third `/loop` leg, alongside `/code` and `/land`
(and `/epic-audit`). `/code` sweeps `needs-rebase` back to a producer; `/land` bounces re-enter
`bd ready`; `/epic-audit` runs itself as `/loop /epic-audit`. The only pipeline outputs with **no
downstream consumer** are the ones parked for a human: a `land-escalated` branch, a `human`-labeled
decision ticket, and an epic that's `epic-audited` + open + every child closed. Nothing pings a
human when work parks on one of these — you only find it by manually running `bd`. I turn that
silence into an active surface.

I am the **lowest-privilege** loop leg, deliberately: I write **one** self-owned bookkeeping issue
(a running digest) and nothing else. The full design record — why this exists, what was debated,
and the decisions that shaped it — lives in the epic `lode-nps` and its children; `bd show lode-nps
--json` is the source-of-record if you need the history.

## How I'm triggered

I take no arguments — there is nothing to scope to, I sweep the whole human-decision queue every
pass. Typically run self-paced as **`/loop 30m /sweep`** (escalations are exceptions and epics
complete rarely, so a slow tick is fine), or invoked ad hoc as bare `/sweep`.

## Non-goals — hold the line

- **Never writes `trunk`.** That is `/land` alone.
- **Never builds, lands, or audits.** Not `/code`, `/land`, or `/epic-audit` — `/epic-audit` already
  sweeps itself via `/loop /epic-audit`; I dispatch no agent of any kind.
- **Never makes or auto-defaults the human's decision.** Surface only — the escalation-autonomy /
  wait-when-away norms apply: I report, I never guess a resolution on the human's behalf.
- **Does not own the livelock caps** (bounce-lineage, rebase-attempt). Those are deferred design
  (see `lode-nps`'s Design field / `docs/decisions.md`); I only make their escalations visible.
- **Does not touch `.beads/issues.jsonl`.** `import.auto: false` is a hard invariant (lode-6ra) —
  I never `git add`, commit, or `bd import` it.
- **Never claims work off `bd ready`** and needs no worktree — every step here is `bd` + `git`
  plumbing run from wherever I'm invoked; I write no repo files (the one doc exception, recording
  my own notification-channel decision, is a separate one-time change in `docs/decisions.md`, not
  a per-pass action).

## 0. Setup — Dolt-authoritative

Same sync discipline as every other bd-writing loop leg: pull fresh state before reading, push
after writing.

```bash
rtk bd dolt pull
```

## 1. Collect the human-decision queue

Two sources, per the epic's decided scope. I defensively exclude my own digest issue from the
`land-escalated` query (it should never carry that label, but cheap insurance costs nothing):

```bash
ESCALATED=$(rtk bd list --label land-escalated --exclude-label sweep-digest --json \
  | jq -r '.[] | "\(.id)\tland-escalated\t\(.title)"')

HUMAN=$(rtk bd human list --status open --json \
  | jq -r '.[] | "\(.id)\thuman\t\(.title)"')
```

If either `bd` call errors, note the failure and treat that source as empty for this pass rather
than aborting — see [Failure handling](#failure-handling-a-substep-fails-the-loop-survives).

## 2. Collect epics ready for a human close-decision

An epic qualifies when it is `epic-audited` + still `open` + **every** `parent-child` child is
`closed` — `/epic-audit` deliberately never closes an epic itself, so nothing else flags these.
Same child-completion check `/epic-audit` uses:

```bash
CLOSABLE=""
for e in $(rtk bd list --type=epic --label epic-audited --status open --json | jq -r '.[].id'); do
  ROW=$(rtk bd show "$e" --json | jq -r '
    .[0] as $e |
    (($e.dependents // []) | map(select(.dependency_type=="parent-child"))) as $kids |
    if (($kids|length)>0) and (all($kids[]; .status=="closed"))
    then "\($e.id)\tepic-ready-to-close\t\($e.title)" else empty end')
  [ -n "$ROW" ] && CLOSABLE="${CLOSABLE}${ROW}
"
done
```

## 3. Build the current queue (dedup on stable IDs)

```bash
CURRENT=$(printf '%s\n%s\n%s\n' "$ESCALATED" "$HUMAN" "$CLOSABLE" | sed '/^$/d' | sort -u -t$'\t' -k1,1)
```

Each line is `<id>\t<kind>\t<title>` — `<id>` is the dedup key throughout.

## 4. Find-or-create the digest issue (locator = reserved label `sweep-digest`)

The dedup state lives **in the digest issue itself** (Dolt-durable, travels cross-machine) — a
scratchpad state file was explicitly rejected during design because it re-notifies the whole queue
from a second machine. The digest issue is found by a **reserved label**, not a remembered ID:

```bash
DIGEST_ROWS=$(rtk bd list --label sweep-digest --all --json)
N=$(echo "$DIGEST_ROWS" | jq 'length')
```

- **`N == 0`** — bootstrap. Only create it if `$CURRENT` is non-empty (an empty queue with no prior
  digest is a clean no-op — nothing to bootstrap, nothing to write). Immediately claim it so it
  never appears in `bd ready` (it is bookkeeping, not buildable work — the same "claimed tickets
  are out of `bd ready`" convention the coding loop relies on for `ready-for-code-review`/
  `ready-for-land`):

  ```bash
  DIGEST_ID=$(rtk bd create --type=chore --title="Human-decision digest (auto-maintained by /sweep — do not build)" \
    --label=sweep-digest --description="$(build_digest_body)" --silent)
  rtk bd update "$DIGEST_ID" --claim
  ```
- **`N == 1`** — steady state. `DIGEST_ID=$(echo "$DIGEST_ROWS" | jq -r '.[0].id')`; update in place
  (see step 6).
- **`N > 1`** — anomaly. Do **not** guess which is authoritative and do **not** write anything.
  Report the duplicate IDs plainly in the final report (below) and stop the write path for this
  pass; the human consolidates by hand (keep one, strip the label off the rest).

## 5. Compute the delta against the prior digest

Read the prior digest body and extract its `SWEEP-ITEM` lines (the digest format, below, is
designed to be trivially re-parseable):

```bash
LAST_BODY=$(rtk bd show "$DIGEST_ID" --json | jq -r '.[0].description')
LAST_IDS=$(printf '%s\n' "$LAST_BODY" | grep '^SWEEP-ITEM' | awk '{print $2}' | sort -u)
CURRENT_IDS=$(printf '%s\n' "$CURRENT" | awk -F'\t' '{print $1}' | sort -u)

NEW_IDS=$(comm -13 <(printf '%s\n' "$LAST_IDS") <(printf '%s\n' "$CURRENT_IDS"))
```

**Two separate triggers, not one** (a fix carried over from the design review — the original
algorithm only rewrote the digest `if new != empty`, which left resolved items showing as zombies
until the next unrelated add; acceptance requires a resolved item to drop out *promptly*):

- **Rewrite the digest** whenever `$CURRENT_IDS` differs from `$LAST_IDS` at all (an add **or** a
  remove) — this is what makes a resolved item actually disappear from the record.
- **Notify** (§7) only when `$NEW_IDS` is non-empty — a pure removal is quiet.

If `$CURRENT_IDS` equals `$LAST_IDS` exactly, nothing changed: skip the write entirely (no
`bd update`, no `bd dolt push` — a true no-op pass).

## 6. Rewrite the digest (only when the queue changed)

Digest body format — stable, line-oriented, designed to be re-parsed by the next pass and to read
cleanly for a human at `bd show <digest-id>`:

```
# Human-decision digest (auto-maintained by /sweep — do not edit by hand)

/sweep overwrites this description every pass the queue changes. It is /sweep's durable,
cross-machine dedup state — do not hand-edit it or delete the `sweep-digest` label.

Last swept: <ISO8601 UTC timestamp>

## Land/build escalations + human decisions (<N>)
SWEEP-ITEM <id> <kind> <title>
...
(none)

## Epics ready for a human close-decision (<M>)
SWEEP-ITEM <id> epic-ready-to-close <title>
...
(none)
```

Where each section lists its `CURRENT` rows for that kind (`land-escalated`/`human` in the first,
`epic-ready-to-close` in the second), or the literal `(none)` when a section is empty. Write it via
`--body-file` (multi-line, avoids shell-quoting the body inline):

```bash
rtk bd update "$DIGEST_ID" --body-file "$BODY_FILE"
```

## 7. Notify (only when `$NEW_IDS` is non-empty)

**No dedicated push-notification tool exists in this harness** (verified — nothing under that name
is available to a skill session). The channel every other loop leg already uses to reach a human is
its own **stop-and-report**: `/land` and `/epic-audit` both surface exclusively through their final
pass summary. I use the same channel, made impossible to miss: the report (§8) leads with a
loud, explicit **NEW HUMAN-DECISION ITEMS** block listing exactly the `$NEW_IDS` rows (id, kind,
title) whenever it's non-empty. This decision — and that it's revisit-if-a-real-push-channel-ever-
exists — is recorded once in `docs/decisions.md` (a repo doc, not per-pass state); if a genuine
push mechanism becomes available to a future session, wire it in there and update this section.

## 8. Publish and report

```bash
rtk bd dolt push   # only if step 6 wrote the digest — publish over refs/dolt/data, durable cross-machine
```

Report exactly one line plus, when non-empty, the loud new-items block:

```
sweep: queue depth <len $CURRENT_IDS>, <len $NEW_IDS> new, <count of epic-ready-to-close rows> closable
```

If §4 found `N > 1` duplicate digests, or any sub-step in §1/§2 failed, say so plainly in the same
report (see below) — the pass still ends cleanly either way.

## Failure handling — a sub-step fails, the loop survives

Each of §1 (two independent `bd` queries), §2 (per-epic `bd show`), §4, and §6 is treated as
**independent**: if one `bd`/`jq` call errors, note the failure in the final report, treat that
source as empty/unknown for this pass, and continue with whatever succeeded. I never let a single
failed sub-step abort the whole pass or leave the digest half-written — either the rewrite in §6
completes cleanly or it is skipped entirely (no partial `--body-file` write). A failed pass still
ends with a report and exit 0, so the next `/loop` tick gets a clean shot.

## What I never do

- **Write `trunk`, merge, close a ticket, or touch a producer's worktree.** Not my layer.
- **Dispatch `/code`, `/land`, `code-reviewer`, `land-review`, or `/epic-audit`.** I only read `bd`
  state and write my own digest issue.
- **Resolve a `land-escalated` or `human` item myself, or guess at a duplicate digest.** Surface,
  never decide.
- **Let the digest issue enter `bd ready`.** I claim it immediately on creation for exactly that
  reason.
- **Commit or `bd import` `.beads/issues.jsonl`**, or record a design decision in a bd note instead
  of `docs/`.

## Stop and report

When the pass ends I report: the one-line summary (§8), the full **NEW HUMAN-DECISION ITEMS** block
when `$NEW_IDS` is non-empty, any duplicate-digest anomaly, and any sub-step that failed. A clean,
unchanged queue is a valid, common outcome — I say so plainly and stop.
