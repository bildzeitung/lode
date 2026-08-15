#!/usr/bin/env bash
#
# The isolation-replay path: after a combined re-gate of /land's first-pass
# batch merge turns red, reset back to a known-green base and replay the
# accepted set one branch at a time -- gating after EACH merge -- so the
# culprit is found and bounced without taking every innocent branch down
# with it. (lode-s9xe.13)
#
# WHY THIS IS A SCRIPT, NOT A SECOND COPY OF land-merge-batch.sh's LOOP.
# /land's Section 3 has two loops with the identical merge-and-classify
# shape -- the first-pass batch merge (scripts/land-merge-batch.sh,
# lode-s9xe.4) and this isolation-replay copy -- fenced separately in
# .claude/skills/land/SKILL.md with a comment asking a human to "keep the two
# loops the same shape". That is an unenforced sync invariant over
# destructive code. This script drives its own copy of that shape (via the
# same scripts/land-merge-one.sh and scripts/drop-from-accepted.sh both
# loops already share), plus the per-branch gate and reset-on-red this loop
# alone performs -- so the two loops sharing their inner merge/classify
# helpers is what stays enforced, not the fenced markdown around them.
#
# Same two traps as land-merge-batch.sh, both learned the hard way in
# /land's own history -- see that script's header for the full account:
#
#   * `if CMD; then rc=0; else rc=$?; fi`, NOT `if ! CMD; then rc=$?; fi` --
#     the negated form's `$?` is the NEGATION's status, always 0, and would
#     read a machine-fault 2 as a clean merge.
#   * a branch that leaves the accepted set (real conflict, or bounced by a
#     failed gate) is written back to the FILE, not just this shell's view
#     of it, and takes every branch stacked on it with it
#     (drop-from-accepted.sh already owns that reduction).
#
# THIS SCRIPT DOES RUN GATES (nox -t fix / nox -s tests / nox -s
# lock_currency) -- unlike land-merge-batch.sh, which runs none. That is the
# entire reason the isolation-replay loop exists: a combined merge can be
# green with two branches each clean in isolation, so the only way to find
# the culprit is to gate after every single merge, on a checkout no other
# branch has touched. It also performs the two destructive resets this
# whole path is named for: `git reset --hard <base-ref>` once, up front, and
# `git reset --hard HEAD~1` per bounced branch.
#
# TWO MORE HAZARDS, both mid-loop and both found technically reviewing
# lode-s9xe.13, fixed under lode-lmu9:
#
#   * `nox -t fix` / `nox -s tests` are gated the SAME way `nox -s
#     lock_currency` already was: exit 1 is the only content verdict either
#     command has, via gate-lib.sh's `escalate_unless_content` -- a 127 (nox
#     not on PATH mid-run), 126, or 128+n (signal) is a machine fault and
#     stops the whole replay, never a bounce of the branch that happened to
#     be merged when it hit.
#   * a `nox -t fix` reformat on the LANDED path is folded into the merge
#     commit via `git commit --amend` before the loop continues, mirroring
#     SKILL.md Section 4's own reformat-commit step -- otherwise it leaves
#     the tree dirty for the NEXT iteration's `git merge`, which most likely
#     machine-faults against it (the BOUNCED path never surfaces this: `git
#     reset --hard HEAD~1` cleans it along with everything else).
#
# Usage:
#   scripts/land-replay.sh --accepted <file> --msg-dir <dir> \
#       --conflicts-dir <dir> --landed <file> [--graph <file>] \
#       [--token <token>] [--base-ref <ref>]
#
#   --accepted       the ordered accepted-set file (base before dependent --
#                     /land's Section 3a). Loaded with --require-nonempty:
#                     unlike the first-pass loop, an empty accepted set here
#                     is unreachable by construction (a nothing-merged pass
#                     skips the combined re-gate, and therefore this script,
#                     entirely -- see SKILL.md's Section 3 note) and is
#                     therefore always a fault, never a legitimate outcome
#                     (lode-0jan's asymmetry, preserved from the isolation
#                     loop's own SKILL.md prose).
#   --msg-dir        directory of precomputed merge messages, one file per id
#                     at <msg-dir>/<id> -- forwarded verbatim to
#                     land-merge-one.sh. Same directory the first-pass loop
#                     used; this script never writes to it.
#   --conflicts-dir  directory to write <conflicts-dir>/<id> into on a real
#                     conflict -- the conflicting paths land-merge-one.sh
#                     printed, persisted for a later, separate invocation
#                     (the needs-rebase kick-back note) to read back
#                     (lode-rfon's reasoning).
#   --landed         REQUIRED (unlike land-merge-batch.sh's optional flag of
#                     the same name): this loop's whole point is producing
#                     the durable record /land's Section 4 reads back from a
#                     later, separate invocation to decide what to close.
#                     Truncated by THIS script immediately after the reset
#                     below (which discards whatever the first-pass loop
#                     already recorded there) and BEFORE the baseline gates,
#                     so a baseline stop leaves behind no record claiming
#                     merges the reset just threw away.
#   --graph          scripts/stacked-graph.sh output, forwarded to
#                     drop-from-accepted.sh so a bounced or conflicting
#                     base's dependents are held too. Omit only when this
#                     pass has no stacked branches at all.
#   --token          this pass's land-lock token (lode-q9pm), forwarded
#                     verbatim to land-merge-one.sh. Omit to fall through to
#                     that script's blind heartbeat.
#   --base-ref       the ref to reset onto before replaying, and the ref the
#                     baseline gates below run against. Defaults to
#                     `origin/trunk` -- re-specialized from a generalized
#                     upstream default the same way scripts/stacked-graph.sh
#                     (lode-s9xe.2) re-specializes its own --base-ref, since
#                     lode's tracked default branch is `trunk`.
#
# BASELINE GATES, before attributing anything (lode-sys4, extended to `nox -s
# tests` by lode-kq4v, and to `nox -t fix` by lode-mps0). No gate this
# script attributes is a pure function of the tree -- an ambient FORCE_COLOR
# in the calling shell, a stale lock against today's PyPI, can turn a gate
# red with no branch involved at all. So every gate run below is baselined
# on bare --base-ref BEFORE the replay loop merges anything: if the baseline
# itself is red, nothing in --accepted caused it, and this script stops
# rather than blaming (and deleting) whichever branch happened to merge
# first. `nox -t fix` baselines on BOTH its exit code (red) and its effect
# on the tree (a reformat, possible even on exit 0) -- see the baseline
# block below and docs/decisions.md (search "lode-mps0") for why a dirty
# baseline reformat is gate-could-not-run, never committed invisibly or
# discarded.
#
# Output (stdout), one line per id processed, in accepted-set order:
#   LANDED\t<id>      merged AND gated clean; stays merged on the current
#                     checkout.
#   CONFLICT\t<id>    a real textual conflict against a branch already
#                     merged this replay. Left the accepted set.
#   BOUNCED\t<id>     merged cleanly but turned a gate red; backed out via
#                     `git reset --hard HEAD~1` and left the accepted set.
#                     Not a textual conflict -- the caller still owes this id
#                     a bounce (new rebuild ticket, drop the branch), same as
#                     any other gate failure.
#   HELD\t<id>        NOT processed -- removed from the accepted set as a
#                     dependent of a branch this call classified CONFLICT or
#                     BOUNCED (or an earlier HELD's own dependent, via
#                     drop-from-accepted.sh's transitive closure).
#
# Exit codes: 0 = the loop ran to completion (LANDED/CONFLICT/BOUNCED/HELD
# are all non-fault outcomes). 2 = machine fault: bad usage, a required file
# missing, the baseline gate itself red or faulting, or a called script
# failing in any way that is not its own documented content verdict. Per
# lode-9i2p's rule this is never read as a branch verdict, and processing
# stops immediately. There is no exit 1: like land-merge-batch.sh, a batch of
# multiple ids has no single verdict to report through the exit code -- read
# stdout.
set -uo pipefail   # deliberately NOT -e -- every branch below inspects an
                   # exit code by hand, same as land-merge-batch.sh.

