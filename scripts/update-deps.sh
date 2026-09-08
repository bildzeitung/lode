#!/bin/bash -e
#
# Controlled dependency update for lode's locked Python runtime set
# (lode-g274.2 / lode-fdjr) -- the ONLY sanctioned way to move
# requirements.lock. A human sees what is changing before anything installs.
#
# Run from the repo root:
#   scripts/update-deps.sh                    # recompile the WHOLE lock, gate, promote/rollback
#   scripts/update-deps.sh --dry-run          # print the version diff only, touch nothing
#   scripts/update-deps.sh --package NAME     # bump just NAME (+ whatever it drags with it)
#   scripts/update-deps.sh --package NAME --dry-run
#   scripts/update-deps.sh --no-file          # promote as usual but never file the churn stub
#
# What it does:
#   1. Recompile the lock from pyproject.toml into a temp file, via
#      scripts/compile-lock.sh (the SINGLE shared lock-gen command --
#      lode-sys4 -- which derives --python-version from .python-version so
#      this always resolves for the same interpreter CI targets, whatever
#      this machine's own default Python is). A whole-set update re-resolves
#      everything fresh (--upgrade). A --package update seeds the temp file
#      from the CURRENT committed lock first and passes --upgrade-package,
#      so uv only lets that one package (and anything it forces) move --
#      everything else stays pinned to what's already committed.
#   2. Print a readable VERSION DIFF against the committed lock -- names and
#      versions only, no hash noise: "pkg  OLD -> NEW" for a bump, "+ pkg
#      VERSION" for an addition, "- pkg  VERSION (removed)". This diff is
#      the whole point of the script: it is the artifact, not the install.
#   3. --dry-run stops here, having touched nothing. Otherwise: save a copy
#      of the currently-committed lock, then PROMOTE the candidate over
#      requirements.lock immediately -- BEFORE any gating (lode-2zi9). Any
#      test that reads the committed lock from disk (a workflow-pin gate, a
#      docs table, a future lock-vs-pyproject consistency check) must see
#      what the update is about to change, not the pins it is about to
#      replace; gating with the old lock still on disk is exactly the wrong
#      signal (see the ticket for the incident this fixes). Then trash
#      ./venv and rebuild it FRESH from that now-promoted lock (see NO -x /
#      TRASH-NOT-REPAIR below -- never patched in place).
#   4. Run the gates: nox -t fix, nox -s tests -- against the promoted lock.
#   5. GREEN (candidate installs AND both gates pass) -> nothing further to
#      do to the lock; it already holds the candidate. This script never
#      commits -- review (`git diff -- requirements.lock`) and commit it
#      yourself. ANY OTHER FAILURE -- the candidate install itself
#      (uninstallable / hash-mismatched pin, yanked release, network blip)
#      just as much as a red nox gate -- prints the paste-into-bd failure
#      report FIRST, then RESTORES the committed lock from the saved copy
#      byte-for-byte, trashes whatever venv state exists, and rebuilds
#      clean from that restored lock. The report does not depend on the
#      restore or that rollback rebuild succeeding (see FAILURE HANDLING
#      below), and if the rollback rebuild itself also fails, a loud
#      warning after the report says so and points at
#      scripts/python-init.sh as the manual recovery.
#   6. On a green run (step 5's GREEN path only -- never on --dry-run,
#      never on a failed/rolled-back run), file ONE bd stub ticket carrying the
#      VERSION DIFF as a durable work order for a human/producer to read
#      upstream changelogs and judge required-work vs. judgment-call in the
#      context of lode's actual call sites (lode-i642). This is a WORK
#      ORDER, not a finding: the script cannot itself judge required-vs-
#      decision, so the stub's own acceptance criteria delegate that
#      judgment (required-only: file follow-ups only for churn that
#      demonstrably breaks/degrades a lode call site; surface new
#      capabilities and judgment calls in the executor's hand-off instead --
#      lode-cai6 is the worked example of a judgment call that should NOT
#      have been auto-filed). Noise gate: filing is skipped entirely when
#      every moved package changed only its patch component (mechanically
#      decidable from the diff already computed) -- a ticket per run that is
#      usually noise gets ignored -- that gate, the diff parsing and the
#      rendering all live in the sourceable scripts/dep-churn-lib.sh so they
#      are unit-tested rather than trapped in this script's uninvokable
#      middle. `--no-file` suppresses filing outright;
#      `--dry-run` never reaches this step at all. Filing writes Dolt, so a
#      missing/failing `bd` (or the `bd dolt push` after it) only WARNS --
#      it never changes this script's exit status or the lock promotion
#      (this script still never commits anything outside ./venv and
#      requirements.lock).
#
# NO -x / TRASH-NOT-REPAIR -- deviates from the scripts/*.sh house style of
# `#!/bin/bash -ex`, per this ticket's own note that -ex may fight the
# rollback path. `-e` alone still aborts on any unguarded failing command,
# but the genuinely risky steps -- installing the candidate and running the
# gates -- are deliberately NOT run under bare `-e`: each is checked
# explicitly (`if ! rebuild_venv ...`, `if ! nox ...`) so a failure at ANY
# of those steps is caught and handled by THIS script, not left to `-e`
# tearing the process down mid cleanup with a half-migrated venv and no
# report (lode-fdjr -- this is exactly the defect that bounced the first
# attempt: the candidate install step was invoked as a bare statement,
# unguarded, so an install-time failure let errexit abort before rollback
# or reporting ever ran). `-x` is dropped because uv's own compile/install
# chatter and the gate output are already the useful signal -- xtrace would
# just bury it in line noise.
#
# FAILURE HANDLING -- rollback is never "reverse a partial install"
# (2026-07-19 user decision, lode-g274 notes): it is always restore the
# saved committed lock byte-for-byte, then `rm -rf ./venv` + a clean rebuild
# from that restored lock, so there is no half-migrated state to reason
# about *if the restore and rebuild succeed*. The failure report is built
# and printed BEFORE the lock is restored or that rollback rebuild is
# attempted (not after), so a hiccup during either step (e.g. a transient
# network failure on `pip install -U uv`) can never swallow the report --
# the two are independent by construction, not by ordering luck. The restore
# is ALSO armed on an EXIT trap (RESTORE_LOCK_ON_EXIT) for the window between
# the step-3 promotion and step 5 deciding an outcome, so a Ctrl-C, a SIGTERM
# or an errexit abort mid-gate cannot leave an UNGATED candidate on disk that a
# human would then read as a gated one -- the very failure this ordering exists
# to prevent. The one residual no trap can cover is a SIGKILL (or a power cut):
# there, note that $LOCK is git-tracked where ./venv is not, so `git checkout --
# requirements.lock` is the last-resort restore. $SAVED_LOCK rather than git is
# what the script itself uses, because the lock may legitimately be dirty when
# a run starts.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

