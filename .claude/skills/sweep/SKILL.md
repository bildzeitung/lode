---
name: sweep
description: The third `/loop` leg — a SURFACE-ONLY human-decision surfacer. Scans bd for work that has stopped waiting on a human and nothing else consumes (`land-escalated` branches, `human`-labeled decision tickets that are not dependency-blocked, epics ready for a human close-decision), dedups against a durable cross-machine digest issue, and surfaces new items; every pass's report ends with the full "Actionable now" list of what's decidable right now (every current, non-deferred row, in full — not just the delta), and also lists every `deferred`-status ticket (§2a), every `in_progress` ticket claimed more than 24h ago that carries no pipeline label (§2b), and every dependency-blocked `human`-labeled ticket (§2c) in its report each pass (read-only, no dedup, never in the digest) so parked, stranded, and not-yet-decidable work stays visible. Writes no `trunk`, makes no decisions, dispatches no builders/landers/auditors. Run self-paced as `/loop 30m /sweep`. Examples — "/sweep", "/loop 30m /sweep", "what needs a human decision right now?", "sweep the human-decision queue".
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
`bd ready` hides by design and no other loop leg surfaces — every `in_progress` ticket claimed
more than 24h ago (§2b's age discriminator, below) that carries none of the pipeline labels — claimed
work that fell out of every consumer's sight (`bd ready` excludes it because it's `in_progress`;
every pipeline leg keys on a label it doesn't have) — and every open `human`-labeled ticket that is
currently dependency-blocked (§2c) — a sign-off placeholder whose artifact does not exist yet, so it
is not decidable and is subtracted from §1's `$HUMAN` source before it can reach `$CURRENT`/the
digest/the push (lode-csxh). All three are report-only: no dedup state, no digest rewrite, no
notification.

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
  *also* `deferred` — is a **decided**, deliberate exception, not a gap: it still reaches `$CURRENT`
  and the digest through §1 (which passes no `--status` filter), §7 suppresses only its
  `PushNotification`, and it is listed twice in the report — §7/§8's `NEW HUMAN-DECISION ITEMS`
  block (annotated `(deferred)`) and §2a (unannotated). That is information about a parked
  escalation, not redundancy to tidy away. Full rationale: lode-o7ai in
  [docs/decisions.md](../../../docs/decisions.md).
- **Never promotes a ticket to a human-decision item *because* it is `in_progress` with no pipeline
  label.** §2b is visibility only, on the same terms as §2a above. Unlike §2a, §2b's query is
  *non-overlapping* with §1 by construction — the opposite of lode-o7ai's decided §1 x §2a overlap,
  and deliberately so. The mechanism is spelled out once, in §2b's exclude-label prose; the decision
  is recorded alongside lode-o7ai in [docs/decisions.md](../../../docs/decisions.md) (lode-ppki).
- **Never auto-remediates a stranded ticket.** §2b does not unclaim, reassign, or reopen anything —
  surface only. A human decides whether a stranded ticket is abandoned or deliberately held.
- **Never lets a dependency-blocked `human` ticket sit in `$CURRENT`/the digest/the push, and never
  drops it from view either.** §1 subtracts `bd blocked`'s id set from `$HUMAN` — a sign-off
  placeholder for an artifact that doesn't exist yet is not decidable — but the subtracted rows are
  still listed, unconditionally, every pass, in §2c's report-only "Blocked human tickets" section.
  When the blocking dependency closes, the ticket enters `$CURRENT` for the first time and notifies
  as NEW — deliberate, not a side effect. Full rationale: lode-csxh in
  [docs/decisions.md](../../../docs/decisions.md).

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
bd dolt pull
SWEEP_TMP="${TMPDIR:-/tmp}/lode-sweep-state"
rm -rf "$SWEEP_TMP" && mkdir -p "$SWEEP_TMP"
```

## 1. Collect the human-decision queue

Two sources, per the epic's decided scope, plus a `bd blocked` subtraction on the `human` source
(lode-csxh) — see the note below the block. I defensively exclude my own digest issue from the
`land-escalated` query (it should never carry that label, but cheap insurance costs nothing):

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/lode-sweep-state"   # re-derive -- fresh Bash invocation, see §0

set -o pipefail   # REQUIRED -- makes a failed bd query detectable; see the note below.

# The marker is written INSIDE each failure branch, never as a trailing `[ $FAILED = 1 ] &&
# touch ...`: a conditional at the end of a block becomes the block's own exit status, so the
# healthy path would short-circuit to 1 and report a failure on every good pass. Existence is the
# whole signal -- content is irrelevant. Deliberately NOT §2a/§2b's SWEEP-QUERY-ERROR sentinel:
# that sentinel lives inside a report-only list; this marker's consumer is §5, gating the §6
# digest rewrite, and its meaning is "skip the rewrite this pass", not "render as errored".
if ! ESCALATED=$(bd list --label land-escalated --exclude-label sweep-digest --limit 0 --json \
  | jq -r '(. // []) | .[] | "\(.id)\tland-escalated\t\(.title)\t\(.status)"'); then
  touch "$SWEEP_TMP/source_query_failed"
  ESCALATED=""   # the capture may be partial/garbled on failure -- never persist it as data
fi
printf '%s' "$ESCALATED" > "$SWEEP_TMP/escalated"

if ! HUMAN_RAW=$(bd human list --status open --json \
  | jq -r '(. // []) | .[] | "\(.id)\thuman\t\(.title)"'); then
  touch "$SWEEP_TMP/source_query_failed"
  HUMAN_RAW=""
fi

# lode-csxh: a human-labeled ticket that is dependency-blocked is not decidable -- the
# artifact it signs off on does not exist yet -- so subtract `bd blocked`'s id set from
# $HUMAN before it can reach $CURRENT/the digest/PushNotification. $ESCALATED/$CLOSABLE are
# never filtered this way -- this is a $HUMAN-only subtraction. `bd blocked` has no --limit
# flag to pin (checked -- it is a distinct subcommand from `bd list`, outside
# tests/test_bd_list_limit_gate.py's `bd list`-only scan surface).
# Pre-truncate BOTH outputs, unconditionally: awk never opens an output file it writes zero
# rows to, so without these an empty list would leave NO file and §8 would read `missing`
# ("§1 never ran") instead of `ok`/`(none)` ("§1 ran, nothing to report"). These lines are
# load-bearing for that three-state distinction -- do not drop them in favour of awk's own
# redirection.
: > "$SWEEP_TMP/human"
: > "$SWEEP_TMP/blocked_human"

# The query and the partition it feeds are ONE branch deliberately: the failure path has to
# write both files itself, so splitting them would mean re-testing a flag 20 lines below the
# branch that set it.
#
# Partition $HUMAN_RAW on membership in $BLOCKED_IDS: the non-blocked rows become the real
# $HUMAN source (unchanged shape, `<id>\thuman\t<title>`); the blocked-out rows are persisted
# separately for §2c's report-only "Blocked human tickets" section, which shares the §2a/§2b
# contract (own scratch file, own sentinel -- see that section below).
if ! BLOCKED_IDS=$(bd blocked --json | jq -r '(. // []) | .[] | .id'); then
  # Same marker as the two queries above: a failed `bd blocked` must NOT be read as "nothing
  # is blocked" -- that would let the whole blocked set flood $CURRENT and false-notify it as
  # new. It suppresses the §6 rewrite exactly like an $ESCALATED/$HUMAN failure.
  touch "$SWEEP_TMP/source_query_failed"
  # The partition itself is meaningless now (we don't know the true blocked set), so $HUMAN is
  # left unfiltered -- harmless, since the marker above already suppresses §6/§7 for this whole
  # pass -- and §2c's own copy gets the SWEEP-QUERY-ERROR sentinel per its contract.
  printf '%s' "SWEEP-QUERY-ERROR" > "$SWEEP_TMP/blocked_human"
  printf '%s' "$HUMAN_RAW" > "$SWEEP_TMP/human"
else
  awk -F'\t' -v human_out="$SWEEP_TMP/human" -v blocked_out="$SWEEP_TMP/blocked_human" '
    NR == FNR { if ($1 != "") blocked[$1] = 1; next }
    $1 == "" { next }
    ($1 in blocked) { print $1 "\t" $3 >> blocked_out; next }
    { print $1 "\t" $2 "\t" $3 >> human_out }
  ' <(printf '%s\n' "$BLOCKED_IDS") <(printf '%s\n' "$HUMAN_RAW")
fi
```

**`set -o pipefail` is what makes those `if !` guards mean anything** — without it a failing `bd`
never reaches the assignment's exit status at all. The mechanism, and the measured bd 1.1.0
behaviour behind it, is stated once in §2a below; it applies identically here, at the
higher-consequence site: `$ESCALATED`/`$HUMAN` would come out empty on a failed query,
indistinguishable from a legitimately empty queue, at the exact site that gates §6's wholesale
digest rewrite. Set inside this block, so it is scoped to this Bash invocation (§0).

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
signature: measured on bd 1.1.0, a failing `bd list`/`bd human list` writes its diagnostic to
**stderr** and **zero bytes to stdout** — `jq` then reads no input, emits nothing, and exits `0`,
so without `set -o pipefail` (above) the assignment would report success on a failed query. With
`pipefail` set, the pipeline's exit status is `bd`'s non-zero status, which the `if !
VAR=$(...)` guard above catches directly — no malformed-JSON path is involved; `jq` never sees
anything to abort on, because it never receives input in this case.

If either `bd` call errors, the `$SWEEP_TMP/source_query_failed` marker above is written and **the digest rewrite is
skipped for this pass** (§5 checks for `$SWEEP_TMP/source_query_failed`) rather than the pass
aborting — a failed query is not an empty queue (but a `null`-serialized *empty* result is not
a failure — see above). See
[Failure handling](#failure-handling--a-sub-step-fails-the-loop-survives).

**Why `$ESCALATED` carries a 4th tab field (`.status`) and `$HUMAN` doesn't:** the `land-escalated`
query above passes no `--status` filter — deliberately, and unchanged by lode-o7ai — so it can
return a ticket that is *also* `deferred`, and §7 needs that per-row status to suppress the push and
annotate the report. The value is already on every row this query returns, so capturing it costs no
extra `bd` call (the same derivable-state principle §2's `title` comment below relies on). `$HUMAN`
never needs it — that query already filters to `--status open`, so a `human`-labeled row can never
be `deferred` in the first place. Rationale: lode-o7ai in
[docs/decisions.md](../../../docs/decisions.md).

**Why `$HUMAN` is subtracted against `bd blocked` before it becomes the real `$HUMAN` source
(lode-csxh):** a `human`-labeled ticket that is dependency-blocked is a sign-off placeholder for an
artifact that does not exist yet — it is not decidable, so it must not sit in `$CURRENT`, the
digest, or the push. `bd human list --json` carries no dependency fields, so the filter is a
subtraction against `bd blocked --json`'s id set, done once here, never touching `$ESCALATED` or
`$CLOSABLE`. The subtracted-out rows are never dropped from view — they are persisted to their own
report-only §2c section below, so a human ticket blocked on a deferred dependency does not vanish
from every surface indefinitely. A failed `bd blocked` query is treated exactly like a failed
`$ESCALATED`/`$HUMAN` query — it writes `source_query_failed` and suppresses §6/§7 for this pass,
never "nothing is blocked" (which would flood `$CURRENT` with the whole blocked set and false-notify
it as new). Full rationale, the three load-bearing constraints, and the deliberate
notify-on-unblock consequence: lode-csxh in [docs/decisions.md](../../../docs/decisions.md).

## 2. Collect epics ready for a human close-decision

An epic qualifies when it is `epic-audited` + still `open` + **every** `parent-child` child is
`closed` — `/epic-audit` deliberately never closes an epic itself, so nothing else flags these.
Same shared child-completion check `/epic-audit` and `/land` use, `scripts/epic-children-closed.sh`
(NOT `bd show`'s `.dependents` array — see the script's own header; that array is populated only
with the opt-in `--include-dependents` flag, and its absence made this exact check dead code in all
three skills until lode-v4rk):

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/lode-sweep-state"   # re-derive -- fresh Bash invocation, see §0

set -o pipefail   # REQUIRED -- see the note below.

CLOSABLE=""
# Pull id AND title in the ONE list read -- `bd list --json` rows already carry
# `title`, so re-fetching it per epic with a second `bd show` would be a wasted
# round-trip against derivable state. Capture the query into a plain variable FIRST so its exit
# status is checkable -- piping straight into the loop via `< <(...)` (the prior form) only ever
# exposes `read`'s own exit status to the `while`, silently swallowing a failed `bd`/`jq` upstream
# no matter how the pipeline's own status is set (this ticket's §2 finding).
if EPICS=$(bd list --type=epic --label epic-audited --status open --limit 0 --json \
  | jq -r '(. // []) | .[] | [.id, .title] | @tsv'); then
  while IFS=$'\t' read -r e TITLE; do
    [ -z "$e" ] && continue   # a genuinely empty $EPICS still yields one blank read via <<<
    [ "$(scripts/epic-children-closed.sh "$e")" = "true" ] || continue
    ROW=$(printf '%s\tepic-ready-to-close\t%s' "$e" "$TITLE")
    # The newline MUST sit outside the command substitution above: `$(...)` strips
    # trailing newlines, so building the row as `printf '...\n'` would silently drop
    # the separator and jam every epic onto ONE line (only visible with >=2 closable
    # epics, which is why it reads fine in a one-epic spot check).
    CLOSABLE="${CLOSABLE}${ROW}
"
  done <<< "$EPICS"
else
  # Same marker file as §1 -- either section's failure is sufficient to skip §6/§7 for this pass,
  # so it is one shared existence check, not a per-section one. Written inside the branch for the
  # reason §1's block spells out.
  touch "$SWEEP_TMP/source_query_failed"
fi
printf '%s' "$CLOSABLE" > "$SWEEP_TMP/closable"
```

**Why the query is captured into `$EPICS` first, rather than piped straight into `< <(...)` as
before:** a `while read` loop's own exit status is `read`'s, never the upstream pipeline's — no
setting of `pipefail` changes that, because process substitution runs the pipeline in a *separate*
subshell the loop's exit status never reflects. That is what let a failed `bd`/`jq` upstream pass
silently before: the loop itself always reported success (or simply produced zero rows, read as an
empty epic queue). Checking the `if EPICS=$(...); then … else …; fi` assignment's own
status, before the loop ever starts, is what actually surfaces the failure.

`--limit 0` for the same reason as §1 — same `$CURRENT`, same wholesale §6 rewrite.

## Report-only sections (§2a, §2b, §2c) — shared contract

§2a (`deferred` tickets), §2b (stranded `in_progress` tickets), and §2c (dependency-blocked `human`
tickets) are three report-only lists that share a **rendering contract** — how each list's result is
persisted and what it's excluded from — stated once here rather than three times below. §2a and §2b
additionally share a **collection contract** — how each list's own `bd` query is run. §2c has no
collection contract of its own: its data is the `bd blocked --json` call §1 already makes (to
compute the `$HUMAN` subtraction, lode-csxh), not an independent read — see §2c's own section below
for what stands in its place.

### Rendering contract (§2a, §2b, §2c — no exceptions)

**The sentinel, and why it can't collide with a real row.** Each section persists its result to its
own `$SWEEP_TMP` file (`$SWEEP_TMP/deferred` for §2a, `$SWEEP_TMP/stranded` for §2b,
`$SWEEP_TMP/blocked_human` for §2c) the same way §1 persists `$ESCALATED`/`$HUMAN` — §8 (a later,
separate Bash invocation) reads it back from disk rather than relying on the model's in-context
memory of the block's output, which is not the mechanism §0 says this file uses. On a query error
(`bd` or `jq`), the writer overwrites the capture — which may be partial or garbled — with the
literal string `SWEEP-QUERY-ERROR` instead of letting the pass abort. That gives each file three
readable states: *missing* (the writer never ran this pass — e.g. it crashed before reaching the
`printf`), *the sentinel* (the query itself errored, detected on the assignment itself, so the
failure replaces the capture before it can be written out as data), and *anything else* (the query
succeeded — zero or more real `@tsv` rows, each of which always contains a tab). The sentinel is a
single line with **no tab**, which is structurally impossible for a real row to produce (every row is
`<id>\t<title>` via `@tsv`) — so this isn't a string-luck collision avoidance, it's a format
invariant. §8 checks for the sentinel by exact match before treating a file's content as data.

**Deliberately excluded from everything else in this skill.** None of `$DEFERRED` (§2a),
`$STRANDED` (§2b), or the blocked-human rows (§2c) ever feed `$CURRENT` (§3) — none may enter
`$CURRENT_IDS`/`$NEW_IDS` (§5), drive the digest rewrite/no-op decision, or trigger the §7
`PushNotification`. None is ever written into the digest body (§6), and none carries **dedup
state** of its own — each is recomputed fresh, in full, every pass, straight into the §8 report.
(What a ticket entering or leaving each list *means* differs by section — see each section's own
note below.)

§8 owns what each state renders as — see its three-state rule, and
[Failure handling](#failure-handling--a-sub-step-fails-the-loop-survives).

### Collection contract (§2a, §2b only)

**`set -o pipefail` is what makes the failure detectable at all** — it is the load-bearing line in
each section's block, not hygiene. Without it, `VAR=$(bd … | jq …)` carries the exit status of the
*last* command in the pipeline, `jq` alone, and a failing `bd` never reaches it: measured on bd
1.1.0, a failed `bd list --limit 0 --json` writes its diagnostic to **stderr** and **zero bytes to
stdout**, so `jq` reads no input, emits nothing, and exits `0`. The assignment reports success and
the sentinel branch never fires — the phantom-empty read this section exists to prevent,
reintroduced one layer down. It is set inside each block, so it is scoped to that Bash invocation
(§0: each block is a fresh shell). §1 and §2 above carry the identical `pipefail` guard, at the
higher-consequence site (they gate the §6 digest rewrite) — via a shared `source_query_failed`
marker file rather than this section's `SWEEP-QUERY-ERROR` sentinel, since their consumer (§5) needs
"skip the rewrite", not "render as errored in a report line" (lode-5qbi).

Same `(. // [])` null-empty guard as §1/§2 — and the same `@tsv` as §2, which escapes a tab or
newline embedded in a title instead of letting it break the row.

**`--limit 0` — same reason as §1.** The stake specific to these sections: each promises its list
"in full, with no dedup" every pass, so a capped query would under-report past 50 while the §8 count
still read as the true total. (`lode-2gun` extended the same pin to `/land`, `/code`, `/epic-audit`,
`/release` and to `scripts/epic-children-closed.sh`, called from §2 above; `lode-9bbq` added
`.claude/statusline.sh`. A grep is not a substitute for that roster: `statusline.sh` calls
`bd -C "$cwd" list`, and the `-C` between `bd` and `list` is exactly what hid it from `lode-2gun`'s
literal `bd list` search. **But the roster is no longer what enforces this** — `lode-200t` added
`tests/test_bd_list_limit_gate.py`, which fails on a *new* unguarded call site rather than trusting
this list to stay current by itself. That test owns the scan surface and the exclusions; this
paragraph is documentation for a human reader, and deliberately does not restate them.)

Failure here is isolated to that step alone: the block writes the sentinel instead of aborting, and
the pass continues.

## 2a. Collect deferred tickets (report-only — never touches the digest or notify path)

A third, independent read, on its own track. `deferred`-status tickets are explicitly parked "deal
with later" by a human — the opposite of a fresh human-decision item — but `bd ready` hides them by
design and no other loop leg lists them, so once parked they otherwise vanish from every workflow
surface. I list them for visibility only:

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/lode-sweep-state"   # re-derive -- fresh Bash invocation, see §0

set -o pipefail   # REQUIRED, not hygiene -- see the shared report-only contract above.

# On a query error (bd or jq), overwrite the capture -- which may be partial or garbled -- with
# the sentinel. §8 tells that apart from both a missing file and a legitimately empty one.
if ! DEFERRED=$(bd list --status deferred --limit 0 --json \
  | jq -r '(. // []) | .[] | [.id, .title] | @tsv'); then
  DEFERRED="SWEEP-QUERY-ERROR"
fi
printf '%s' "$DEFERRED" > "$SWEEP_TMP/deferred"
```

The persistence/sentinel convention, the `(. // [])`/`@tsv` guards, the `--limit 0` stake, and what
this section is deliberately excluded from are all stated once, for both this section and §2b, in
[Report-only sections (§2a, §2b, §2c) — shared contract](#report-only-sections-2a-2b-2c--shared-contract)
just above. A ticket moving into (or out of) `deferred` is not a new human-decision item.

**The one deliberate overlap:** a ticket that is simultaneously `land-escalated` (§1) and `deferred`
is listed here (unconditionally, unannotated) *and*, on the pass it first appears, also in §7/§8's
`NEW HUMAN-DECISION ITEMS` block (there, annotated `(deferred)`) — decided, not a gap (lode-o7ai,
[docs/decisions.md](../../../docs/decisions.md)). This section's own listing is unaffected either
way — it stays exactly what it always was, every current `deferred` ticket, in full.

## 2b. Collect stranded in_progress tickets (report-only — never touches the digest or notify path)

A fourth, independent read, on its own track. Claiming a ticket sets `status=in_progress`, which
removes it from `bd ready` — so `/code` never picks it up again. Without a `ready-for-*` label it is
also invisible to `/code` phase 2, to `/code`'s `needs-rebase` sweep, and to `/land`; and if it is not
`deferred` and not `land-escalated`, nothing else in the pipeline sees it either. Every consumer keys
on either `bd ready` or a label, and `in_progress` + unlabeled satisfies neither — the ticket is
stranded silently. (A `human`-labeled ticket can be stranded the same way, for a different reason —
see the exclude-label list below.) I list them for visibility only:

**Age discriminator — DECIDED 2026-08-06 (maintainer, `lode-3k6x`; full record:
[docs/decisions.md](../../../docs/decisions.md), entry "`/sweep` §2b gets an age discriminator on
`started_at` (24h)").** Unlike §2a's `deferred` (a terminal, parked state), `in_progress` is a
*transient working state* — a coding producer claims its ticket up front (`bd update --claim` ->
`in_progress`) and only applies `ready-for-code-review` at hand-off, minutes-to-hours later, so for
that whole build window the ticket carries none of the exclude-labels below and is indistinguishable
from a stranding. Unfiltered, §2b therefore lists the live build queue every pass. So §2b filters on
`started_at` — "when claimed", not `updated_at` — with a **24h** threshold. Why that field and that
number (and why not 3 days) is settled in the decisions.md entry above; don't re-derive it here.
`bd list` exposes no `--started-*` flag, but the `--json` rows carry `started_at` and §2b already
pipes through `jq`, so the discriminator is one added `select(...)` clause, no new dependency:

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/lode-sweep-state"   # re-derive -- fresh Bash invocation, see §0

set -o pipefail   # REQUIRED, same reason as §2a.

# Same sentinel convention as §2a -- see that section's note for the rationale.
# select(...): only tickets claimed (started_at) more than 24h ago (86400s -- a DECIDED threshold,
# see the age-discriminator note above; do not retune it here). A null/missing started_at
# (should not happen for in_progress, but defensively) is treated as stranded, not filtered out,
# since there is no age evidence to exclude it on.
if ! STRANDED=$(bd list --status in_progress --limit 0 --json \
  --exclude-label ready-for-code-review,ready-for-land,needs-rebase,sweep-digest,land-escalated \
  | jq -r '(. // []) | .[]
      | select(.started_at == null or (.started_at | fromdateiso8601) < (now - 86400))
      | [.id, .title] | @tsv'); then
  STRANDED="SWEEP-QUERY-ERROR"
fi
printf '%s' "$STRANDED" > "$SWEEP_TMP/stranded"
```

The persistence/sentinel convention, the `(. // [])`/`@tsv` guards, the `--limit 0` stake, and what
this section is deliberately excluded from are all stated once, for both this section and §2a, in
[Report-only sections (§2a, §2b, §2c) — shared contract](#report-only-sections-2a-2b-2c--shared-contract)
above. A ticket becoming (or ceasing to be) stranded is not a new human-decision item — it is
surfaced so a human notices it, not resolved by this skill.

**The exclude-label list — deliberately not the fuller set §1 might suggest.**
`ready-for-code-review`, `ready-for-land`, and `needs-rebase` exclude live mid-pipeline work — those
rows are not strandings, they are mid-flight. `sweep-digest` excludes my own digest issue (claimed on
purpose, never a stranding). That leaves `land-escalated`, which *is* excluded, and the label a
reader would expect to see beside it, `human`, which deliberately is not. Each for its own reason:

- **`land-escalated` is on the exclude-label list, and correctly so.** §1's `land-escalated` query
  passes no `--status` filter, so an `in_progress` + `land-escalated` ticket already reaches
  `$CURRENT` and the digest through §1. Were §2b not to exclude it, the same ticket would surface a
  second time here with no benefit — §1 already has it covered regardless of status.
- **`human` is deliberately NOT excluded.** §1's `human` source (`bd human list --status open
  --json`, §1 above) is status-filtered — an `in_progress` ticket that also carries the `human`
  label is invisible to that query. If §2b also excluded `human`, such a ticket would be surfaced by
  **neither** §1 nor §2b — stranded from every consumer, exactly the class of silence this section
  exists to close. So §2b's exclude-label list is deliberately narrower than "everything §1 also
  looks at": it excludes only the label whose §1 counterpart is status-agnostic
  (`land-escalated`), and leaves `human` in the stranded list, where an `in_progress` human-labeled
  ticket will actually be seen. Full rationale, and why this diverges from lode-o7ai's decided §1 x
  §2a overlap: lode-ppki in [docs/decisions.md](../../../docs/decisions.md).

**This roster is enforced by a gate test, not by staying current on its own** (decided `lode-mm73`,
[docs/decisions.md](../../../docs/decisions.md)). `tests/test_sweep_pipeline_label_roster_gate.py`
scans every `--add-label`/`bd label add` site across `.claude/skills/*/SKILL.md` and
`.claude/agents/*.md` and fails on a label applied to a ticket (not an epic) that this exclude-label
list doesn't cover — the same shape as `tests/test_bd_list_limit_gate.py` above. The test owns the
scan surface and its exclusions; like the `--limit 0` paragraph above, this prose deliberately does
not restate them.

## 2c. Blocked human tickets (report-only — never touches the digest or notify path; DECIDED lode-csxh)

A fifth, independent list, but not an independent **read**: its data is the `bd blocked --json` call
§1 already made to subtract dependency-blocked ids out of `$HUMAN` (see §1's note above). §2c is
just this pass's persistence of the rows §1 partitioned out — every open `human`-labeled ticket that
is currently blocked on an unclosed `blocks` dependency, listed for visibility only, so it does not
vanish from every workflow surface for the epic's whole lifetime the way it did before this ticket
(e.g. `lode-fhql.12`/`.13`/`.14`, blocked on their builder tickets).

§1 already wrote this section's file (`$SWEEP_TMP/blocked_human`, `<id>\t<title>` rows, or the
`SWEEP-QUERY-ERROR` sentinel on a failed `bd blocked`) as part of partitioning `$HUMAN` — there is no
separate fenced block here to run, and so no `--limit 0`/`set -o pipefail` collection contract of its
own: `bd blocked` exposes no `--limit` flag at all (checked directly against `bd blocked --help`), so
there is nothing to pin. The rendering contract — the persistence/sentinel convention, the
three-state file contract, and what this section is deliberately excluded from — is stated once, for
this section and §2a/§2b both, in [Report-only sections (§2a, §2b, §2c) — shared
contract](#report-only-sections-2a-2b-2c--shared-contract) above, and applies to §2c identically,
with one difference in how its failure surfaces: since §2c's data is §1's `bd blocked` call rather
than a query of its own, a failure there writes `source_query_failed` (§1) *and* the
`SWEEP-QUERY-ERROR` sentinel into `$SWEEP_TMP/blocked_human`, both at once — rather than the sentinel
alone, as §2a/§2b's own failed queries write.

A ticket entering or leaving this list is not itself a new human-decision item — but leaving it (its
blocking dependency closes) is exactly what makes the ticket enter `$CURRENT` for the *first* time in
§1/§3, which *does* trigger a fresh `PushNotification` on that later pass (§5/§7) — the sign-off push
arrives exactly when the artifact it signs off on exists. That is the decided, deliberate point of
this section, not a side effect: lode-csxh in [docs/decisions.md](../../../docs/decisions.md).

## 3. Build the current queue (dedup on stable IDs)

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/lode-sweep-state"   # re-derive -- fresh Bash invocation, see §0

# Load §1/§2's results back from disk and assert each one loaded -- a missing file means that
# step never ran this pass, and continuing on a phantom-empty queue would risk deleting real
# escalations from the digest (§5's hard precondition, in reverse).
#
# scripts/land-state-load.sh (lode-dc4n) makes that the "missing fatal, empty OK" default policy,
# which is what all three sites already had; do not add --require-nonempty, an empty queue is the
# ordinary healthy case (lode-3oik).
ESCALATED="$(scripts/land-state-load.sh "$SWEEP_TMP/escalated" -- \
  "§1 did not run this pass")" || exit 1
HUMAN="$(scripts/land-state-load.sh "$SWEEP_TMP/human" -- \
  "§1 did not run this pass")" || exit 1
CLOSABLE="$(scripts/land-state-load.sh "$SWEEP_TMP/closable" -- \
  "§2 did not run this pass")" || exit 1

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
DIGEST_ROWS=$(bd list --label sweep-digest --all --limit 0 --json)
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
  DIGEST_ID=$(bd create --type=chore --title="Human-decision digest (auto-maintained by /sweep — do not build)" \
    --label=sweep-digest --description="(bootstrapping — /sweep fills this in on this same pass)" --silent)
  bd update "$DIGEST_ID" --claim
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

# Hard precondition (below): a failed §1/§2 source query is indistinguishable from an empty
# queue, and §6 rewrites the digest WHOLESALE from $CURRENT -- so a failure here must suppress
# the rewrite, not fall through to it. This is the actual enforcement of that rule; §1/§2's
# failure branches write the marker, this reads it.
if [ -f "$SWEEP_TMP/source_query_failed" ]; then
  echo "SOURCE QUERY FAILED THIS PASS (§1 and/or §2) -- skipping §6 rewrite and §7" \
    "notification; prior digest left untouched. Re-run /sweep next tick." >&2
  exit 1
fi

# Re-derive DIGEST_ID -- cheap and deterministic, so re-running the query beats persisting an id.
# The script refuses unless exactly one digest exists; a bare `.[0].id` would silently pick the
# first of several duplicates (§4's `N > 1` anomaly) or yield "null" when none exists (§4's
# `N == 0`). Quote its stderr rather than re-deriving a cause of my own.
DIGEST_ID="$(scripts/sweep-digest-id.sh)" || exit 1
# Default policy (missing fatal, empty OK) -- what this site always had; do not add
# --require-nonempty, an empty $CURRENT is legitimate (lode-3oik).
CURRENT="$(scripts/land-state-load.sh "$SWEEP_TMP/current" -- \
  "§3 did not run this pass")" || exit 1

LAST_BODY=$(bd show "$DIGEST_ID" --json | jq -r '.[0].description')
LAST_IDS=$(printf '%s\n' "$LAST_BODY" | grep '^SWEEP-ITEM' | awk '{print $2}' | sort -u)
CURRENT_IDS=$(printf '%s\n' "$CURRENT" | awk -F'\t' '{print $1}' | sort -u)

NEW_IDS=$(comm -13 <(printf '%s\n' "$LAST_IDS") <(printf '%s\n' "$CURRENT_IDS"))

# Persist NOW, before §6 (a separate, later Bash invocation) rewrites the digest
# body this block just read as $LAST_BODY. §7 needs this exact value -- the
# delta against the digest as it stood BEFORE the rewrite -- but §7 runs after
# §6, by which point re-deriving from the digest would see the body §6 just
# wrote (LAST_IDS == CURRENT_IDS by construction) and compute an always-empty
# NEW_IDS (lode-fm7t). Same `printf '%s'` idiom as §1/§2/§2a/§2b/§3 above: no
# trailing newline, so a legitimately-empty NEW_IDS lands as a ZERO-BYTE file
# rather than one blank line -- §7 distinguishes an absent file ("§5 never ran")
# from an empty one ("nothing new"), so that distinction must survive the write.
printf '%s' "$NEW_IDS" > "$SWEEP_TMP/new_ids"
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
escalations from the durable record** and then re-notify them as "new" on the next pass. Enforced
by the `$SWEEP_TMP/source_query_failed` check at the top of this block's code above — a detected
failure exits this block before `$DIGEST_ID` is even derived, so §6 and §7 (both later, separate
Bash invocations) never run this pass, and the prior digest is left untouched. Stale is recoverable;
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
DIGEST_ID="$(scripts/sweep-digest-id.sh)" || exit 1
BODY_FILE="$(mktemp)"
# …write the digest body (format above) into "$BODY_FILE"…
bd update "$DIGEST_ID" --body-file "$BODY_FILE"
```

## 7. Notify (only when there is a new item to push)

I run as a **skill in the main conversation**, so I have the main session's tools — including
`PushNotification`, which reaches a human who is away from the terminal. That is the entire point of
this leg: the `land-escalated` and `human` labels already sat in `bd`, where nobody was looking.

**First, read back this pass's new ids and split them on deferred status.** This is its own, separate Bash tool
invocation — nothing from §5 survives into it (lode-sfnb; §0's governing rule) — so `$CURRENT` is
re-derived from the scratch file §3 wrote, the sanctioned remedy for cross-block state (re-deriving
is cheap and deterministic; see this skill's own §0 and `docs/agents-workflow.md`'s
cross-block-shell-state section). The new-id set itself is **not** re-derived here — it is read back from
`$SWEEP_TMP/new_ids`, which §5 persisted *before* §6 rewrote the digest body. Re-deriving it the same
way §5 did — reading the digest's current description — would see the body §6 just wrote, where
`LAST_IDS == CURRENT_IDS` by construction, and always compute an empty `NEW_IDS` (lode-fm7t: this is
the defect this file exists to fix, so this block must never read the digest description again for
this purpose). Nothing here calls `scripts/sweep-digest-id.sh` or `bd show "$DIGEST_ID"` any more —
there is no digest read left in this block:

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/lode-sweep-state"   # re-derive -- fresh Bash invocation, see §0

# Default policy (missing fatal, empty OK) -- what this site always had; do not add
# --require-nonempty, an empty $CURRENT is legitimate (lode-3oik).
CURRENT="$(scripts/land-state-load.sh "$SWEEP_TMP/current" -- \
  "§3 did not run this pass")" || exit 1
# Existence, not content: the awk below reads $SWEEP_TMP/new_ids as a file, so
# nothing here needs its value in a variable. An ABSENT file means §5 never ran
# this pass and is a hard stop; a file that exists but is EMPTY is the ordinary
# "nothing new" pass and must run on through to a clean no-op (lode-fm7t,
# acceptance #3 -- missing vs. empty are distinct and must never be conflated).
[ -f "$SWEEP_TMP/new_ids" ] || {
  echo "GATE COULD NOT RUN: $SWEEP_TMP/new_ids missing -- §5 did not run this pass" >&2
  exit 1
}

# Split the new ids into "report it" (all of them) vs "push it" (not the deferred ones)
# -- decided, lode-o7ai; rationale in docs/decisions.md. Only $ESCALATED-sourced rows
# carry a 4th field (§1/§3), so a $HUMAN/$CLOSABLE row's empty $4 is correctly never
# "deferred" and $CURRENT alone is enough -- no second read of $SWEEP_TMP/escalated.
# Emit ONLY fields 1-3: §8's report format is `<id> <kind> <title>`, so passing the
# row through whole would leak the raw status ("<title>\topen") into every escalated
# row. Truncate both outputs first, so a zero-match pass leaves them empty, not stale.
: > "$SWEEP_TMP/new_annotated"
: > "$SWEEP_TMP/push_ids"
awk -F'\t' -v ann="$SWEEP_TMP/new_annotated" -v push="$SWEEP_TMP/push_ids" '
  NR == FNR      { new[$1] = 1; next }          # pass 1: the new ids
  !($1 in new)   { next }                       # pass 2: $CURRENT, new rows only
  { row = $1 "\t" $2 "\t" $3 }
  $4 == "deferred" { print row " (deferred)" > ann; next }
                   { print row > ann; print $1 > push }
' "$SWEEP_TMP/new_ids" "$SWEEP_TMP/current"
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

This is its own, separate Bash tool invocation — nothing from §1/§2a/§2b survives into it (§0's
governing rule) — so `$DEFERRED`, `$STRANDED`, and `$BLOCKED_HUMAN` are re-derived from the scratch
files §1/§2a/§2b already wrote, not from in-context memory of those blocks' output:

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/lode-sweep-state"   # re-derive -- fresh Bash invocation, see §0

# Deliberately NOT §3's `|| { ... exit 1; }` guard: §8 must finish either way (the digest push
# below is unrelated to the deferred/stranded lists), so a missing file degrades that section alone.
# One 3-valued state per list, not a pair of booleans: `missing` (§2a/§2b's block never ran this
# pass), `error` (it ran, but its query failed -- the SWEEP-QUERY-ERROR sentinel), or `ok` (real
# content, possibly legitimately empty). One variable makes the three mutually exclusive by
# construction, so no combination has to be ruled out in prose.
#
# lode-3oik: NOT retrofitted onto scripts/land-state-load.sh -- a missing $SWEEP_TMP/deferred is a
# non-fatal third state, which neither of that script's two policies expresses (both exit 1 on
# missing). Rationale: docs/decisions.md, lode-3oik.
if DEFERRED="$(cat "$SWEEP_TMP/deferred" 2>/dev/null)"; then
  DEFERRED_STATE=ok
  [ "$DEFERRED" = "SWEEP-QUERY-ERROR" ] && DEFERRED_STATE=error
else
  DEFERRED_STATE=missing
fi

# lode-3oik: NOT retrofitted onto scripts/land-state-load.sh -- a missing $SWEEP_TMP/stranded is a
# non-fatal third state, same reason as the deferred read above.
if STRANDED="$(cat "$SWEEP_TMP/stranded" 2>/dev/null)"; then
  STRANDED_STATE=ok
  [ "$STRANDED" = "SWEEP-QUERY-ERROR" ] && STRANDED_STATE=error
else
  STRANDED_STATE=missing
fi

# lode-3oik: NOT retrofitted onto scripts/land-state-load.sh -- a missing $SWEEP_TMP/blocked_human is a
# non-fatal third state, same reason as the two reads above.
# lode-csxh: §1 wrote this file (as part of partitioning $HUMAN), not a §2c block of its own --
# see §2c's own note. That changes only WHICH block a `missing` state indicts, not the policy.
if BLOCKED_HUMAN="$(cat "$SWEEP_TMP/blocked_human" 2>/dev/null)"; then
  BLOCKED_HUMAN_STATE=ok
  [ "$BLOCKED_HUMAN" = "SWEEP-QUERY-ERROR" ] && BLOCKED_HUMAN_STATE=error
else
  BLOCKED_HUMAN_STATE=missing
fi

# §1/§2's shared marker (written there, enforced in §5). Read from DISK, like everything else in
# this block: §5's stderr message is in-context state, which §0 says this file never relies on.
if [ -f "$SWEEP_TMP/source_query_failed" ]; then SOURCE_STATE=error; else SOURCE_STATE=ok; fi

scripts/bd-dolt-push.sh   # only if step 6 wrote the digest — publish over refs/dolt/data, durable cross-machine

# lode-8xl2: the always-present "Actionable now" section, rendered LAST in the report — after the
# report-only sections and after the NEW HUMAN-DECISION ITEMS block (when present), not here.
# Source is every row of $SWEEP_TMP/current (§3), fields 1-3, EXCLUDING any row whose optional 4th
# field (the $ESCALATED-sourced .status, §1) is `deferred` — a deferred row is never listed here;
# it already appears in §2a's unchanged "Deferred (surfaced, not reviewed)" section, so no
# `(deferred)` annotation is needed. Report-only, feeds nothing (no digest change, §6 unchanged; no
# dedup state; no PushNotification change, §7 unchanged). Missing is fatal here the same way it is
# for §5/§7's own re-derivation of $CURRENT: §3 must have run for this section to have anything to
# show — a hard exit here is deliberate and does NOT contradict this block's opening note, which
# scopes "§8 must finish either way" to the three report-only lists (a missing $SWEEP_TMP/deferred
# is an ordinary third state; a missing $SWEEP_TMP/current means the pass itself never happened).
# It runs AFTER the digest push above and never before it precisely so that exit can never suppress
# the publish.
# An item appearing in both this section and the NEW HUMAN-DECISION ITEMS block above it is
# deliberate — "what's new" vs. "what's decidable now" answer different questions.
CURRENT="$(scripts/land-state-load.sh "$SWEEP_TMP/current" -- \
  "§3 did not run this pass")" || exit 1
ACTIONABLE_NOW=$(printf '%s\n' "$CURRENT" | awk -F'\t' '
  NF == 0 { next }
  $4 == "deferred" { next }
  { print $1 " " $2 " " $3 }
')
```

The rule is one rule, over all three report-only lists — for each `<list>` in {`deferred`,
`stranded`, `blocked_human`} there are three mutually exclusive states, and §8 must not confuse
them:

- **`missing`** — the block that writes that section's file never ran this pass at all (e.g. it
  crashed before reaching its `printf`) — §2a's or §2b's own block, or, for `blocked_human`, **§1**
  (which writes that file as part of partitioning `$HUMAN`; §2c has no block of its own). Section
  body: "`<list>` list unavailable this pass"; summary field: `unavailable`, never `0`.
- **`error`** — that writing block *did* run, but its `bd`/`jq` query failed and wrote the
  sentinel. Section body: "`<list>` query failed this pass"; summary field: `error` — never `0`,
  and never `unavailable`, which is a different failure with a different remedy.
- **`ok`** — the query succeeded; report it normally, including the legitimately-empty case
  (section body `(none)`, summary field `<len $<LIST>>`, which may be `0`).

None of the three states aborts this block (the digest push above still has to run), and none
suppresses or is suppressed by the §1/§2 escalation/human/epic reporting or by the other
report-only sections — every one of these reads its own, separately-persisted scratch file, and
each of the three lists is judged solely on its own file's content.

Report exactly one line, then the deferred section (§2a, always present), the stranded section
(§2b, always present), and the blocked-human section (§2c, always present), plus, when non-empty,
the loud new-items block, and finally the always-present **Actionable now** section (last):

```
sweep: queue depth <len $CURRENT_IDS>, <len $NEW_IDS> new, <count of epic-ready-to-close rows> closable, <deferred field> deferred, <stranded field> stranded, <blocked_human field> blocked

## Deferred (surfaced, not reviewed) (<deferred field>)
<id> <title>
...
(none) | unavailable this pass | query failed this pass

## Stranded (in_progress 24h+, no pipeline label) (<stranded field>)
<id> <title>
...
(none) | unavailable this pass | query failed this pass

## Blocked human tickets (dependency-blocked, not yet decidable) (<blocked_human field>)
<id> <title>
...
(none) | unavailable this pass | query failed this pass
```

`<deferred field>`/`<stranded field>`/`<blocked_human field>` and the alternatives on the last line
of each section are the three states above, per that list's `$<LIST>_STATE`. On `ok`, each section
lists every current row (id + title) each pass, in full, with no dedup.

When `$SWEEP_TMP/new_annotated` (§7) is non-empty, follow the three sections above with:

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

**Finally, always append the `## Actionable now` section — DECIDED (maintainer, 2026-08-14, amended
same day, lode-8xl2): every pass's report ends with the full list of human decisions that are
actionable RIGHT NOW, not just the delta.** Its rows are `$ACTIONABLE_NOW` (computed in §8's script
above): every row of `$SWEEP_TMP/current` (§3), fields 1-3, in full, EXCLUDING any row whose 4th
field is `deferred` (that row already appears in §2a's "Deferred (surfaced, not reviewed)" section,
unchanged — no `(deferred)` annotation is needed here since deferred rows are excluded outright):

```
## Actionable now (<count of rows in $ACTIONABLE_NOW>)
<id> <kind> <title>
...
(none)
```

This is distinct from the three report-only lists above (§2a/§2b/§2c list *parked/stranded/
not-yet-decidable* work `bd ready` already hides) and from the `NEW HUMAN-DECISION ITEMS` block
above it (that block is delta-only — new since the last digest, deferred rows included and
annotated). This section is the standing, decidable-now queue — `land-escalated`, open `human`, and
`epic-ready-to-close` rows minus anything deferred — every pass, so a human reading the transcript
never has to run `bd show` to see what is still waiting on them and can act on it without first
filtering out parked items themselves. It feeds nothing downstream: no digest change (§6 is
unchanged), no dedup state of its own, no `PushNotification` change (§7 is unchanged — the push
still covers only `$SWEEP_TMP/push_ids`, the NEW non-deferred ids). A row appearing here **and** in
the `NEW HUMAN-DECISION ITEMS` block on the same pass is deliberate, not redundant — "what's new"
vs. "what's decidable now" answer different questions.

If §4 found `N > 1` duplicate digests, any sub-step in §1/§2 failed (`$SOURCE_STATE` = `error`, in
which case also say that §6 and §7 were skipped and the prior digest is stale but intact), or the
§2a deferred, §2b stranded, or §2c blocked-human query failed, say so plainly in the same report —
the pass still ends cleanly either way. A failed `bd blocked` query is both at once: it sets
`$SOURCE_STATE = error` (via §1's shared marker) *and* `$BLOCKED_HUMAN_STATE = error` (via its own
sentinel) — report both, not just one.

## Failure handling — a sub-step fails, the loop survives

A failed sub-step must never abort the pass — and must never corrupt the digest either. Those pull
in opposite directions, and the digest wins: it is rebuilt wholesale from `$CURRENT`, so a source
query that errors is indistinguishable from "that queue is empty", and rewriting on it would delete
real items from the durable record a human relies on.

- If any §1/§2 query errors (`bd` or `jq`), §1/§2's `set -o pipefail` + `if !` guards detect
  it and write `$SWEEP_TMP/source_query_failed`; §5 checks that marker and exits before §6/§7 ever
  run, leaving the prior digest exactly as it was. §8 re-reads the same marker from disk (never
  from memory of §5's stderr — §0) and reports the failure. Stale, not truncated. An *empty* result that serializes as literal `null` is **not** a failure — the
  `(. // [])` guard in §1/§2 normalizes it to an empty list, so a queue that legitimately emptied
  still rewrites the digest and drops the resolved item promptly (a bare `jq '.[]'` abort on that
  `null` would otherwise look like a failed query and wrongly suppress the rewrite).
- The §6 rewrite is all-or-nothing: it either completes cleanly or is skipped (no partial
  `--body-file` write).
- If §4 finds `N > 1` digests, the write path stops for the pass (that anomaly is reported, never
  guessed at).
- **A report-only section's failure is isolated to that section alone** — this covers §2a
  (deferred) and §2b (stranded) identically: if either query errors, its block writes the
  `SWEEP-QUERY-ERROR` sentinel instead of the (possibly-partial) query output, and §8's three-state
  rule — the canonical statement of what each state renders as — reports it. Neither a failed query
  nor a missing scratch file may suppress the §6 rewrite or §7 notification for the (unrelated)
  escalation/human/epic queue; the reverse holds too (a §1/§2 failure never suppresses §2a or §2b,
  neither of which has a rewrite to protect), and so does the §2a-vs-§2b case — each is its own
  isolated read. **§2c is the one exception, deliberately:** its query is §1's own `bd blocked`
  call, so a failure there is *not* isolated the way §2a/§2b's are — it writes both
  `$SWEEP_TMP/blocked_human`'s `SWEEP-QUERY-ERROR` sentinel (for §8's report) *and*
  `$SWEEP_TMP/source_query_failed` (§1's shared marker, since a failed `bd blocked` must not be
  read as "nothing is blocked" — see §1's note). The rewrite-suppression half is real, not
  redundant caution.
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
the stranded section (§2b, always present), the blocked-human section (§2c, always present), the
full **NEW HUMAN-DECISION ITEMS** block when `$NEW_IDS` is non-empty (annotated `(deferred)`
per-row where applicable, per §7 — lode-o7ai), and finally — always, last — the **Actionable now**
section (every non-`deferred` row of `$CURRENT`, in full, every pass — DECIDED lode-8xl2), plus any
duplicate-digest anomaly and any sub-step that failed. A clean, unchanged queue is a valid, common
outcome — I say so plainly and stop.