# shellcheck source=gate-lib.sh
if ! . "$(dirname "$0")/gate-lib.sh" --no-advisory; then
  echo "GATE COULD NOT RUN: scripts/gate-lib.sh is missing or unreadable" >&2
  echo "next to $0 -- this is a machine/checkout fault, not a branch verdict." >&2
  exit 2
fi

SCRIPT_DIR="$(dirname "$0")"

# Main-checkout identity (lode-1nty's pattern, same as land-merge-one.sh):
# every destructive git call below (`git reset --hard`) is cwd-resolved, with
# no `-C`/`--git-dir` of its own pinning it to a specific checkout. This
# script asserts its own main-checkout identity as its first real action so
# no caller needs to fence it separately -- a caller with cwd-resolved
# mutations of its OWN still needs its own guard, but this script's resets
# are covered here.
if [ ! -x "$SCRIPT_DIR/assert-main-checkout.sh" ]; then
  gate_could_not_run \
    "scripts/assert-main-checkout.sh is missing or not executable next to $0." \
    "This is a bootstrap/checkout fault -- the guard could not run at all, which" \
    "is NOT a verdict that cwd is the wrong checkout, and never a branch conflict."
fi
if ! "$SCRIPT_DIR/assert-main-checkout.sh"; then
  gate_could_not_run \
    "not running in lode's main checkout (see the diagnostic above)." \
    "scripts/assert-main-checkout.sh refused -- every git call in this" \
    "script is cwd-resolved with no -C of its own, so this is a" \
    "machine/dispatch fault, never a branch conflict."