LOCK="requirements.lock"

DRY_RUN=0
PACKAGE=""
NO_FILE=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --package)
      PACKAGE="${2:?--package requires a package name}"
      shift 2
      ;;
    --no-file)
      NO_FILE=1
      shift
      ;;
    -h|--help)
      echo "usage: $0 [--dry-run] [--package NAME] [--no-file]" >&2
      exit 0
      ;;
    *)
      echo "update-deps.sh: unknown argument '$1'" >&2
      echo "usage: $0 [--dry-run] [--package NAME] [--no-file]" >&2
      exit 1
      ;;
  esac
done

if [ ! -f ./venv/bin/activate ]; then
  echo "update-deps.sh: ./venv not found -- run scripts/python-init.sh first" >&2
  exit 1
fi
# shellcheck source=/dev/null
. ./venv/bin/activate   # just to put `uv` on PATH for the compile step below --
                        # the venv itself gets trashed and rebuilt from
                        # scratch before anything is gated (see rebuild_venv).

CANDIDATE="$(mktemp)"
SAVED_LOCK="$(mktemp)"
# The candidate is promoted over $LOCK BEFORE the gates run (lode-2zi9), so from
# that promotion until this script decides an outcome there is a window in which
# an UNGATED lock sits on disk looking exactly like a gated one. A death in that
# window -- Ctrl-C during the long `nox -s tests`, SIGTERM, an errexit abort --
# must not strand it, which is the very defect this ticket exists to kill. So the
# restore hangs off process EXIT, not off the handled-failure path alone;
# RESTORE_LOCK_ON_EXIT is cleared the moment the outcome IS decided (green: keep
# the candidate; failed: the explicit restore below already ran).
RESTORE_LOCK_ON_EXIT=0
cleanup() {
  if [ "$RESTORE_LOCK_ON_EXIT" -eq 1 ]; then
    cp -f "$SAVED_LOCK" "$LOCK"
    echo "update-deps.sh: died after promoting the candidate -- restored the committed $LOCK." >&2
  fi
  rm -f "$CANDIDATE" "$SAVED_LOCK"
}
trap cleanup EXIT
cp -f "$LOCK" "$SAVED_LOCK"

