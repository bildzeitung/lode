#!/bin/bash -e
#
# Kick off a lode release: gate the tree, tag, and push the tag.
#
# Run from the repo root: scripts/release.sh X.Y.Z [notes-file]
#
# Guards that we're on trunk with a clean working tree, that local trunk is
# up to date with origin/trunk, that X.Y.Z is well-formed SemVer strictly
# greater than the latest existing tag, that vX.Y.Z doesn't already exist
# (locally or on origin), and that the full test suite (nox -s tests) plus
# the packaging assertion (nox -s build, lode-zuqp) are green; then creates
# an annotated tag on HEAD and pushes it to origin.
# An optional notes-file becomes the tag BODY — the release notes that
# .github/workflows/release.yml publishes; the subject stays "lode vX.Y.Z"
# either way. Stops there — the workflow (lode-0ru.3) owns the actual
# build+publish, triggered by the tag push. See docs/release.md for the full
# flow.

# Resolve the notes file against the caller's cwd, before the cd below.
NOTES_FILE="${2:-}"
if [ -n "$NOTES_FILE" ]; then
  if [ ! -s "$NOTES_FILE" ]; then
    echo "release.sh: notes file '$NOTES_FILE' not found or empty" >&2
    exit 1
  fi
  NOTES_FILE="$(realpath "$NOTES_FILE")"
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

VERSION="${1:?usage: scripts/release.sh X.Y.Z [notes-file]}"
if ! echo "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "release.sh: version must be X.Y.Z (semver, no leading 'v'), got '$VERSION'" >&2
  exit 1
fi
TAG="v$VERSION"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "trunk" ]; then
  echo "release.sh: must be run on trunk (currently on '$BRANCH')" >&2
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "release.sh: working tree is dirty — commit or stash first" >&2
  exit 1
fi

git fetch origin --tags
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/trunk)" ]; then
  echo "release.sh: local trunk is not up to date with origin/trunk — pull/push before releasing" >&2
  exit 1
fi

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  echo "release.sh: tag $TAG already exists locally" >&2
  exit 1
fi

if git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
  echo "release.sh: tag $TAG already exists on origin" >&2
  exit 1
fi

# SemVer monotonicity: $1 > $2, both bare X.Y.Z (no leading 'v').
version_gt() {
  local -a a b
  IFS=. read -ra a <<< "$1"
  IFS=. read -ra b <<< "$2"
  for i in 0 1 2; do
    if [ "${a[i]}" -gt "${b[i]}" ]; then return 0; fi
    if [ "${a[i]}" -lt "${b[i]}" ]; then return 1; fi
  done
  return 1
}

LATEST=""
for t in $(git tag -l 'v*'); do
  tv="${t#v}"
  case "$tv" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) continue ;;
  esac
  if [ -z "$LATEST" ] || version_gt "$tv" "$LATEST"; then
    LATEST="$tv"
  fi
done
if [ -n "$LATEST" ] && ! version_gt "$VERSION" "$LATEST"; then
  echo "release.sh: $VERSION does not exceed latest existing tag v$LATEST" >&2
  exit 1
fi

if [ ! -f ./venv/bin/activate ]; then
  echo "release.sh: ./venv not found — run scripts/python-init.sh first" >&2
  exit 1
fi
. ./venv/bin/activate
nox -s tests
nox -s build

if [ -n "$NOTES_FILE" ]; then
  # --cleanup=whitespace: the default 'strip' deletes '#'-prefixed lines,
  # which would eat the notes' markdown headings.
  { echo "lode $TAG"; echo; cat "$NOTES_FILE"; } | git tag -a "$TAG" --cleanup=whitespace -F -
else
  git tag -a "$TAG" -m "lode $TAG"
fi
git push origin "$TAG"

echo "release.sh: pushed $TAG — CI will build + publish the release"