fi

ACCEPTED=""
MSG_DIR=""
CONFLICTS_DIR=""
LANDED_FILE=""
GRAPH=""
TOKEN=""
BASE_REF="origin/trunk"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --accepted)       shift; [ "$#" -gt 0 ] || gate_could_not_run "--accepted needs a value"; ACCEPTED="$1" ;;
    --msg-dir)        shift; [ "$#" -gt 0 ] || gate_could_not_run "--msg-dir needs a value"; MSG_DIR="$1" ;;
    --conflicts-dir)  shift; [ "$#" -gt 0 ] || gate_could_not_run "--conflicts-dir needs a value"; CONFLICTS_DIR="$1" ;;
    --landed)         shift; [ "$#" -gt 0 ] || gate_could_not_run "--landed needs a value"; LANDED_FILE="$1" ;;
    --graph)          shift; [ "$#" -gt 0 ] || gate_could_not_run "--graph needs a value"; GRAPH="$1" ;;
    --token)          shift; [ "$#" -gt 0 ] || gate_could_not_run "--token needs a value"; TOKEN="$1" ;;
    --base-ref)       shift; [ "$#" -gt 0 ] || gate_could_not_run "--base-ref needs a value"; BASE_REF="$1" ;;
    *)                gate_could_not_run "unknown argument '$1'" \
                        "usage: land-replay.sh --accepted <file> --msg-dir <dir> --conflicts-dir <dir>" \
                        "  --landed <file> [--graph <file>] [--token <token>] [--base-ref <ref>]" ;;
  esac
  shift
done

[ -n "$ACCEPTED" ]      || gate_could_not_run "--accepted is required"
[ -n "$MSG_DIR" ]       || gate_could_not_run "--msg-dir is required"
[ -n "$CONFLICTS_DIR" ] || gate_could_not_run "--conflicts-dir is required"
[ -n "$LANDED_FILE" ]   || gate_could_not_run "--landed is required"
[ -d "$MSG_DIR" ]       || gate_could_not_run "--msg-dir '$MSG_DIR' does not exist"
[ -d "$CONFLICTS_DIR" ] || gate_could_not_run "--conflicts-dir '$CONFLICTS_DIR' does not exist"
if [ -n "$GRAPH" ] && [ ! -f "$GRAPH" ]; then
  gate_could_not_run "graph file '$GRAPH' does not exist" \
    "Pass the file scripts/stacked-graph.sh wrote, or omit --graph only if this pass has no stacks."
