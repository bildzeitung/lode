---
name: release
description: Propose the next SemVer version from commit history since the latest vX.Y.Z tag, get it confirmed (or take an explicit override), and drive scripts/release.sh to cut the release. Thin wrapper — no build logic of its own; scripts/release.sh (lode-0ru.2) owns the actual gate + tag + push. Parses conventional-commit prefixes (feat -> minor, fix -> patch, `!`/BREAKING CHANGE -> major, but pre-1.0 a breaking change bumps MINOR per docs/release.md) and defaults to a PATCH proposal when no commit carries a recognized prefix. Examples — "/release", "/release patch", "/release minor", "/release major", "/release 0.2.0", "cut a release", "what's the next version".
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
hand-off here. I just propose a version, confirm it, and invoke the script.

## How to use me

- **`/release`** — derive the proposal from commit history since the latest tag (the common case).
- **`/release patch`** / **`/release minor`** / **`/release major`** — skip history parsing, bump the
  latest tag by the named part.
- **`/release X.Y.Z`** — skip everything, propose exactly that version (still confirmed, still gated
  by the script's own monotonicity check).

## What I do

### 1. Find the latest tag

Mirror `scripts/release.sh`'s own selection — the SemVer-greatest `vX.Y.Z` tag, not just the most
recently created one:

```bash
version_gt() {   # $1 > $2, both bare X.Y.Z — same comparison scripts/release.sh uses
  local IFS=.
  local -a a=($1) b=($2)
  for i in 0 1 2; do
    if [ "${a[i]}" -gt "${b[i]}" ]; then return 0; fi
    if [ "${a[i]}" -lt "${b[i]}" ]; then return 1; fi
  done
  return 1
}

LATEST_TAG=""
for t in $(git tag -l 'v*'); do
  tv="${t#v}"
  case "$tv" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) continue ;;
  esac
  if [ -z "$LATEST_TAG" ] || version_gt "$tv" "${LATEST_TAG#v}"; then
    LATEST_TAG="$t"
  fi
done
```

**No tag found** → this is the first release. Per `docs/release.md` the first release is pinned to
**`v0.1.0`** — propose that directly (no commit parsing needed) and skip to confirmation.

### 2. Derive the proposal

**Explicit override given** (`patch`/`minor`/`major`, or a literal `X.Y.Z`) — skip history parsing
entirely and go straight to confirmation with that target. (An explicit `patch`/`minor`/`major` only
makes sense once a baseline tag exists; on a first release it still proposes `v0.1.0` and says so.)

**Otherwise, parse conventional-commit subjects since `$LATEST_TAG`:**

```bash
BUMP="none"
while IFS= read -r -d '' MSG; do
  SUBJECT="$(printf '%s' "$MSG" | head -1)"
  if printf '%s' "$MSG" | grep -qE 'BREAKING[ -]CHANGE:' \
     || printf '%s' "$SUBJECT" | grep -qE '^[a-zA-Z]+(\([^)]*\))?!:'; then
    BUMP="breaking"; break                                    # highest priority, stop scanning
  elif printf '%s' "$SUBJECT" | grep -qE '^feat(\([^)]*\))?:' && [ "$BUMP" != "feat" ]; then
    BUMP="feat"
  elif printf '%s' "$SUBJECT" | grep -qE '^fix(\([^)]*\))?:' && [ "$BUMP" = "none" ]; then
    BUMP="fix"
  fi
done < <(git log "${LATEST_TAG}..HEAD" --format='%B%x00')
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

### 3. Always confirm before touching anything

State the proposal plainly before doing anything else:

```
Latest tag: v<LATEST> (or: no tag yet — this is the first release)
Proposing:  v<PROPOSED>   (<breaking|feat|fix|none, and the reasoning — e.g. "2 feat commits since v0.3.1">)

Commits since v<LATEST>:
  <one line per subject>

Confirm v<PROPOSED>, or override: /release patch|minor|major|X.Y.Z
```

I do not proceed past this point without an explicit go-ahead in the conversation — a bare `/release`
never cuts a tag unattended. If the human overrides with a bump word or a literal version, I recompute
from that and re-confirm rather than silently substituting it.

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
scripts/release.sh "$PROPOSED"     # bare X.Y.Z, no leading 'v' — the script adds it
```

I do **not** run `nox` myself first, and I do **not** re-implement the clean-tree / on-`trunk` /
up-to-date-with-`origin/trunk` / tag-monotonicity checks — `scripts/release.sh` already gates all of
that (including the full `nox -s tests` run) before it tags and pushes. My job ends at handing it the
confirmed version string. If the script exits non-zero (dirty tree, stale `trunk`, failing tests,
non-monotonic version, tag already exists), I surface its exact error and stop — I do not retry with a
different version on its behalf; that is a decision for whoever is running the release.

## What I don't do

- I never tag, push, or run the test suite directly — that's entirely `scripts/release.sh`.
- I never propose past a bare confirmation — no auto-release, ever, even when the derivation is
  obvious.
- I never touch a producer worktree, a bd ticket, or `trunk`'s merge queue — this is an operator
  utility over the release script, not a build task.
