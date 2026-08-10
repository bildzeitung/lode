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
#   3. --dry-run stops here, having touched nothing. Otherwise: trash
#      ./venv and rebuild it FRESH from the candidate lock (see NO -x /
#      TRASH-NOT-REPAIR below -- never patched in place).
#   4. Run the gates: nox -t fix, nox -s tests.
#   5. GREEN (candidate installs AND both gates pass) -> promote the
#      candidate over requirements.lock. This script never commits --
#      review (`git diff -- requirements.lock`) and commit it yourself.
#      ANY OTHER FAILURE -- the candidate install itself (uninstallable /
#      hash-mismatched pin, yanked release, network blip) just as much as a
#      red nox gate -- prints the paste-into-bd failure report FIRST, then
#      trashes whatever venv state exists and rebuilds clean from the
#      UNCHANGED committed requirements.lock. The report does not depend on
#      that rollback rebuild succeeding (see FAILURE HANDLING below): the
#      lock is always left untouched either way, and if the rollback
#      rebuild itself also fails, a loud warning after the report says so
#      and points at scripts/python-init.sh as the manual recovery.
#   6. On promote (step 5's GREEN path only -- never on --dry-run, never on
#      a failed/rolled-back run), file ONE bd stub ticket carrying the
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
#      usually noise gets ignored. `--no-file` suppresses filing outright;
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
# (2026-07-19 user decision, lode-g274 notes): it is always `rm -rf ./venv`
# + a clean rebuild from a known-good lock, so there is no half-migrated
# state to reason about *if the rebuild succeeds*. The failure report is
# built and printed BEFORE that rollback rebuild is attempted (not after),
# so a hiccup during the rollback rebuild itself (e.g. a transient network
# failure on `pip install -U uv`) can never swallow the report -- the two
# are independent by construction, not by ordering luck.

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
trap 'rm -f "$CANDIDATE"' EXIT

if [ -n "$PACKAGE" ]; then
  cp "$LOCK" "$CANDIDATE"   # seed with the committed lock so uv reuses every
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

# Readable name==version diff -- deliberately ignores the --hash lines;
# those are the whole reason this isn't `git diff`. Also stashes the raw,
# unformatted "$name\t$old\t$new" change lines (moved packages only, i.e.
# $old != $new) in the global CHANGES_RAW for the noise-gate check in
# all_patch_level_only() below -- computed once here so the filing step
# never has to re-derive the diff.
CHANGES_RAW=""
print_version_diff() {
  local old="$1" new="$2"
  local old_kv new_kv changes
  old_kv="$(mktemp)"
  new_kv="$(mktemp)"
  grep -oE '^[A-Za-z0-9_.+-]+==[A-Za-z0-9_.!+-]+' "$old" \
    | sed -E 's/==/\t/' | sort -t $'\t' -k1,1 -u > "$old_kv"
  grep -oE '^[A-Za-z0-9_.+-]+==[A-Za-z0-9_.!+-]+' "$new" \
    | sed -E 's/==/\t/' | sort -t $'\t' -k1,1 -u > "$new_kv"

  CHANGES_RAW="$(join -a1 -a2 -e '(none)' -o 0,1.2,2.2 -t $'\t' "$old_kv" "$new_kv" \
    | awk -F'\t' '$2 != $3')"

  changes="$(printf '%s\n' "$CHANGES_RAW" | awk -F'\t' '
        NF < 3 { next }
        $2 == "(none)" { printf "  + %-30s %s\n", $1, $3; next }
        $3 == "(none)" { printf "  - %-30s %s (removed)\n", $1, $2; next }
        { printf "    %-30s %s -> %s\n", $1, $2, $3 }
      ')"
  rm -f "$old_kv" "$new_kv"

  if [ -z "$changes" ]; then
    echo "VERSION DIFF: no change -- candidate lock is identical to $old"
  else
    printf 'VERSION DIFF (%s -> candidate):\n%s\n' "$old" "$changes"
  fi
}