fi

# Beads-exclusion pathspecs for both dirty-tree checks below (baseline
# reformat-detect, per-branch reformat-detect), read from the canonical list
# rather than the hardcoded ':!.beads' this replaced (lode-3cda). Same idiom
# as scripts/worktree-gc-classify.sh's own load of the same list: built
# ONCE here, fail-loud if unreadable or empty. Unlike the old ':!.beads',
# this excludes only the two listed jsonl relpaths -- a real non-passive
# .beads/ change (e.g. config.yaml) now counts as dirty, matching every other
# consumer of scripts/beads-passive-exports.txt.
#
# lode-xlcm: the load+validate+":(exclude)" transform itself is owned by the
# sourced helper scripts/beads-passive-exports.sh (this script keeps its own
# gate_could_not_run failure semantics on top of the helper's plain return code).
# shellcheck source=beads-passive-exports.sh
if ! . "$SCRIPT_DIR/beads-passive-exports.sh"; then
  gate_could_not_run "cannot source $SCRIPT_DIR/beads-passive-exports.sh" \
    "Both dirty-tree checks below cannot know which beads paths to exclude without it."
fi
if ! load_beads_passive_exports "$SCRIPT_DIR/beads-passive-exports.txt"; then
  gate_could_not_run "could not load $SCRIPT_DIR/beads-passive-exports.txt" \
    "Both dirty-tree checks below cannot know which beads paths to exclude without it."
fi

# Missing -> fatal (Section 3a's precompute never ran). Empty -> ALSO fatal
# here, unlike land-merge-batch.sh's own load of the same file: this script
# only runs after a combined re-gate turned red, and a nothing-merged pass
# skips that re-gate (and therefore this script) entirely, so an empty
# accepted set at this point is unreachable by construction and always a
# fault (lode-0jan's asymmetry).
ACCEPTED_IDS=$("$SCRIPT_DIR/land-state-load.sh" "$ACCEPTED" --require-nonempty -- \
  "land-replay.sh: isolation-replay path -- nothing to attribute a red combined re-gate to.") \
  || gate_could_not_run "could not read --accepted '$ACCEPTED' (see land-state-load.sh's own diagnostic above)"

# -q: this script's stdout is the caller's LANDED/CONFLICT/BOUNCED/HELD
# channel (same contract as land-merge-one.sh's $CONFLICTS) and must stay
# clean of git's own "HEAD is now at ..." chatter.
# Fail CLOSED: --base-ref is caller-supplied, and with no `set -e` a failed
# reset would fall straight through into the baseline gates and the replay
# loop -- gating, attributing and bouncing branches against whatever the
# first-pass loop happened to leave merged, which is the one tree this whole
# path exists to discard.
git reset --hard -q "$BASE_REF" \
  || gate_could_not_run "'git reset --hard $BASE_REF' failed (see git's own error above)." \
       "Everything below assumes the tree IS '$BASE_REF' -- most likely --base-ref names a ref" \
       "this checkout cannot resolve. Nothing merged, nothing attributed."

# The reset above discarded whatever the first-pass loop already recorded in
# --landed; start THIS replay's record from empty so the caller closes only
# what this loop actually keeps merged. Done HERE -- immediately after the
# reset, BEFORE the baseline gates -- so that a baseline stop cannot leave a
# durable record naming merges the reset has just thrown away.
: > "$LANDED_FILE" \
  || gate_could_not_run "could not truncate --landed '$LANDED_FILE'" \
       "Leaving it would claim first-pass merges the reset above has discarded."