if [ -n "$PACKAGE" ]; then
  cp -f "$LOCK" "$CANDIDATE"   # seed with the committed lock so uv reuses every
                               # other package's pinned version as a preference
  "$REPO/scripts/compile-lock.sh" --upgrade-package "$PACKAGE" -q -o "$CANDIDATE"
else
  # -q: uv pip compile otherwise echoes the ENTIRE compiled lock (every
  # package + every hash) to stdout in addition to writing -o -- exactly
  # the noise the VERSION DIFF below exists to replace.
  "$REPO/scripts/compile-lock.sh" --upgrade -q -o "$CANDIDATE"
fi
# uv's autogenerated header comment records the literal -o PATH it was
# invoked with -- normalize the leaked tempfile path back to the real
# committed filename so promoting the candidate never bakes a throwaway
# /tmp path into requirements.lock's history.
sed -i "s|$CANDIDATE|$LOCK|" "$CANDIDATE"

# Readable name==version diff. The parsing, the rendering and the stub's skip
# policy all live in the sourceable scripts/dep-churn-lib.sh so they are
# unit-tested (tests/test_dep_churn_lib.py) rather than trapped in this
# script's uninvokable middle. Both values below are assigned HERE, at top
# level -- see that library's CONTRACT note for why nothing there may return a
# value by setting a global.
# shellcheck source=dep-churn-lib.sh
. "$REPO/scripts/dep-churn-lib.sh"

CHANGES_RAW="$(dep_changes_raw "$LOCK" "$CANDIDATE")"
DIFF_TEXT="$(dep_version_diff_text "$LOCK" "$CHANGES_RAW")"
echo "$DIFF_TEXT"

if [ "$DRY_RUN" -eq 1 ]; then
  exit 0
fi

# File ONE bd stub ticket carrying the VERSION DIFF as a durable work order
# (lode-i642) -- only called from the GREEN promote path (step 5). Every
# failure mode here WARNS and returns 0: filing must never change this
# script's exit status or the lock promotion outcome.
file_churn_stub() {
  local skip
  if skip="$(dep_stub_skip_reason "$NO_FILE" "$CHANGES_RAW")"; then
    echo "update-deps.sh: $skip"
    return 0
  fi
  if ! command -v bd >/dev/null 2>&1; then
    echo "update-deps.sh: WARNING -- bd not found; skipping churn-evaluation stub ticket." >&2
    return 0
  fi

  local title body acceptance new_id
  title="Evaluate dependency churn from update-deps.sh ($(date +%Y-%m-%d))"
  body="$(cat <<BODY_EOF
Dependency lock update landed via scripts/update-deps.sh. Evaluate the churn
below in the context of lode's actual call sites.

$DIFF_TEXT
BODY_EOF
)"
  acceptance="Required-only filing policy for THIS ticket's executor: read the upstream changelog for each moved package's crossed versions, then open a follow-up ticket ONLY for churn that demonstrably breaks or degrades an existing lode call site. Do NOT file tickets for new capabilities or other judgment calls -- surface those in your hand-off for a human instead."

  if ! new_id="$(bd create --title="$title" --description="$body" \
      --acceptance="$acceptance" --type=task --silent 2>&1)"; then
    echo "update-deps.sh: WARNING -- bd create failed; skipping churn-evaluation stub ticket. ($new_id)" >&2
    return 0
  fi
  echo "update-deps.sh: filed churn-evaluation stub ticket $new_id"

  if ! "$REPO/scripts/bd-dolt-push.sh" >/dev/null 2>&1; then
    echo "update-deps.sh: WARNING -- bd dolt push failed after filing $new_id; sync manually." >&2
  fi
  return 0
}

# shellcheck source=venv-install.sh
. "$REPO/scripts/venv-install.sh"

