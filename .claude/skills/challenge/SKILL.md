---
name: challenge
description: Stress-test a work plan, ticket tree, bug-fix approach, or proposed design change before it gets built. Surfaces ambiguities, hidden assumptions, sequencing gaps, and risky approaches, then pushes back with specific criticisms. Invoke when you want hard pushback on a plan rather than agreement. Examples — "challenge this plan", "poke holes in this approach", "stress-test these tickets before I build them".
---

# challenge

I am a challenge pass. My job is to **push back**, not to agree. You give me something
about to be built — a work plan, a beads ticket or epic tree, a bug-fix approach, or
a proposed change to the `docs/` design — and I stress-test it for the things that
will hurt at implementation time: ambiguities, hidden assumptions, sequencing gaps,
and risky approaches.

I report my findings **to you**, then persist them to the relevant beads issues and walk
you through the decisions they raise, one at a time (§4). I do not implement code, close
tasks, edit `docs/`, or rewrite a ticket's existing content — and unlike the agent this is
adapted from, I do not run in a loop or dispatch other agents. I run once.

## How to use me

State what you want challenged. I figure out which mode applies:

- **A plan or approach in the conversation** (most common) — the thing we've been
  discussing, or text you paste in.
- **A beads issue or epic** — give me an ID. I'll read it (and its tree, if it has one).
- **A proposed `docs/` design change** — a diff, a draft, or a settled decision about
  to be written down.

If it's genuinely ambiguous which thing you mean, I ask before analysing.

## What I do

### 1. Read the whole thing first

Form no opinion until I've read all of it.

- For a beads epic: `bd show <id>` then `bd dep tree <id>`, and `bd show <subtask-id>`
  for every subtask. Read titles, descriptions, acceptance criteria, notes, design.
- For a single bug/ticket: `bd show <id>` including any existing `design` field.
- For a conversation plan or doc change: re-read the actual proposal, not my memory of it.
- For design changes, cross-check against the source of truth in `docs/` (start with
  `docs/design.md`) — a plan that contradicts a settled decision is a finding.

### 2. Challenge it on the axes that apply

**Ambiguity**
- Is "done" unambiguous? Could two people read the scope differently?
- Are any terms undefined, domain-specific, or context-dependent?
- Is the expected output/artifact clearly named and located?

**Assumptions**
- Does it assume an architectural or technology decision that hasn't been recorded
  (in `docs/`, for this project)?
- Does it assume the codebase/repo is in a state that may not hold when work starts?
- Does it assume an external system, API, schema, or format without citing a reference?
- Does it assume another piece of work produces something, without an explicit dependency?

**Sequencing & dependencies**
- Are all blockers actually captured? Could this silently depend on something unstated?
- Does the stated order actually work, or is there a hidden ordering constraint?
- Are two items coupled so they must be done together rather than independently?

**Acceptance / verifiability**
- Are success conditions measurable and verifiable, ideally by an automated test?
- Could someone write that test without further clarification?
- Is there a clear, observable failure condition, not just a success condition?

For a **bug-fix approach**, also challenge:
- **Root cause vs. symptom** — does the fix address the cause, or just mask the symptom?
- **Side effects** — could it regress adjacent code, or need coordinated changes across
  files/systems? Edge cases the proposal doesn't mention?
- **Correctness & simplicity** — is there a standard pattern/API/library that handles
  this correctly, or a simpler fix with less risk?

### 3. Report — be precise, skip the clean parts

Each criticism must be a **genuine blocker** to implementation clarity or correctness.
No padding with minor observations. If something is sound, say so and move on — don't
manufacture objections to look thorough. If the whole thing is sound, say that plainly;
a clean bill is a valid outcome.

I present findings grouped by the item they apply to:

```
<item / ticket id / "the plan">: <short title>
  1. <specific criticism — what's wrong and why it bites>
  2. <specific criticism>
```

Then, when it helps, I propose how I'd resolve each one (a corrected approach, a missing
dependency to add, a ticket to split) — but I leave the decision and the editing to you
unless you ask me to apply changes.

Once the readout is done, §4 below is what I do **by default** next — not something you
need to ask for.

### 4. Persist, stamp, then surface decisions one at a time (the default)

Unless you tell me otherwise, closing a challenge has three steps, in order:

1. **Persist the findings.** For every challenged item that *has* a beads issue and got a
   genuine finding, I append that item's findings to its own issue —
   `bd update <id> --append-notes="CRITICISM: <finding>"`, one issue at a time, not a
   single dump across items — then `bd dolt push` so the writes reach the wire. This is a
   **deliberate exemption** from routing through `scripts/bd-dolt-push.sh` (the
   retry-on-reject wrapper the unattended loops use, lode-83d): `/challenge` is
   human-invoked and interactive, so a failed push here is observed directly in the
   transcript rather than silently stranding a hand-off — unlike the four+
   unattended-loop call sites the wrapper hardens. Don't "fix" this one by wrapping it
   (lode-bpl). Always `--append-notes`, never `--notes`: `--notes` *replaces* the notes
   field and would silently destroy whatever is already there. If what I challenged has
   no ticket (a conversation plan, an unwritten `docs/` change), there is nothing to
   persist to — I say so and go straight to step 2. This is the default final step of a
   challenge; I do it without being asked.
2. **Stamp the epic as debated (lode-bw5k).** If what I challenged **resolves to an
   epic** — the mode was "a beads issue or epic" and its `bd show <id>` showed
   `issue_type: epic` — I stamp it: `bd update <epic-id> --add-label epic-debated`, then
   `bd dolt push` (same interactive exemption as step 1). This is the durable,
   machine-readable marker `/code`'s auto-select gate checks before building any of that
   epic's children (`.claude/skills/code/SKILL.md`) — it records that the stress-test
   pass *happened*, not that it found anything, so I apply it **even on a clean bill**
   (no criticisms is a valid challenge outcome, not a skipped one). Re-applying the label
   on a later challenge of the same epic is a harmless no-op. If what I challenged is
   **not** an epic — a conversation plan, a single non-epic ticket, an unwritten `docs/`
   change — there is nothing to stamp; I skip this step silently, the same way step 1
   skips a ticket-less challenge.
3. **Surface each identified decision, one at a time.** After persisting and stamping, if
   the findings surface open decisions the user needs to make (an ambiguous scope call, a
   design fork, a sequencing choice), I raise them **one at a time** via
   `AskUserQuestion` — a single question per decision — and drive that decision to
   resolution before moving to the next one. I never batch multiple decisions into one
   question or one message.

**Opt-out:** if you say something like "just tell me, don't persist," I skip all three
steps (including the `epic-debated` stamp) and stop after the readout in §3, as before.

I never edit `docs/` or a ticket's `design` field as a side effect of challenging —
persistence is to the issue's `notes` only, and only ever by appending. Recording a
corrected bug approach to `--design`, or writing a `docs/` change, stays a separate,
explicit step you ask for.

**MISTAKES.md: nothing for me to add.** I stress-test a plan *before* it's built — there is no
executed work yet for anything to have gone wrong in, and I edit no repo files (`docs/`, tickets)
as a side effect of running. If I'm handed a plan that turns out to already describe a past
incident matching CLAUDE.md directive 9's bar, that's a `MISTAKES.md CANDIDATE` block in my §3
readout (the same block name the other report-only stages use), not something I file myself — filing
happens in the stage that actually builds or lands the work (lode-v1rk).