# Baseline every gate this loop attributes, on the reset tree, BEFORE
# touching anything (see the file header). A baseline failure is never a
# branch's fault: stop here, land nothing from this replay.
#
# `nox -t fix` first (mirrors the per-branch gate's own ordering below), then
# `nox -s tests` (lode-mps0, extending lode-sys4/lode-kq4v's baseline
# coverage to the one attributing gate that had never been baselined).
# `nox -t fix` needs TWO checks, not one: noxfile.py's `fix` session runs
# `ruff format .` UNCONDITIONALLY before `ruff check --fix .`, so it can
# leave the bare base tree dirty (a reformat) even when it exits 0 -- the
# exit code alone would miss exactly the reformat-only case. Both arms
# `gate_could_not_run`: this loop neither commits a base-ref reformat
# invisibly under no branch's name nor discards one via `git reset --hard`,
# and the reformat is left IN the working tree for the human to commit to
# '$BASE_REF' directly. Rejected alternatives: docs/decisions.md (search
# "lode-mps0"). No `escalate_unless_content` partition here -- like the
# `nox -s tests` baseline arm below, every nonzero stops the pass, so there
# is no mid-loop rc to split into content-vs-machine.
if ! nox -t fix; then
  gate_could_not_run "'nox -t fix' is red on bare '$BASE_REF', before any branch merged." \
    "Not attributable to anything in --accepted. '$BASE_REF' itself needs a human's fix" \
    "(see lode-mps0's decision in docs/decisions.md)."
fi
# A plain string, not the NUL-read array the per-branch reformat step below
# builds: nothing is staged here, so the paths are only ever a diagnostic.
# The pathspec itself stays byte-identical to that site's.
fix_reformat_paths=$(git diff --name-only -- . "${BEADS_PASSIVE_EXPORTS_EXCLUDE_PATHSPECS[@]}")
if [ -n "$fix_reformat_paths" ]; then
  gate_could_not_run "'nox -t fix' reformatted the bare base tree at '$BASE_REF' (exit 0, but" \
    "the tree is now dirty: $(printf '%s' "$fix_reformat_paths" | tr '\n' ' '))." \
    "Not attributable to anything in --accepted -- '$BASE_REF' genuinely needs this reformat," \
    "but this loop must not land it invisibly under no branch's name, nor discard it via" \
    "'git reset --hard'. The reformat is left in the working tree -- a human should commit it" \
    "directly to '$BASE_REF' (see lode-mps0's decision in docs/decisions.md)."
fi

if ! nox -s tests; then
  gate_could_not_run "'nox -s tests' is red on bare '$BASE_REF', before any branch merged." \
    "Not attributable to anything in --accepted. Check the calling shell's own" \
    "environment (FORCE_COLOR / NO_COLOR / TTY_COMPATIBLE / TTY_INTERACTIVE, lode-kq4v)" \
    "before assuming this is a genuine regression on '$BASE_REF' itself."
fi
lc_rc=0
nox -s lock_currency || lc_rc=$?
case "$lc_rc" in
  0) : ;;
  2) gate_could_not_run "'nox -s lock_currency' machine-faulted (exit 2) on bare '$BASE_REF'." \
       "This is a machine fault (lode-jhry's 0/1/2 contract), never a branch verdict --" \
       "surface it as-is, never as a bounce." ;;
  *) gate_could_not_run "'nox -s lock_currency' is red (exit $lc_rc) on bare '$BASE_REF', before any branch merged." \
       "Not attributable to anything in --accepted -- trunk's own lock is stale." ;;
esac

