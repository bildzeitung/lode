---
name: release
description: Propose the next SemVer version from commit history since the latest vX.Y.Z tag, compile the release notes from the resolved bd epics/tickets in that window (itemized + categorized, delivered as the annotated tag body that CI publishes), get both confirmed (or take an explicit version override), and drive scripts/release.sh to cut the release. Thin wrapper — no build logic of its own; scripts/release.sh (lode-0ru.2) owns the actual gate + tag + push. Parses conventional-commit prefixes (feat -> minor, fix -> patch, `!`/BREAKING CHANGE -> major, but pre-1.0 a breaking change bumps MINOR per docs/release.md) and defaults to a PATCH proposal when no commit carries a recognized prefix. Examples — "/release", "/release patch", "/release minor", "/release major", "/release 0.2.0", "cut a release", "what's the next version".
---

# release

I am a **thin wrapper** over `scripts/release.sh` (lode-0ru.2) — I compute *what* the next version
should be and get a human to confirm it; the script owns the actual gate + tag + push. I have **no
build logic of my own**: I never tag, push, or run the test gate directly — `scripts/release.sh`
does all of that, and re-checks everything itself regardless of what I found. See
[`docs/release.md`](../../../docs/release.md) for the full release design; where this skill and that
doc disagree, the doc wins.

I run on the **main checkout, on `trunk`** — same as `scripts/release.sh` requires (it refuses to run
anywhere else). I am not a producer task: there is no worktree, no bd ticket, no `ready-for-review`
hand-off here. I propose a version, compile the release notes from the resolved ticket record,
confirm both, and invoke the script.

## How to use me

- **`/release`** — derive the proposal from commit history since the latest tag (the common case).
- **`/release patch`** / **`/release minor`** / **`/release major`** — skip history parsing, bump the
  latest tag by the named part.
- **`/release X.Y.Z`** — skip everything, propose exactly that version (still confirmed, still gated
  by the script's own monotonicity check).

## What I do

### 1. Find the latest tag

Delegate to `scripts/release-latest-tag.sh` (lode-b2bf) — the SemVer-greatest `vX.Y.Z` tag, not
just the most recently created one. This used to be an inline tag-selection loop +
`version_gt()` comparator embedded directly in this file, hand-duplicated (and free to drift) in
`scripts/release.sh`'s own monotonicity check — same "ungated inline shell in a SKILL.md rots
silently" lesson as §2's `scripts/release-bump.sh` (lode-ns3r) below. It is now an extracted,
tested script that both this skill and `scripts/release.sh` call, so there is exactly one
implementation of tag selection and SemVer comparison to keep correct:

```bash
LATEST_TAG="$(scripts/release-latest-tag.sh)" || {
  echo "GATE COULD NOT RUN: scripts/release-latest-tag.sh failed" >&2
  # Exits 2 for a machine fault (git failure) and never for a statement
  # about which tag is latest, same convention as scripts/release-bump.sh.
  # Its stderr has already surfaced the detail verbatim. Stop -- do NOT
  # silently guess a baseline tag.
  exit 1
}
```

**Empty `LATEST_TAG`** (no matching tag exists yet) → this is the first release. Per
`docs/release.md` the first release is pinned to **`v0.1.0`** — propose that directly (no commit
parsing needed) and skip to confirmation.

### 2. Derive the proposal

**Explicit override given** (`patch`/`minor`/`major`, or a literal `X.Y.Z`) — skip history parsing
entirely and go straight to confirmation with that target. (An explicit `patch`/`minor`/`major` only
makes sense once a baseline tag exists; on a first release it still proposes `v0.1.0` and says so.)

**Otherwise, derive the bump via `scripts/release-bump.sh`** (lode-ns3r) — this used to be an inline
shell snippet embedded directly in this file, which silently under-detected feat/fix commits due to
a leading-newline bug in how it split `git log`'s NUL-delimited record stream (only the newest
commit in the range was read correctly; see the script's own header comment for the full mechanism
and the concrete v1.2.0 case it produced, lode-905v). It is now an extracted, tested script — same
"ungated inline shell in a SKILL.md rots silently" lesson as `scripts/merge-precheck.sh`, lode-mh9g:

