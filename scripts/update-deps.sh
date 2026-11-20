#!/bin/bash -e
#
# Controlled dependency update for lode's locked Python set (lode-g274.2 /
# lode-fdjr, moved onto uv by lode-s09w.4) -- the ONLY sanctioned way to move
# uv.lock. A human sees what is changing before anything installs.
#
# Run from the repo root:
#   scripts/update-deps.sh                    # re-resolve the WHOLE lock, gate, keep/rollback
#   scripts/update-deps.sh --dry-run          # print the version diff only; uv.lock restored
#   scripts/update-deps.sh --package NAME     # bump just NAME (+ whatever it drags with it)
#   scripts/update-deps.sh --package NAME --dry-run
#   scripts/update-deps.sh --no-file          # promote as usual but never file the churn stub
#
# What it does:
#   1. Save a copy of the committed uv.lock, then move it in place:
#      `uv lock --upgrade` (whole set) or `uv lock --upgrade-package NAME`
#      (only NAME and whatever it forces; every other pin stays put).
#   2. Print a readable VERSION DIFF between the saved copy and the moved
#      uv.lock -- one line per changed package, "pkg  OLD -> NEW" for a bump,
#      "+ pkg VERSION" for an addition, "- pkg  VERSION (removed)" -- derived
#      from the lock's own `[[package]]` name/version pairs, never from its
#      hash lines. This diff is the whole point of the script: it is the
#      artifact, not the install.
#   3. --dry-run restores the saved lock and stops, having changed nothing.
#      Otherwise the moved uv.lock STAYS on disk -- it is promoted BEFORE any
#      gating (lode-2zi9). Any test that reads the committed lock from disk
#      (a workflow-pin gate, a docs table) must see what the update is about
#      to change, not the pins it is about to replace. Then trash ./.venv and
#      rebuild it FRESH from that lock with `uv sync --frozen` (hash-verified
#      against uv.lock's recorded hashes -- docs/stack.md), never patched in
#      place.
#   4. Run the gates: nox -t fix, nox -s tests -- against the promoted lock.
#   5. GREEN -> nothing further to do to the lock; this script never commits
#      -- review (`git diff -- uv.lock`) and commit it yourself. ANY OTHER
#      FAILURE -- the install itself (hash-mismatched pin, yanked release,
#      network blip) just as much as a red nox gate -- prints the
#      paste-into-bd failure report FIRST, then RESTORES the committed lock
#      from the saved copy byte-for-byte, trashes whatever venv state exists,
#      and rebuilds clean from that restored lock. The report does not depend
#      on the restore or that rollback rebuild succeeding, and if the
#      rollback rebuild itself also fails, a loud warning after the report
#      says so and points at `uv sync --frozen` as the manual recovery.
#   6. On a green run only (never on --dry-run or a rolled-back run), file ONE
#      bd stub ticket carrying the VERSION DIFF as a durable work order for a
#      human/producer to read upstream changelogs and judge required-work vs.
#      judgment-call in the context of lode's actual call sites (lode-i642).
#      A WORK ORDER, not a finding: required-only filing policy, see the
#      stub's own acceptance text. Noise gate: skipped entirely when every
#      moved package changed only its patch component. The diff parsing, the
#      rendering and that gate live in the sourceable scripts/dep-churn-lib.sh
#      so they are unit-tested. `--no-file` suppresses filing outright. Filing
#      writes Dolt, so a missing/failing `bd` (or the `bd dolt push` after it)
#      only WARNS -- it never changes this script's exit status or the lock
#      (this script never mutates anything outside ./.venv and uv.lock).
#
# NO -x / TRASH-NOT-REPAIR -- deviates from the scripts/*.sh house style of
# `#!/bin/bash -ex`: `-e` alone still aborts on any unguarded failing command,
# but the genuinely risky steps -- installing the candidate and running the
# gates -- are each checked explicitly (`if ! rebuild_venv ...`) so a failure
# at ANY of them is caught and handled by THIS script, not left to `-e`
# tearing the process down mid cleanup with no report (lode-fdjr). `-x` is
# dropped because uv's own chatter and the gate output are already the useful
# signal.
#
# FAILURE HANDLING -- rollback is never "reverse a partial install": it is
# always restore the saved committed lock byte-for-byte, then `rm -rf ./.venv`
# + a clean rebuild from it. The failure report is built and printed BEFORE
# the lock is restored or the rollback rebuild is attempted, so a hiccup in
# either can never swallow it. The restore is ALSO armed on an EXIT trap
# (RESTORE_LOCK_ON_EXIT) for the whole window between moving the lock and
# deciding an outcome, so a Ctrl-C, a SIGTERM or an errexit abort mid-gate
# cannot leave an UNGATED candidate on disk that a human would read as a gated
# one. The one residual no trap can cover is a SIGKILL (or a power cut): there,
# `git checkout -- uv.lock` is the last-resort restore, since uv.lock is
# git-tracked where ./.venv is not.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

LOCK="uv.lock"

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