# Trash ./venv and rebuild it FRESH from $1 (a lock file path) -- never
# patched in place, so there is never a half-migrated venv to reason about
# ONCE THIS FUNCTION RETURNS SUCCESSFULLY. Every caller below checks its
# return value explicitly (never invoked as a bare statement) -- but that
# alone is NOT enough: when a function is called inside `if ! func; then`,
# bash suspends -e for the ENTIRE function body during that call (not just
# the call site), so without the explicit `&&` chaining below, an install
# step failing partway through (e.g. the hash-verified install) would be
# silently skipped past -- later lines in the function would still run,
# and the function would return the LAST command's (successful) exit
# status, masking the real failure instead of reporting it. Verified
# empirically while building this fix (a bare-statement chain here reached
# the "gates green" branch even with a deliberately-failing install step).
# Chaining with `&&` makes the function's own return code reflect the
# FIRST failing step regardless of the caller's -e state, independent of
# how the function happens to be invoked. install_locked_venv() (lode-02xy,
# scripts/venv-install.sh) carries the same guarantee for the actual install
# steps it performs -- this function chains that same way around it so the
# combined venv-creation-plus-install sequence stays one failure-transparent
# chain end to end.
rebuild_venv() {
  local lockfile="$1"
  deactivate 2>/dev/null || true
  # shellcheck source=/dev/null
  rm -rf ./venv &&
    python -m venv venv &&
    . ./venv/bin/activate &&
    install_locked_venv "$lockfile"
}

# Promote BEFORE gating (lode-2zi9) -- see step 3 and FAILURE HANDLING above.
# Everything from here to the verdict below runs with an UNGATED lock on disk.
cp -f "$CANDIDATE" "$LOCK"
RESTORE_LOCK_ON_EXIT=1

echo "update-deps.sh: installing the candidate lock into a freshly rebuilt ./venv..."
FAILED_AT=""
if ! rebuild_venv "$LOCK"; then
  FAILED_AT="candidate install (rebuild_venv failed partway -- see output above)"
else
  echo "update-deps.sh: running gates (nox -t fix, nox -s tests)..."
  if ! nox -t fix; then
    FAILED_AT="nox -t fix"
  elif ! nox -s tests; then
    FAILED_AT="nox -s tests"
  fi
fi

if [ -z "$FAILED_AT" ]; then
  RESTORE_LOCK_ON_EXIT=0
  echo "update-deps.sh: gates green -- $LOCK already holds the promoted candidate."
  echo "update-deps.sh: review and commit it yourself: git diff -- $LOCK"
  file_churn_stub
  exit 0
fi

echo "update-deps.sh: FAILED ($FAILED_AT) -- restoring the committed $LOCK." >&2

# Build and print the report BEFORE restoring the lock or attempting the
# rollback rebuild, and regardless of whether either succeeds -- see FAILURE
# HANDLING in the header. So the "restored" line below states what this script
# is about to do, not a result already confirmed.
REPORT="$(cat <<REPORT_EOF
=== update-deps.sh FAILURE REPORT (paste into a bd ticket) ===
Attempted update: $( [ -n "$PACKAGE" ] && echo "single package '$PACKAGE'" || echo "full lock recompile" )
Failed at:         $FAILED_AT (see output above for the actual error)
Candidate diff that was attempted:
$DIFF_TEXT
Committed $LOCK:   restored to its pre-update pins.
=== end report ===
REPORT_EOF
)"
echo "$REPORT"

# Eager, not left to the trap: rebuild_venv below installs from $LOCK, so the
# restore has to land before it or the rollback would reinstall the rejection.
cp -f "$SAVED_LOCK" "$LOCK"
RESTORE_LOCK_ON_EXIT=0
echo "update-deps.sh: trashing ./venv and rebuilding clean from the restored $LOCK..." >&2
if ! rebuild_venv "$LOCK"; then
  echo "update-deps.sh: WARNING -- the clean rollback rebuild from $LOCK ALSO failed." >&2
  echo "update-deps.sh: ./venv may now be missing or broken. The report above is still" >&2
  echo "update-deps.sh: accurate ($LOCK was restored to its pre-update pins); re-run" >&2
  echo "update-deps.sh: scripts/python-init.sh by hand to restore ./venv." >&2
fi

exit 1