```bash
# Re-derive LATEST_TAG -- this is a fresh Bash invocation, separate from Section 1's; nothing
# carries over (docs/agents-workflow.md's "Guard against cross-block shell state" rule). Cheap and
# deterministic (a single git-tag lookup), so re-running it beats persisting it to a file here.
LATEST_TAG="$(scripts/release-latest-tag.sh)" || {
  echo "GATE COULD NOT RUN: scripts/release-latest-tag.sh failed" >&2
  exit 1
}
BUMP="$(scripts/release-bump.sh "${LATEST_TAG}..HEAD")" || {
  echo "GATE COULD NOT RUN: scripts/release-bump.sh failed on range ${LATEST_TAG}..HEAD" >&2
  # The script exits 2 for a machine fault (unresolvable range, git failure)
  # and never for a statement about the commits, so ANY non-zero here means
  # "no verdict was produced". Its stderr has already surfaced the detail
  # verbatim. Stop -- do NOT silently fall back to a guessed bump.
  exit 1
}
```

Precedence when several kinds of commits are present: **breaking > feat > fix**. Then bump the
latest tag's `MAJOR.MINOR.PATCH`:

- `breaking` — pre-1.0 (`MAJOR` is `0`) bumps **MINOR** (resets `PATCH` to 0), per `docs/release.md`
  §"Tag format"; once past `1.0.0` it bumps **MAJOR** (resets `MINOR`/`PATCH` to 0) — standard SemVer.
- `feat` — bumps **MINOR**, resets `PATCH` to 0.
- `fix` — bumps **PATCH**.
- `none` (**fallback**) — no commit since `$LATEST_TAG` carried a recognized prefix. Default the
  proposal to a **PATCH** bump rather than guessing higher, and say plainly that nothing matched so
  the human can confirm or override.

### 2a. Compile the release notes from the ticket record

The notes are **compiled, not composed**: I don't write freeform prose about the release — I gather
the resolved bd epics and tickets in the release window and turn them into an itemized, categorized
list of what was implemented. The confirmed list becomes the annotated tag's **body**
(`scripts/release.sh` keeps the `lode vX.Y.Z` subject), and `.github/workflows/release.yml`
publishes that body as the GitHub release notes.

1. **Collect the ticket IDs that landed in the window** — first-parent `trunk` history since the
   latest tag; the **whole history** when this is the first release:

   ```bash
   # Re-derive LATEST_TAG again -- another fresh Bash invocation, same reasoning as Section 2 above.
   LATEST_TAG="$(scripts/release-latest-tag.sh)" || {
     echo "GATE COULD NOT RUN: scripts/release-latest-tag.sh failed" >&2
     exit 1
   }
   if [ -n "$LATEST_TAG" ]; then RANGE="${LATEST_TAG}..HEAD"; else RANGE="HEAD"; fi
   git log --first-parent --format='%s' "$RANGE"
   ```

   A landed unit is identified by its subject: `Merge land/<id>: …` (a `/land` merge) or a trailing
   `(<id>)` marker on a direct-to-trunk commit. Dedupe — the same ticket can appear both ways.

2. **Resolve each ID in bd** — `bd show <id>` for title, type, parent epic, and status. Only
   **closed** tickets get a notes line; an ID that landed but is still open gets *flagged to the
   human* at confirmation instead — that's a bookkeeping smell, not a delivered feature. If bd
   doesn't know an ID at all, fall back to the merge subject as the line item rather than silently
   dropping landed work.

3. **Group and categorize** — suitable categorization means:
   - **Child tickets group under their parent epic**, epic title as the heading; check
     `bd list --status=closed --type=epic` for epics that closed in the window. An epic gets a
     one-line summary, then its landed children as sub-items — don't re-list children as
     top-level bullets.
   - **Standalone tickets** categorize by type: **Features**, **Fixes**, and **Internal /
     workflow** (tasks, CI, agent/skill machinery) — in that order.
   - One line per item: what it delivers, phrased for a reader of the release page (not a commit
     subject), with the ticket ID in parentheses.

