#!/bin/bash -e
#
# Kick off a lode release: gate the tree, tag, and push the tag.
#
# Run from the repo root: scripts/release.sh X.Y.Z
#
# Guards that we're on trunk with a clean working tree, that vX.Y.Z doesn't
# already exist (locally or on origin), and that the full test suite
# (nox -s tests) is green; then creates an annotated tag on HEAD and pushes
# it to origin. Stops there — .github/workflows/release.yml (lode-0ru.3)
# owns the actual build+publish, triggered by the tag push. See
# docs/release.md for the full flow.

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

VERSION="${1:?usage: scripts/release.sh X.Y.Z}"
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

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  echo "release.sh: tag $TAG already exists locally" >&2
  exit 1
fi

if git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
  echo "release.sh: tag $TAG already exists on origin" >&2
  exit 1
fi

if [ ! -f ./venv/bin/activate ]; then
  echo "release.sh: ./venv not found — run scripts/python-init.sh first" >&2
  exit 1
fi
. ./venv/bin/activate
nox -s tests

git tag -a "$TAG" -m "lode $TAG"
git push origin "$TAG"

echo "release.sh: pushed $TAG — CI will build + publish the release"
