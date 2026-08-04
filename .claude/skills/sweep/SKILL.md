---
name: sweep
description: The third `/loop` leg — a SURFACE-ONLY human-decision surfacer. Scans bd for work that has stopped waiting on a human and nothing else consumes (`land-escalated` branches, `human`-labeled decision tickets, epics ready for a human close-decision), dedups against a durable cross-machine digest issue, and surfaces new items; also lists every `deferred`-status ticket in its report each pass (read-only, no dedup, never in the digest) so parked work stays visible. Writes no `trunk`, makes no decisions, dispatches no builders/landers/auditors. Run self-paced as `/loop 30m /sweep`. Examples — "/sweep", "/loop 30m /sweep", "what needs a human decision right now?", "sweep the human-decision queue".
---

# sweep

I am lode's **human-decision surfacer** — the third `/loop` leg, alongside `/code` and `/land`
(and `/epic-audit`). `/code` sweeps `needs-rebase` back to a producer; `/land` bounces re-enter
`bd ready`; `/epic-audit` runs itself as `/loop /epic-audit`. The only pipeline outputs with **no
downstream consumer** are the ones parked for a human: a `land-escalated` branch, a `human`-labeled
decision ticket, and an epic that's `epic-audited` + open + every child closed. Nothing pings a
human when work parks on one of these — you only find it by manually running `bd`. I turn that
silence into an active surface.

I also list every `deferred`-status ticket in my report each pass (§2a) — parked work that
`bd ready` hides by design and no other loop leg surfaces. Report-only: no dedup state, no digest
rewrite, no notification.

I am the **lowest-privilege** loop leg, deliberately: I write **one** self-owned bookkeeping issue
(a running digest) and nothing else. The full design record — why this exists, what was challenged,
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
- **Never claims work off `bd ready`** and needs no worktree — every step here is `bd` plumbing run
  from wherever I'm invoked; I touch no `git` and write no repo files at all. (Scratch files under
  `${TMPDIR:-/tmp}` that carry this pass's own intermediate state between fenced blocks — see §1 —
  are neither: no git command, no path inside this repo's working tree.)
- **Never promotes a ticket to a human-decision item *because* it is `deferred`.** §2a is
  visibility only: nothing it reads enters `$CURRENT`/`$NEW_IDS`, touches the digest, or fires a
  `PushNotification`. The converse — a ticket that *independently* carries `land-escalated` and is
  *also* `deferred` — is a **decided**, deliberate exception, not a gap (lode-o7ai,
  `docs/decisions.md`): §1 passes no `--status` filter, so such a ticket still reaches `$CURRENT`
  and the digest through §1 regardless of its `deferred` status. Dropping it would delete a real,
  unresolved escalation from the durable record — and `bd defer` is not one of `land-escalated`'s
  three resolution exits, so deferring never actually resolves it. §7 suppresses only the
  `PushNotification` for such a row (never the report), and it is deliberately listed twice in the
  report — once in §7/§8's `NEW HUMAN-DECISION ITEMS` block (annotated `(deferred)`), once in §2a
  (unannotated) — information about a parked escalation, not redundancy to tidy away.

## 0. Setup — Dolt-authoritative, fresh scratch state

Same sync discipline as every other bd-writing loop leg: pull fresh state before reading, push
after writing. I also run each fenced `bash` block below as its own, separate Bash tool invocation
— nothing carries over between them (variables, arrays, functions — same governing rule
`.claude/skills/land/SKILL.md` states and `docs/agents-workflow.md` records repo-wide, lode-sfnb /
lode-x495). §1-§3's queue-building steps persist their results to files under a **fixed** scratch
directory (`${TMPDIR:-/tmp}`, never this repo's `.git/` or working tree — see the "no repo files"
non-goal above) so later blocks can read them back instead of relying on shell state that does not
survive. Fresh per pass, so no stale queue data from an earlier tick can leak into this one:

```bash
rtk bd dolt pull
SWEEP_TMP="${TMPDIR:-/tmp}/lode-sweep-state"
rm -rf "$SWEEP_TMP" && mkdir -p "$SWEEP_TMP"
```