4. **Write the notes to a fixed, re-derivable path** — body only, no `lode vX.Y.Z` heading, no
   version line (the script owns the tag subject). A **fixed** path under the repo's own `.git/`
   (not `$(mktemp)`, whose random path can't be re-derived by a later block — the same reasoning as
   `.claude/skills/land/SKILL.md`'s `$STATE_DIR`) so Section 4 can read it back without depending on
   this section's own shell state having survived:

   ```bash
   NOTES_FILE="$(git rev-parse --git-dir)/release-state/notes.md"
   mkdir -p "$(dirname "$NOTES_FILE")"
   # …write the itemized list into "$NOTES_FILE"…
   ```

An explicit `/release X.Y.Z` (or `patch|minor|major`) override skips the *version* derivation,
never this step — every release gets compiled notes.

### 3. Always confirm before touching anything

State the proposal plainly before doing anything else:

```
Latest tag: v<LATEST> (or: no tag yet — this is the first release)
Proposing:  v<PROPOSED>   (<breaking|feat|fix|none, and the reasoning — e.g. "2 feat commits since v0.3.1">)

Release notes (<N> resolved tickets, <M> epics since v<LATEST>):
  <the compiled notes body from §2a, verbatim>

<flags, if any: landed-but-still-open tickets, IDs unknown to bd>

Confirm v<PROPOSED>, or override: /release patch|minor|major|X.Y.Z
```

Confirming the version also confirms the notes — if the human wants a notes line reworded or
dropped, I apply that and re-show before proceeding. I do not proceed past this point without an
explicit go-ahead in the conversation — a bare `/release` never cuts a tag unattended. If the human
overrides with a bump word or a literal version, I recompute from that and re-confirm rather than
silently substituting it.

### 3a. The `.beads/issues.jsonl`-only dirty tree — discard, don't block

`scripts/release.sh` requires a clean tree, but the one modification that recurs constantly here is a
lone `M .beads/issues.jsonl` — the **passive beads export** (see CLAUDE.md: Dolt is authoritative, the
jsonl is export-only). It carries no release-relevant content and must never gate a release. When the
*only* dirty path is `.beads/issues.jsonl`, discard it and proceed — no need to ask:

```bash
git restore --staged .beads/issues.jsonl 2>/dev/null   # in case it's staged (first-column M)
git checkout -- .beads/issues.jsonl
git status --porcelain                                  # confirm now clean
```

If anything *other* than `.beads/issues.jsonl` is dirty, stop and surface it — that's a real tree the
operator needs to decide on, not a passive export to throw away.

### 4. On confirm, invoke the script — nothing else

```bash
# Re-derive NOTES_FILE (fixed path, Section 2a step 4 already wrote the content there) -- a fresh
# Bash invocation, nothing from Section 2a's shell survives. $PROPOSED is NOT a shell variable
# anywhere in this file: it is the version string Section 3 just confirmed IN CONVERSATION, with no
# preceding bash computing it (Section 2's scripts/release-bump.sh only classifies breaking/feat/
# fix/none, never the actual X.Y.Z arithmetic) -- fill in the literal confirmed version here, the
# same way a `<...>` template placeholder gets filled in elsewhere in this skill.
NOTES_FILE="$(git rev-parse --git-dir)/release-state/notes.md"
scripts/release.sh "$PROPOSED" "$NOTES_FILE"   # bare X.Y.Z, no leading 'v' — the script adds it
```

The notes file rides along as the second argument; the script embeds it as the annotated tag's
body (subject stays `lode vX.Y.Z`) and refuses a missing/empty file — so a confirmed release
always carries its compiled notes.

I do **not** run `nox` myself first, and I do **not** re-implement the clean-tree / on-`trunk` /
up-to-date-with-`origin/trunk` / tag-monotonicity checks — `scripts/release.sh` already gates all of
that (including the full `nox -s tests` run) before it tags and pushes. My job ends at handing it the
confirmed version string. If the script exits non-zero (dirty tree, stale `trunk`, failing tests,
non-monotonic version, tag already exists), I surface its exact error and stop — I do not retry with a
different version on its behalf; that is a decision for whoever is running the release.

## What I don't do

- I never tag, push, or run the test suite directly — that's entirely `scripts/release.sh`.
- I never write freeform release notes — the notes are compiled from the resolved bd ticket
  record (§2a), and anything that can't be traced to a ticket is flagged, not narrated.
- I never propose past a bare confirmation — no auto-release, ever, even when the derivation is
  obvious.
- I never touch a producer worktree, a bd ticket, or `trunk`'s merge queue — this is an operator
  utility over the release script, not a build task.