# One copy of the drop-and-report-HELD step, called from all three arms that
# take a branch out of the accepted set (CONFLICT, and both BOUNCED arms).
# land-merge-batch.sh inlines the equivalent block once; this loop needs it
# three times, and three in-file copies of a step that rewrites the accepted
# set is the same unenforced-sync hazard this script's header objects to.
drop_and_hold() {   # $1 = id already reported CONFLICT/BOUNCED on stdout
  local drop_out verb held_id
  drop_out=$("$SCRIPT_DIR/drop-from-accepted.sh" "$1" --accepted "$ACCEPTED" \
    ${GRAPH:+--graph "$GRAPH"}) \
    || gate_could_not_run "drop-from-accepted.sh faulted dropping '$1' (see its own diagnostic above)"
  # A here-string, not `printf ... | while`: a pipeline would run the loop in a
  # subshell for no gain.
  while IFS=$'\t' read -r verb held_id; do
    [ "$verb" = "HELD" ] || continue
    printf 'HELD\t%s\n' "$held_id"
  done <<< "$drop_out"
}

# Back the just-merged culprit out and report it. NOT a textual conflict --
# its content merged fine; a gate judged it bad.
bounce() {   # $1 = id, merged at HEAD, whose gate turned red
  git reset --hard -q HEAD~1 \
    || gate_could_not_run "'git reset --hard HEAD~1' failed backing '$1' out." \
         "The tree still carries this branch's merge; continuing would attribute the next" \
         "branch's gates to a tree that includes content this loop has already rejected."
  printf 'BOUNCED\t%s\n' "$1"
  drop_and_hold "$1"
}

