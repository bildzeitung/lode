# lode — Release process

How a version is derived, how a release is tagged, and how a kickoff turns a tag into a published
GitHub release. This is the design record for epic `lode-0ru`.

## Version source of truth

**The git tag *is* the version — there is no literal to hand-edit.** Versioning is wired through
[setuptools-scm](https://setuptools-scm.readthedocs.io/):

- `pyproject.toml` — `[build-system].requires` includes `setuptools-scm>=8`; `[project]` declares
  `dynamic = ["version"]` instead of a literal `version = "…"`; `[tool.setuptools_scm]` sets
  `version_file = "src/lode/_version.py"` (the build-generated file setuptools-scm writes the
  resolved version into — gitignored, never committed).
- `src/lode/__init__.py` resolves `__version__` via `importlib.metadata.version("lode")`, falling
  back to `"0.0.0+unknown"` (wrapped in `try/except PackageNotFoundError`) for a raw source tree
  that was never installed — not even editable.
- Because `requirements.txt` is a single `-e .[dev]` editable install, every dev checkout already
  has package metadata, so `lode version` always reports a real scm-derived version — something
  like `0.1.dev4+g<sha>` between releases, and the exact tag (e.g. `0.1.0`) on a release commit.

`lode version` (`src/lode/cli.py`) and its test (`tests/test_cli.py`) need no changes — both key
off `__version__`, which now resolves dynamically instead of a hardcoded string.

## Tag format

- **`vX.Y.Z`**, annotated (`git tag -a`, not lightweight) — the message carries provenance
  (who/when/why) and `git describe` prefers annotated tags.
- [SemVer](https://semver.org/): `MAJOR.MINOR.PATCH`. Pre-1.0 while the core loop
  (`docs/design.md` §7) stabilizes, so breaking changes bump `MINOR`, not `MAJOR`.
- **First release: `v0.1.0`.**

## Release flow (high level)

1. **Kickoff** — `scripts/release.sh` (lode-0ru.2) gates the working tree (clean, `nox -s tests`
   green — check-only; it never runs `nox -t fix`, which mutates the tree and would violate the
   clean-tree guard at tag time), computes/confirms the next `vX.Y.Z`, creates the annotated tag on
   the release commit, and pushes the tag.
2. **CI builds on tag push** — `.github/workflows/release.yml` (lode-0ru.3) triggers on `v*` tag
   push, does a clean-room `python -m build` (wheel + sdist; the `Version` metadata comes straight
   from `git describe` against the pushed tag), and publishes a GitHub release with both artifacts
   attached.
3. **Result** — `lode version` on an install of the released artifact reports the exact `X.Y.Z`
   tag, not `0.0.0`.

A `/release` Claude skill (lode-0ru.4) wraps step 1: it computes the next semver bump from the
commit history since the last tag and drives the kickoff.

**First-release ordering constraint:** GitHub Actions runs the workflow file *as it exists in the
tagged commit* — not whatever version of `release.yml` is on `trunk` at push time. That means
`.github/workflows/release.yml` (lode-0ru.3) **must already be merged to `trunk` before the first
`vX.Y.Z` tag is cut** by `scripts/release.sh`. Cut the tag first and the push triggers nothing —
silently, with no error — because the tagged commit predates the workflow file.

## Non-goals

- **No PyPI releases.** lode publishes a GitHub release with the wheel and sdist attached as
  downloadable artifacts — nothing is pushed to PyPI (confirmed by owner 2026-07-07). Don't assume
  `pip install lode` works; installing from a release means pulling the artifact off the GitHub
  release page (or installing from source).

## Why this shape

- **No version to forget to bump.** A hand-edited literal drifts from the tag it's supposed to
  match; scm-derived versioning makes the tag and the version the same fact.
- **CI owns the reproducible build**, not the local machine — the kickoff script's job is only to
  gate + tag + push; the workflow does the actual `python -m build` in a clean-room checkout so the
  published artifact isn't shaped by whatever happens to be in a developer's working tree.
- **Every dev checkout already reports a real version** (via the editable install), so "what version
  am I running" is answerable *before* the first tagged release, not just after.
