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
#
# What it does:
#   1. Recompile the lock from pyproject.toml into a temp file, via
#      `uv pip compile --generate-hashes`. A whole-set update re-resolves
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
    -h|--help)
      echo "usage: $0 [--dry-run] [--package NAME]" >&2
      exit 0
      ;;
    *)
      echo "update-deps.sh: unknown argument '$1'" >&2
      echo "usage: $0 [--dry-run] [--package NAME]" >&2
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
  uv pip compile pyproject.toml --generate-hashes --upgrade-package "$PACKAGE" -q -o "$CANDIDATE"
else
  # -q: uv pip compile otherwise echoes the ENTIRE compiled lock (every
  # package + every hash) to stdout in addition to writing -o -- exactly
  # the noise the VERSION DIFF below exists to replace.
  uv pip compile pyproject.toml --generate-hashes --upgrade -q -o "$CANDIDATE"
fi
# uv's autogenerated header comment records the literal -o PATH it was
# invoked with -- normalize the leaked tempfile path back to the real
# committed filename so promoting the candidate never bakes a throwaway
# /tmp path into requirements.lock's history.
sed -i "s|$CANDIDATE|$LOCK|" "$CANDIDATE"

# Readable name==version diff -- deliberately ignores the --hash lines;
# those are the whole reason this isn't `git diff`.
print_version_diff() {
  local old="$1" new="$2"
  local old_kv new_kv changes
  old_kv="$(mktemp)"
  new_kv="$(mktemp)"
  grep -oE '^[A-Za-z0-9_.+-]+==[A-Za-z0-9_.!+-]+' "$old" \
    | sed -E 's/==/\t/' | sort -t $'\t' -k1,1 -u > "$old_kv"
  grep -oE '^[A-Za-z0-9_.+-]+==[A-Za-z0-9_.!+-]+' "$new" \
    | sed -E 's/==/\t/' | sort -t $'\t' -k1,1 -u > "$new_kv"

  changes="$(join -a1 -a2 -e '(none)' -o 0,1.2,2.2 -t $'\t' "$old_kv" "$new_kv" \
    | awk -F'\t' '
        $2 == $3 { next }
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