for id in $ACCEPTED_IDS; do
  # A branch already HELD/CONFLICT/BOUNCED-dropped by an earlier iteration
  # this same call may still appear in $ACCEPTED_IDS (captured before the
  # loop started) -- re-check membership against the file, same idiom as
  # land-merge-batch.sh's own loop, for the same reason.
  grep -qxF "$id" "$ACCEPTED"
  grc=$?
  case "$grc" in
    0) ;;
    1) continue ;;
    *) gate_could_not_run "grep failed (exit $grc) re-checking '$id' in '$ACCEPTED'" \
         "Reading this as 'already dropped' would silently skip the rest of the replay." ;;
  esac

  # Same idiom as land-merge-batch.sh's loop, for the same reason: `if !
  # CMD; then rc=$?` would capture the negation's status (always 0 in that
  # arm), silently reading a machine-fault 2 as a clean merge.
  if CONFLICTS=$("$SCRIPT_DIR/land-merge-one.sh" "$id" "$MSG_DIR" "$TOKEN"); then
    rc=0
  else
    rc=$?
  fi

  case "$rc" in
    0) : ;;   # merged -- gate it below before deciding LANDED vs. BOUNCED
    1)
      # A real textual conflict against a branch already merged this
      # replay. Persist the conflicting paths now, while this loop actually
      # holds them (lode-rfon), then drop it and its dependents.
      printf '%s\n' "$CONFLICTS" > "$CONFLICTS_DIR/$id"
      printf 'CONFLICT\t%s\n' "$id"
      drop_and_hold "$id"
      continue
      ;;
    *)
      # MACHINE FAULT -- land-merge-one.sh's own exit 2, or any other
      # nonzero (127/126 missing/non-executable next to this script, 128+n
      # on a signal). CONFLICT (1) is the ONE code that is a branch verdict;
      # everything else is the machine (lode-9i2p). Stop rather than guess
      # at the fate of ids not yet reached.
      gate_could_not_run "land-merge-one.sh failed (exit $rc) on '$id'" \
        "Exit 1 is the only conflict verdict; $rc is a machine/bootstrap fault" \
        "(see land-merge-one.sh's own diagnostic above, if it ran at all)." \
        "Do NOT bounce '$id' on the strength of this."
      ;;
  esac

  # Merged cleanly -- gate it, on THIS checkout alone, before deciding its
  # fate. `nox -t fix` first (may reformat what was just merged), then `nox
  # -s tests`. Each is checked separately, never via `if ! CMD_A || ! CMD_B`
  # (that idiom collapses BOTH commands' exit codes into one boolean and
  # cannot tell a genuine content failure (exit 1) from a non-verdict
  # mid-loop fault -- 127 (not on PATH), 126, or 128+n (signal) -- which
  # would otherwise BOUNCE an innocent branch (lode-lmu9). Same
  # `escalate_unless_content` partition gate-lib.sh already gives
  # `nox -s lock_currency` two paragraphs below: exit 1 is the only content
  # verdict either of these two commands has; anything else is a machine
  # fault and stops the whole replay (lode-9i2p), exactly like the baseline
  # gates above and `nox -s lock_currency`'s own mid-loop exit-2 arm. The
  # success arm is a bare `:` and the bounce lives INSIDE the else arm, on
  # the far side of `escalate_unless_content` -- which only ever returns on
  # the content verdict (exit 1), so a second `if [ "$rc" -ne 0 ]` after the
  # `fi` could never read anything but "bounce" (dead state). `rc=$?` must
  # still be the FIRST command in the else arm, and the condition must stay
  # un-negated: `if ! CMD; then rc=$?` captures the NEGATION's status.
  if nox -t fix; then
    :
  else
    fix_rc=$?
    escalate_unless_content "$fix_rc" \
      "'nox -t fix' failed with exit $fix_rc after merging '$id'." \
      "Exit 1 is the only content verdict (lode-9i2p); a 127/126/signal here is a" \
      "machine fault, not '$id''s verdict -- do NOT bounce it on the strength of this."
    bounce "$id"
    continue
  fi

  if nox -s tests; then
    :
  else
    tests_rc=$?
    escalate_unless_content "$tests_rc" \
      "'nox -s tests' failed with exit $tests_rc after merging '$id'." \
      "Exit 1 is the only content verdict (lode-9i2p); a 127/126/signal here is a" \
      "machine fault, not '$id''s verdict -- do NOT bounce it on the strength of this."
    bounce "$id"
    continue
  fi

  # `nox -t fix` may have reformatted the just-merged content, leaving the
  # working tree dirty. Fold that reformat INTO the merge commit (`--amend`,
  # not a separate commit) so a later bounce's single `git reset --hard
  # HEAD~1` discards both together, and so the tree handed to the NEXT
  # iteration's `git merge` (inside land-merge-one.sh) is clean -- a dirty
  # tree there most likely machine-faults that merge (lode-lmu9). Mirrors
  # SKILL.md Section 4's own reformat-commit step: stage only the explicit
  # paths `git diff` names, never `-A` (CLAUDE.md's workflow gotchas), and
  # skip the commit entirely when nothing changed.
  reformat_paths=()
  while IFS= read -r -d '' path; do
    reformat_paths+=("$path")
  done < <(git diff -z --name-only -- . "${BEADS_PASSIVE_EXPORTS_EXCLUDE_PATHSPECS[@]}")
  if [ "${#reformat_paths[@]}" -gt 0 ]; then
    git add -- "${reformat_paths[@]}" \
      || gate_could_not_run "could not stage nox -t fix's reformat of '$id' (${reformat_paths[*]})" \
           "The merge itself succeeded; only staging the reformat failed."
    git commit --no-verify -q --amend --no-edit \
      || gate_could_not_run "could not amend '$id''s merge commit with nox -t fix's reformat" \
           "The tree now carries an uncommitted reformat on top of '$id''s merge -- the next" \
           "iteration's merge would most likely fault against it."
  fi

  lc_rc=0
  nox -s lock_currency || lc_rc=$?
  case "$lc_rc" in
    0)
      printf 'LANDED\t%s\n' "$id"
      printf '%s\n' "$id" >> "$LANDED_FILE" \
        || gate_could_not_run "could not append '$id' to --landed '$LANDED_FILE'" \
             "This id IS merged onto the current checkout; the record of it is not."
      ;;
    2)
      # Machine fault MID-LOOP, not this branch's fault (lode-jhry): stop
      # the whole replay here rather than bounce an innocent id.
      gate_could_not_run "'nox -s lock_currency' machine-faulted (exit 2) after merging '$id'." \
        "This is a machine fault, never a branch verdict -- do not bounce '$id' on it."
      ;;
    *)
      bounce "$id"
      ;;
  esac
done

exit 0