DIFF_TEXT="$(print_version_diff "$LOCK" "$CANDIDATE")"
echo "$DIFF_TEXT"

if [ "$DRY_RUN" -eq 1 ]; then
  exit 0
fi

# Noise gate for the churn-evaluation stub (lode-i642): true iff every moved
# package in CHANGES_RAW changed ONLY its patch component (major.minor
# unchanged). An addition/removal ("(none)" on either side) or a version
# string that doesn't parse as major.minor.patch is conservatively treated
# as NOT patch-only, so it still triggers filing.
all_patch_level_only() {
  [ -n "$CHANGES_RAW" ] || return 1   # no changes at all -- nothing to gate
  local name old new old_mm new_mm
  while IFS=$'\t' read -r name old new; do
    [ -n "$name" ] || continue
    [ "$old" = "(none)" ] && return 1
    [ "$new" = "(none)" ] && return 1
    old_mm="$(echo "$old" | grep -oE '^[0-9]+\.[0-9]+')" || return 1
    new_mm="$(echo "$new" | grep -oE '^[0-9]+\.[0-9]+')" || return 1
    [ -n "$old_mm" ] && [ "$old_mm" = "$new_mm" ] || return 1
  done <<<"$CHANGES_RAW"
  return 0
}

# File ONE bd stub ticket carrying the VERSION DIFF as a durable work order
# (lode-i642) -- only called from the GREEN promote path (step 5). Every
# failure mode here WARNS and returns 0: filing must never change this
# script's exit status or the lock promotion outcome.
file_churn_stub() {
  if [ "$NO_FILE" -eq 1 ]; then
    echo "update-deps.sh: --no-file set -- skipping churn-evaluation stub ticket."
    return 0
  fi
  if [ -z "$CHANGES_RAW" ]; then
    echo "update-deps.sh: no version changes -- skipping churn-evaluation stub ticket."
    return 0
  fi
  if all_patch_level_only; then
    echo "update-deps.sh: every moved package is patch-level only -- skipping churn-evaluation stub ticket (noise gate)."
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

echo "update-deps.sh: installing the candidate lock into a freshly rebuilt ./venv..."
FAILED_AT=""
if ! rebuild_venv "$CANDIDATE"; then
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
  cp "$CANDIDATE" "$LOCK"
  echo "update-deps.sh: gates green -- promoted candidate to $LOCK."
  echo "update-deps.sh: review and commit it yourself: git diff -- $LOCK"
  file_churn_stub
  exit 0
fi

echo "update-deps.sh: FAILED ($FAILED_AT) -- discarding the candidate." >&2

# Build and print the report BEFORE attempting the rollback rebuild, and
# regardless of whether that rebuild succeeds -- see FAILURE HANDLING in
# the header. The committed lock is untouched either way; that line in the
# report is true even if the rebuild below also fails.
REPORT="$(cat <<REPORT_EOF
=== update-deps.sh FAILURE REPORT (paste into a bd ticket) ===
Attempted update: $( [ -n "$PACKAGE" ] && echo "single package '$PACKAGE'" || echo "full lock recompile" )
Failed at:         $FAILED_AT (see output above for the actual error)
Candidate diff that was attempted:
$DIFF_TEXT
Committed $LOCK:   unchanged -- nothing to revert.
=== end report ===
REPORT_EOF
)"
echo "$REPORT"

echo "update-deps.sh: trashing ./venv and rebuilding clean from the last-good $LOCK..." >&2
if ! rebuild_venv "$LOCK"; then
  echo "update-deps.sh: WARNING -- the clean rollback rebuild from $LOCK ALSO failed." >&2
  echo "update-deps.sh: ./venv may now be missing or broken. The report above is still" >&2
  echo "update-deps.sh: accurate (the committed $LOCK itself was never touched); re-run" >&2
  echo "update-deps.sh: scripts/python-init.sh by hand to restore ./venv." >&2
fi

exit 1