## 1. Collect the human-decision queue

Two sources, per the epic's decided scope. I defensively exclude my own digest issue from the
`land-escalated` query (it should never carry that label, but cheap insurance costs nothing):

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/lode-sweep-state"   # re-derive -- fresh Bash invocation, see §0

ESCALATED=$(rtk bd list --label land-escalated --exclude-label sweep-digest --limit 0 --json \
  | jq -r '(. // []) | .[] | "\(.id)\tland-escalated\t\(.title)\t\(.status)"')
printf '%s' "$ESCALATED" > "$SWEEP_TMP/escalated"

HUMAN=$(rtk bd human list --status open --json \
  | jq -r '(. // []) | .[] | "\(.id)\thuman\t\(.title)"')
printf '%s' "$HUMAN" > "$SWEEP_TMP/human"
```

**`--limit 0` on every `bd list` in this skill — the canonical reason, referenced from §2/§2a/§4.**
`bd list --help` documents `--limit` with a default of 50 ("use 0 for unlimited"), and bd emits **no**
truncation signal — it neither errors nor marks a short result. But measured on bd 1.1.0, the cap is
applied only when `--limit` is passed *explicitly*; an omitted flag returns the full set (a bare
`bd list --status closed --json` returns hundreds of rows, well past 50). So these queries are **not**
truncating today — `--limit 0` pins the documented "unlimited" semantics so they stay correct if a
later bd starts enforcing its own documented default. It matters most here: §6 rewrites the digest
**wholesale** from `$CURRENT`, so a capped source query would drop items 51+ from the durable record,
read as resolved, and re-notify as "new" on a later pass — §5's hard precondition in reverse.
(`bd human list` exposes no `--limit` flag at all, so `$HUMAN` cannot be pinned the same way.)

**Why `(. // [])` and not a bare `.[]`:** an *empty* `bd` result serializes as literal `null`, not
`[]` (seen most often on `bd human list` when no `human` ticket is open). A bare `jq '.[]'` on that
`null` aborts with `Cannot iterate over null`, which the pipeline below would misread as a *failed
query* — and a failed query suppresses the rewrite (§5's hard precondition), so a `human` item that
was just resolved would **zombie in the digest** instead of dropping out promptly. `(. // [])`
normalizes `null` → `[]` so an empty queue reads as empty. It does **not** mask the usual failure
signature: a `bd` error prints a diagnostic (malformed JSON), on which `jq` still aborts, so that
failure surfaces. (The one case neither the guard nor `jq` distinguishes — a `bd` failure that exits
non-zero but writes *zero bytes* — was already indistinguishable from an empty queue before this
guard; it is unchanged, not introduced, here.)

If either `bd` call errors, note the failure and **skip the digest rewrite for this pass** rather
than aborting — a failed query is not an empty queue (but a `null`-serialized *empty* result is not
a failure — see above). See
[Failure handling](#failure-handling--a-sub-step-fails-the-loop-survives).

**Why `$ESCALATED` carries a 4th tab field (`.status`) and `$HUMAN` doesn't (decided, lode-o7ai —
see [docs/decisions.md](../../../docs/decisions.md)):** the `land-escalated` query above passes no
`--status` filter — deliberately, unchanged from before this decision — so it can return a ticket
that is *also* `deferred`. That per-row status is already present on every row this query returns
(no second call needed, the same derivable-state principle §2's `title` comment below relies on),
and §7 needs it to decide two things: whether to include the row in the `PushNotification`, and
whether to annotate it `(deferred)` in the report. `$HUMAN` never needs this field — its own query
already filters to `--status open`, so a `human`-labeled row can never be `deferred` in the first
place.

## 2. Collect epics ready for a human close-decision

An epic qualifies when it is `epic-audited` + still `open` + **every** `parent-child` child is
`closed` — `/epic-audit` deliberately never closes an epic itself, so nothing else flags these.
Same shared child-completion check `/epic-audit` and `/land` use, `scripts/epic-children-closed.sh`
(NOT `bd show`'s `.dependents` array — see the script's own header; that array is populated only
with the opt-in `--include-dependents` flag, and its absence made this exact check dead code in all
three skills until lode-v4rk):

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/lode-sweep-state"   # re-derive -- fresh Bash invocation, see §0

CLOSABLE=""
# Pull id AND title in the ONE list read -- `bd list --json` rows already carry
# `title`, so re-fetching it per epic with a second `bd show` would be a wasted
# round-trip against derivable state.
while IFS=$'\t' read -r e TITLE; do
  [ "$(rtk scripts/epic-children-closed.sh "$e")" = "true" ] || continue
  ROW=$(printf '%s\tepic-ready-to-close\t%s' "$e" "$TITLE")
  # The newline MUST sit outside the command substitution above: `$(...)` strips
  # trailing newlines, so building the row as `printf '...\n'` would silently drop
  # the separator and jam every epic onto ONE line (only visible with >=2 closable
  # epics, which is why it reads fine in a one-epic spot check).
  CLOSABLE="${CLOSABLE}${ROW}
"
done < <(rtk bd list --type=epic --label epic-audited --status open --limit 0 --json \
  | jq -r '(. // []) | .[] | [.id, .title] | @tsv')
printf '%s' "$CLOSABLE" > "$SWEEP_TMP/closable"
```

`--limit 0` for the same reason as §1 — same `$CURRENT`, same wholesale §6 rewrite.

## 2a. Collect deferred tickets (report-only — never touches the digest or notify path)

A third, independent read, on its own track. `deferred`-status tickets are explicitly parked "deal
with later" by a human — the opposite of a fresh human-decision item — but `bd ready` hides them by
design and no other loop leg lists them, so once parked they otherwise vanish from every workflow
surface. I list them for visibility only:

```bash
DEFERRED=$(rtk bd list --status deferred --limit 0 --json \
  | jq -r '(. // []) | .[] | [.id, .title] | @tsv')
```

Same `(. // [])` null-empty guard as §1/§2 — and the same `@tsv` as §2, which escapes a tab or
newline embedded in a title instead of letting it break the row.

**`--limit 0` — same reason as §1.** The stake specific to this section: it promises the deferred list
"in full, with no dedup" every pass, so a capped query would under-report past 50 while the §8 count
still read as the true total. (`lode-2gun` extended the same pin to `/land`, `/code`, `/epic-audit`,
`/release` and to `scripts/epic-children-closed.sh`, called from §2 above; `lode-9bbq` added
`.claude/statusline.sh`. A grep is not a substitute for that roster: `statusline.sh` calls
`bd -C "$cwd" list`, and the `-C` between `bd` and `list` is exactly what hid it from `lode-2gun`'s
literal `bd list` search. **But the roster is no longer what enforces this** — `lode-200t` added
`tests/test_bd_list_limit_gate.py`, which fails on a *new* unguarded call site rather than trusting
this list to stay current by itself. That test owns the scan surface and the exclusions; this
paragraph is documentation for a human reader, and deliberately does not restate them.)

**Deliberately excluded from everything else in this skill:**

- `$DEFERRED` never feeds `$CURRENT` (§3) — it must never enter `$CURRENT_IDS`/`$NEW_IDS` (§5),
  never drive the digest rewrite/no-op decision, and never trigger the §7 `PushNotification`. A
  ticket moving into (or out of) `deferred` is not a new human-decision item.
- `$DEFERRED` is never written into the digest body (§6) and carries **no dedup state** of its
  own — it is recomputed fresh, in full, every pass, straight into the §8 report.

**The one deliberate overlap:** a ticket that is simultaneously `land-escalated` (§1) and `deferred`
is listed here (unconditionally, unannotated) *and*, on the pass it first appears, also in §7/§8's
`NEW HUMAN-DECISION ITEMS` block (there, annotated `(deferred)`) — decided, not a gap (lode-o7ai,
[docs/decisions.md](../../../docs/decisions.md)). §1 passes no `--status` filter on purpose: `bd
defer` is not one of `land-escalated`'s three resolution exits, so dropping the row out of `$CURRENT`
would delete a real, unresolved escalation from the digest. This section's own listing is unaffected
either way — it stays exactly what it always was, every current `deferred` ticket, in full.

If this query itself errors, the failure is isolated to this step alone — note it in the §8 report
and continue. See [Failure handling](#failure-handling--a-sub-step-fails-the-loop-survives).

## 3. Build the current queue (dedup on stable IDs)

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/lode-sweep-state"   # re-derive -- fresh Bash invocation, see §0

# Load §1/§2's results back from disk and assert each one loaded -- a missing file means that
# step never ran this pass, and continuing on a phantom-empty queue would risk deleting real
# escalations from the digest (§5's hard precondition, in reverse).
ESCALATED="$(cat "$SWEEP_TMP/escalated")" || {
  echo "GATE COULD NOT RUN: $SWEEP_TMP/escalated missing -- §1 did not run this pass" >&2
  exit 1
}
HUMAN="$(cat "$SWEEP_TMP/human")" || {
  echo "GATE COULD NOT RUN: $SWEEP_TMP/human missing -- §1 did not run this pass" >&2
  exit 1
}
CLOSABLE="$(cat "$SWEEP_TMP/closable")" || {
  echo "GATE COULD NOT RUN: $SWEEP_TMP/closable missing -- §2 did not run this pass" >&2
  exit 1
}

CURRENT=$(printf '%s\n%s\n%s\n' "$ESCALATED" "$HUMAN" "$CLOSABLE" | sed '/^$/d' | sort -u -t$'\t' -k1,1)
printf '%s' "$CURRENT" > "$SWEEP_TMP/current"
```

Each line is `<id>\t<kind>\t<title>` — `<id>` is the dedup key throughout. A row sourced from
`$ESCALATED` carries a 4th field (`.status`, see §1); `$HUMAN`/`$CLOSABLE` rows never do, since both
are already filtered to `--status open`. Every downstream reader here (`awk -F'\t' '{print $1}'`,
`sort -t$'\t' -k1,1`) only ever looks at field 1, so the extra field is inert until §7 reads it.

## 4. Find-or-create the digest issue (locator = reserved label `sweep-digest`)

The dedup state lives **in the digest issue itself** (Dolt-durable, travels cross-machine) — a
scratchpad state file was explicitly rejected during design because it re-notifies the whole queue
from a second machine. The digest issue is found by a **reserved label**, not a remembered ID:

```bash
DIGEST_ROWS=$(rtk bd list --label sweep-digest --all --limit 0 --json)
N=$(echo "$DIGEST_ROWS" | jq '(. // []) | length')   # `(. // [])` for the same null-serializes-empty reason as §1
```

**`--limit 0` here is uniformity, not a guard against a reachable failure.** A cap could not hide a
duplicate digest even if it applied: truncation yields `N = min(actual, 50)`, so `N == 1` implies
`actual == 1`, and ≥51 digest rows would read as `N == 50` and still trip the `N > 1` path below. The
flag is passed anyway so every `bd list` in this skill reads the same way and none looks like an
oversight a later edit should "tidy" away.

- **`N == 0`** — bootstrap. Only create it if `$CURRENT` is non-empty (an empty queue with no prior
  digest is a clean no-op — nothing to bootstrap, nothing to write). Create it with a **placeholder
  body carrying no `SWEEP-ITEM` lines**, so §5 reads `LAST_IDS` as empty and every item in the
  current queue counts as new: the first pass must *notify* the full standing queue, not silently
  swallow it. §6 then writes the real body on that same pass. Immediately claim it so it never
  appears in `bd ready` (it is bookkeeping, not buildable work — the same "claimed tickets are out
  of `bd ready`" convention the coding loop relies on for `ready-for-code-review`/`ready-for-land`):

  ```bash
  DIGEST_ID=$(rtk bd create --type=chore --title="Human-decision digest (auto-maintained by /sweep — do not build)" \
    --label=sweep-digest --description="(bootstrapping — /sweep fills this in on this same pass)" --silent)
  rtk bd update "$DIGEST_ID" --claim
  ```
- **`N == 1`** — steady state. Nothing to do here — §5 and §6 each re-derive `$DIGEST_ID` themselves
  via `scripts/sweep-digest-id.sh`, rather than reusing this block's own `$DIGEST_ROWS`/`$N`, since
  neither survives into a later block (fresh Bash invocation each time). **That script re-asserts
  `N == 1` itself**: this branch of this block is not a guard those blocks inherit, so it is
  re-checked there rather than trusted, immediately before the read (§5) and the write (§6).
- **`N > 1`** — anomaly. Do **not** guess which is authoritative and do **not** write anything.
  Report the duplicate IDs plainly in the final report (below) and stop the write path for this
  pass; the human consolidates by hand (keep one, strip the label off the rest).

## 5. Compute the delta against the prior digest

Read the prior digest body and extract its `SWEEP-ITEM` lines (the digest format, below, is
designed to be trivially re-parseable):

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/lode-sweep-state"   # re-derive -- fresh Bash invocation, see §0
# Re-derive DIGEST_ID -- cheap and deterministic, so re-running the query beats persisting an id.
# The script refuses unless exactly one digest exists; a bare `.[0].id` would silently pick the
# first of several duplicates (§4's `N > 1` anomaly) or yield "null" when none exists (§4's
# `N == 0`). Quote its stderr rather than re-deriving a cause of my own.
DIGEST_ID="$(rtk scripts/sweep-digest-id.sh)" || exit 1
CURRENT="$(cat "$SWEEP_TMP/current")" || {
  echo "GATE COULD NOT RUN: $SWEEP_TMP/current missing -- §3 did not run this pass" >&2
  exit 1
}

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

**Hard precondition — a failed source query suppresses the rewrite.** §6 rebuilds the digest
wholesale from `$CURRENT`, so if any §1/§2 query errored, `$CURRENT` is not the true queue: a query
that fails is indistinguishable from a queue that is empty, and rewriting on it would **delete real
escalations from the durable record** and then re-notify them as "new" on the next pass. If any
source failed, skip §6 and §7 and leave the prior digest untouched. Stale is recoverable;
silently truncated is not.

## 6. Rewrite the digest (only when the queue changed, and every source query succeeded)

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
`epic-ready-to-close` in the second), or the literal `(none)` when a section is empty. A
`SWEEP-ITEM` line is always exactly `<id> <kind> <title>` (fields 1-3 of the `$CURRENT` row) —
**never** the optional 4th `.status` field an `$ESCALATED` row may carry (§1). The persisted digest
is deliberately left unannotated: annotating it would go stale the moment a ticket's `deferred`
status flips without its id entering or leaving `$CURRENT_IDS`, since a rewrite here only fires on
an id-set change (§5), not a content change to an unchanged id. The `(deferred)` annotation lives
only in the freshly-recomputed, per-pass report (§7/§8) — see the decision in
[docs/decisions.md](../../../docs/decisions.md) (lode-o7ai). Write it via
`--body-file` (multi-line, avoids shell-quoting the body inline) — `BODY_FILE` and the re-derived
`DIGEST_ID` are both real values only within THIS block (a fresh Bash invocation; nothing from §4/§5
survives), so both are established here, not reused from an earlier one:

```bash
# Same refusal as §5, and load-bearing for a stronger reason: this block WRITES. Under §4's
# `N > 1` anomaly a bare `.[0].id` would overwrite whichever duplicate sorted first.
DIGEST_ID="$(rtk scripts/sweep-digest-id.sh)" || exit 1
BODY_FILE="$(mktemp)"
# …write the digest body (format above) into "$BODY_FILE"…
rtk bd update "$DIGEST_ID" --body-file "$BODY_FILE"
```

## 7. Notify (only when there is a new item to push)

I run as a **skill in the main conversation**, so I have the main session's tools — including
`PushNotification`, which reaches a human who is away from the terminal. That is the entire point of
this leg: the `land-escalated` and `human` labels already sat in `bd`, where nobody was looking.

**First, re-derive `$NEW_IDS` and split it on deferred status.** This is its own, separate Bash tool
invocation — nothing from §5 survives into it (lode-sfnb; §0's governing rule) — so everything it
needs is re-derived from the scratch files §1/§3 already wrote, the sanctioned remedy for
cross-block state (re-deriving is cheap and deterministic; see this skill's own §0 and
`docs/agents-workflow.md`'s cross-block-shell-state section):

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/lode-sweep-state"   # re-derive -- fresh Bash invocation, see §0

DIGEST_ID="$(rtk scripts/sweep-digest-id.sh)" || exit 1
CURRENT="$(cat "$SWEEP_TMP/current")" || {
  echo "GATE COULD NOT RUN: $SWEEP_TMP/current missing -- §3 did not run this pass" >&2
  exit 1
}
ESCALATED="$(cat "$SWEEP_TMP/escalated")" || {
  echo "GATE COULD NOT RUN: $SWEEP_TMP/escalated missing -- §1 did not run this pass" >&2
  exit 1
}

LAST_BODY=$(rtk bd show "$DIGEST_ID" --json | jq -r '.[0].description')
LAST_IDS=$(printf '%s\n' "$LAST_BODY" | grep '^SWEEP-ITEM' | awk '{print $2}' | sort -u)
CURRENT_IDS=$(printf '%s\n' "$CURRENT" | awk -F'\t' '{print $1}' | sort -u)
NEW_IDS=$(comm -13 <(printf '%s\n' "$LAST_IDS") <(printf '%s\n' "$CURRENT_IDS"))

# A NEW land-escalated row whose §1 status is "deferred" (decided, lode-o7ai -- see
# docs/decisions.md): keep it in the report, annotated -- but exclude it from what
# gets pushed. `deferred` means a human already saw this and parked it; re-pushing
# is noise. Only $ESCALATED rows can carry a 4th field (§1) -- a $NEW_IDS id absent
# from $ESCALATED, or present with an empty 4th field, is correctly never treated as
# deferred.
NEW_ANNOTATED=""
PUSH_IDS=""
while IFS= read -r nid; do
  [ -n "$nid" ] || continue
  ROW=$(printf '%s\n' "$CURRENT" | awk -F'\t' -v id="$nid" '$1 == id { print; exit }')
  STATUS=$(printf '%s\n' "$ESCALATED" | awk -F'\t' -v id="$nid" '$1 == id { print $4; exit }')
  if [ "$STATUS" = "deferred" ]; then
    NEW_ANNOTATED="${NEW_ANNOTATED}${ROW} (deferred)
"
  else
    NEW_ANNOTATED="${NEW_ANNOTATED}${ROW}
"
    PUSH_IDS="${PUSH_IDS}${nid}
"
  fi
done <<< "$NEW_IDS"
printf '%s' "$NEW_ANNOTATED" > "$SWEEP_TMP/new_annotated"
printf '%s' "$PUSH_IDS" > "$SWEEP_TMP/push_ids"
```

`$SWEEP_TMP/new_annotated` is the full **NEW HUMAN-DECISION ITEMS** report content (§8) — every row
newly in `$CURRENT_IDS` this pass, `<id> <kind> <title>`, with a trailing `(deferred)` marker on any
row whose status is `deferred`. `$SWEEP_TMP/push_ids` is the (possibly smaller, possibly empty)
subset actually eligible for a push — a `deferred` row is dropped from it, never from
`$SWEEP_TMP/new_annotated`.

`PushNotification` is a **deferred** tool — its schema is not loaded up front, so load it before the
first call, then send **one** notification per pass (not one per item), and **only if
`$SWEEP_TMP/push_ids` is non-empty**:

- `ToolSearch` with query `select:PushNotification` — this returns its schema and makes it callable.
- Call it once with a short summary covering only the ids in `$SWEEP_TMP/push_ids`: how many, and
  their ids/kinds — e.g. `2 new human-decision items: lode-abc (land-escalated), lode-xyz (human)`.
  A row excluded from `push_ids` for being `deferred` is never named in the push — it is still
  reported (below), just not pushed.

Then **also** include a loud, explicit **NEW HUMAN-DECISION ITEMS** block in the §8 report: every row
from `$SWEEP_TMP/new_annotated` (id, kind, title, `(deferred)` when applicable) — the full set,
deferred rows included, not just what got pushed. The two are complementary, not alternatives: the
push reaches an away human; the report block is what they (and anyone reading a deferred row) read
when they return to the `/loop` transcript. If `ToolSearch` cannot resolve `PushNotification` in the
session I am actually running in, fall back to the report block alone and say so plainly in the
report — never fail a pass over the notify channel.

## 8. Publish and report

```bash
rtk scripts/bd-dolt-push.sh   # only if step 6 wrote the digest — publish over refs/dolt/data, durable cross-machine
```

Report exactly one line, then the deferred section (§2a, always present), plus, when non-empty, the
loud new-items block:

```
sweep: queue depth <len $CURRENT_IDS>, <len $NEW_IDS> new, <count of epic-ready-to-close rows> closable, <len $DEFERRED> deferred

## Deferred (surfaced, not reviewed) (<len $DEFERRED>)
<id> <title>
...
(none)
```

The deferred section lists every current `$DEFERRED` row (id + title) each pass, in full, with no
dedup — or the literal `(none)` when `$DEFERRED` is empty.

When `$SWEEP_TMP/new_annotated` (§7) is non-empty, follow the deferred section above with:

```
## NEW HUMAN-DECISION ITEMS (<count of rows in new_annotated>)
<id> <kind> <title>
<id> <kind> <title> (deferred)
...
```

Every row from `$SWEEP_TMP/new_annotated`, verbatim — a trailing `(deferred)` on a row means it is
new to `$CURRENT_IDS` this pass but its status is `deferred`: per the decided behavior (lode-o7ai,
[docs/decisions.md](../../../docs/decisions.md)), it was **not** included in the `PushNotification`
(a human already saw and parked it), but it is not dropped from the report either — and it may *also*
appear in the `## Deferred (surfaced, not reviewed)` section above. That double appearance is
deliberate (the two sections answer different questions — "what's new" vs. "what's parked" — and a
row can honestly be both), not a bug for a later edit to "fix" by suppressing either listing.

If §4 found `N > 1` duplicate digests, any sub-step in §1/§2 failed, or the §2a deferred query
failed, say so plainly in the same report (see below) — the pass still ends cleanly either way.

## Failure handling — a sub-step fails, the loop survives

A failed sub-step must never abort the pass — and must never corrupt the digest either. Those pull
in opposite directions, and the digest wins: it is rebuilt wholesale from `$CURRENT`, so a source
query that errors is indistinguishable from "that queue is empty", and rewriting on it would delete
real items from the durable record a human relies on.

- If any §1/§2 query errors (`bd` or `jq`), note the failure in the report, **skip the §6 rewrite and
  the §7 notification entirely**, and leave the prior digest exactly as it was. Stale, not truncated.
  An *empty* result that serializes as literal `null` is **not** a failure — the `(. // [])` guard in
  §1/§2 normalizes it to an empty list, so a queue that legitimately emptied still rewrites the digest
  and drops the resolved item promptly (without it, the `jq` abort would look like a failed query and
  wrongly suppress the rewrite).
- The §6 rewrite is all-or-nothing: it either completes cleanly or is skipped (no partial
  `--body-file` write).
- If §4 finds `N > 1` digests, the write path stops for the pass (that anomaly is reported, never
  guessed at).
- If the §2a deferred query errors, that failure is isolated to the deferred section alone: note
  "deferred list unavailable this pass" in the report and continue — it must **not** suppress the
  §6 rewrite or §7 notification for the (unrelated) escalation/human/epic queue, and vice versa: a
  §1/§2 failure never suppresses the §2a deferred section, which has no rewrite to protect.
- A failed pass still ends with a report and exit 0, so the next `/loop` tick gets a clean shot.

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

When the pass ends I report: the one-line summary (§8), the deferred section (§2a, always present),
the full **NEW HUMAN-DECISION ITEMS** block when `$NEW_IDS` is non-empty (annotated `(deferred)`
per-row where applicable, per §7 — lode-o7ai), any duplicate-digest anomaly, and any sub-step that
failed. A clean, unchanged queue is a valid, common outcome — I say so plainly and stop.