if ! command -v uv >/dev/null 2>&1; then
  echo "update-deps.sh: uv not found on PATH -- install uv (https://docs.astral.sh/uv/)" >&2
  exit 1
fi

SAVED_LOCK="$(mktemp)"
RESTORE_LOCK_ON_EXIT=0
cleanup() {
  if [ "$RESTORE_LOCK_ON_EXIT" -eq 1 ]; then
    cp -f "$SAVED_LOCK" "$LOCK"
    echo "update-deps.sh: died after moving the lock -- restored the committed $LOCK." >&2
  fi
  rm -f "$SAVED_LOCK"
}
trap cleanup EXIT
cp -f "$LOCK" "$SAVED_LOCK"

# From here until the outcome is decided an UNGATED lock sits on disk.
RESTORE_LOCK_ON_EXIT=1
# Checked explicitly: `bash scripts/update-deps.sh` drops the shebang's -e, and a
# failed resolve must not carry on to a "no change" diff and a green exit.
if [ -n "$PACKAGE" ]; then
  UPGRADE=(--upgrade-package "$PACKAGE")
else
  UPGRADE=(--upgrade)
fi
if ! uv lock -q "${UPGRADE[@]}"; then
  echo "update-deps.sh: uv lock failed (see output above) -- nothing was gated." >&2
  exit 1
fi

# Readable name/version diff. The parsing, the rendering and the stub's skip
# policy all live in the sourceable scripts/dep-churn-lib.sh so they are
# unit-tested (tests/test_dep_churn_lib.py) rather than trapped in this
# script's uninvokable middle. Both values below are assigned HERE, at top
# level -- see that library's CONTRACT note for why nothing there may return a
# value by setting a global.
# shellcheck source=dep-churn-lib.sh
. "$REPO/scripts/dep-churn-lib.sh"

CHANGES_RAW="$(dep_changes_raw "$SAVED_LOCK" "$LOCK")"
DIFF_TEXT="$(dep_version_diff_text "$LOCK" "$CHANGES_RAW")"
echo "$DIFF_TEXT"

if [ "$DRY_RUN" -eq 1 ]; then
  cp -f "$SAVED_LOCK" "$LOCK"
  RESTORE_LOCK_ON_EXIT=0
  exit 0
fi

# File ONE bd stub ticket carrying the VERSION DIFF as a durable work order
# (lode-i642) -- only called from the GREEN run path (step 6). Every
# failure mode here WARNS and returns 0: filing must never change this
# script's exit status or the lock outcome.
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

# Trash ./.venv and rebuild it FRESH from the on-disk uv.lock -- never patched
# in place. `uv sync --frozen` installs the lock as committed, verifying every
# artifact against the hashes uv.lock records (docs/stack.md). Callers check
# the return value explicitly (never as a bare statement); `&&` keeps the
# function's own status the FIRST failing step even though bash suspends -e
# inside a function called under `if !`.
rebuild_venv() {
  rm -rf ./.venv &&
    uv sync --frozen
}

echo "update-deps.sh: installing the candidate lock into a freshly rebuilt ./.venv..."
FAILED_AT=""
if ! rebuild_venv; then
  FAILED_AT="candidate install (rebuild_venv failed partway -- see output above)"
else
  echo "update-deps.sh: running gates (nox -t fix, nox -s tests)..."
  # --no-sync: rebuild_venv just synced ./.venv from this uv.lock; skip a redundant re-sync.
  if ! uv run --frozen --no-sync nox -t fix; then
    FAILED_AT="nox -t fix"
  elif ! uv run --frozen --no-sync nox -s tests; then
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
# rollback rebuild, and regardless of whether either succeeds.
REPORT="$(cat <<REPORT_EOF
=== update-deps.sh FAILURE REPORT (paste into a bd ticket) ===
Attempted update: $( [ -n "$PACKAGE" ] && echo "single package '$PACKAGE'" || echo "full lock upgrade" )
Failed at:         $FAILED_AT (see output above for the actual error)
Candidate diff that was attempted:
$DIFF_TEXT
Committed $LOCK:   restored to its pre-update pins.
=== end report ===
REPORT_EOF
)"
echo "$REPORT"

# Eager, not left to the trap: rebuild_venv installs from $LOCK, so the
# restore has to land before it or the rollback would reinstall the rejection.
cp -f "$SAVED_LOCK" "$LOCK"
RESTORE_LOCK_ON_EXIT=0
echo "update-deps.sh: trashing ./.venv and rebuilding clean from the restored $LOCK..." >&2
if ! rebuild_venv; then
  echo "update-deps.sh: WARNING -- the clean rollback rebuild from $LOCK ALSO failed." >&2
  echo "update-deps.sh: ./.venv may now be missing or broken. The report above is still" >&2
  echo "update-deps.sh: accurate ($LOCK was restored to its pre-update pins); re-run" >&2
  echo "update-deps.sh: uv sync --frozen by hand to restore ./.venv." >&2
fi

exit 1
