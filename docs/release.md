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
   the release commit — with the confirmed release notes as the tag body, when a notes file is
   passed as its second argument — and pushes the tag. It also runs `nox -s build` as a local
   fail-fast check (rationale below).
2. **CI builds on tag push** — `.github/workflows/release.yml` (lode-0ru.3) triggers on `v*` tag
   push and runs `nox -s build -- dist` (wheel + sdist into `./dist`; the `Version` metadata comes
   straight from `git describe` against the pushed tag), then publishes a GitHub release with both
   artifacts attached and the tag body as the release notes.
3. **Result** — `lode version` on an install of the released artifact reports the exact `X.Y.Z`
   tag, not `0.0.0`.

A `/release` Claude skill (lode-0ru.4) wraps step 1: it computes the next semver bump from the
commit history since the last tag, compiles the release notes from the resolved ticket record
(below), and drives the kickoff.

## CI workflow trigger scope (push and pull_request)

Sibling to the tag-triggered flow above: `.github/workflows/build.yml` (lode-qxdn.1) and every
push/PR-triggered CI workflow that follows it — the test suite leg (lode-qxdn.2), coverage
(lode-qxdn.3) — narrow their `push:` trigger to:

```yaml
on:
  push:
    branches: [trunk, "land/**"]
  pull_request:
```

- **`land/**` is deliberate, not incidental.** This repo never opens PRs — producers push
  `land/<id>` directly (CLAUDE.md #8 bars Claude from filing PRs under the maintainer's identity) —
  so `pull_request:` is very nearly dead code here, and the push trigger on `land/**` is the *only*
  pre-trunk CI signal a human gets. Dropping it would make these checks purely post-hoc (trunk-only).
- **No `tags-ignore` line.** Defining only `branches:` on a `push` trigger already excludes tag
  pushes — GitHub scopes the event to the ref types the filter names, so a `v*` tag push does not
  trigger the workflow at all. A `tags-ignore` entry on top would be inert clutter. **This rests on
  GitHub's documented ref-type scoping, NOT on an observation** — `build.yml` landed (083326a,
  2026-07-19) *after* the most recent tag (`v1.0.0`, 2026-07-18), so no `v*` tag has been pushed
  while this workflow has existed and the behavior has never actually been exercised here. Confirm
  it at the next real `v*` tag push (expect: a `release` run and no `build` run); until then treat
  the tag-exclusion as documented-but-unobserved.
- **This matters far more for the heavier siblings than for `build.yml` itself.** Cost is $0 (public
  repo, free Actions minutes) — this is noise/latency, not spend — and `build.yml`'s own job is cheap
  (`pip install build nox` + `nox -s build`, ~4s, zero runtime deps). But `nox -s tests`
  (lode-qxdn.2) and coverage
  (lode-qxdn.3) are heavy: a full `-e .[dev]` install (lancedb/fastembed/textual) plus a
  FastEmbedCrossEncoder model download, minutes per run. An unfiltered `push:` there would burn real
  wall-clock on every producer push — coding handoff, code-reviewer re-push, every rebase pickup —
  measured at ~2-3 pushes per landed ticket. Both MUST reuse this same narrowed trigger rather than
  reintroducing an unfiltered one.
- **Nothing in the landing loop reads these check results.** `/land` gates on its own merge
  precheck, `land-review`, and a local `nox` re-gate; it never queries GitHub Actions. These
  workflows are advisory-to-a-human only, which is the other reason to fire them only where a human
  is likely to look (trunk, or a `land/<id>` branch mid-review) rather than on every internal ref.
- **Dolt sync traffic (`bd dolt push`) never triggers these workflows at all**, so no exclusion is
  needed for it. `refs/dolt/data` is neither `refs/heads/*` nor `refs/tags/*`, and GitHub's `push`
  event only fires for those two ref namespaces (confirmed: several `bd dolt push`es after
  lode-qxdn.1 landed produced zero runs).
- **Badge behavior (lode-qxdn.4) — pin the branch explicitly.** A red `land/<id>` build must never
  redden the README while `trunk` is green. Two mechanisms avoid that, and this repo deliberately
  uses the second:
  - GitHub's native badge (`.../workflows/<file>/badge.svg`) tracks the most recent run on the
    **default branch** by default (confirmed against GitHub's own docs), but falls back to the most
    recent run overall if the workflow has *never* run on the default branch — so its safety is
    conditional on a prior `trunk` run existing.
  - **The convention here:** the shields.io endpoint with an explicit branch pin —
    `https://img.shields.io/github/actions/workflow/status/<owner>/<repo>/<file>.yml?branch=trunk`.
    Naming the branch in the URL makes the guarantee unconditional rather than dependent on a
    fallback rule. This is what the README's build badge uses; **lode-qxdn.5 (tests) and
    lode-qxdn.6 (coverage) must use the same `?branch=trunk` form** rather than the bare
    `badge.svg`, so all three badges report on `trunk` by the same explicit mechanism.

  Either way, a `land/<id>` build cannot affect the README, so keeping `land/**` in the trigger is
  safe to leave in place; there is no badge-hygiene reason to drop it.

## Packaging assertion is a single implementation, shared by both workflows (lode-zuqp)

`noxfile.py`'s `build` session is the ONE place that builds a wheel/sdist and asserts the
package-data (`lode/schema.sql`, `lode/tui/lode.tcss`) made it in — the lode-1i8.4 footgun a clean
`python -m build` exit doesn't catch on its own. Both CI workflows call it rather than each keeping
a hand-copy (a hand-copy already drifted once — lode-j6mj's review caught build.yml's copy checking
only the wheel, not the sdist):

- **`build.yml`** (push/PR) calls `nox -s build` with no posarg — builds into a scratch
  `TemporaryDirectory` and discards the artifacts; only the assertion matters there.
- **`release.yml`** (the `vX.Y.Z` tag push that ships the published wheel) calls
  `nox -s build -- dist` — the session takes an optional output-directory posarg, and passing one
  keeps the built wheel/sdist in `./dist` so the `gh release create ... dist/*` step can upload
  them. Before this, release.yml ran its own untouched `python -m build` with no assertion at all,
  so a wheel silently missing package-data could still ship to users even though the same failure
  was already being caught on every push/PR build.

**Decision: `scripts/release.sh`'s pre-tag gate also runs `nox -s build`** (no posarg — a local
sanity check, not an artifact producer), right after `nox -s tests`. It's cheap (~4s, per lode-j6mj)
and fails the kickoff before a tag is even pushed, rather than waiting for the tag-triggered CI run
to catch the same problem a few seconds later. This is fail-fast redundancy, not the only gate —
release.yml's own `nox -s build -- dist` call is what actually gates the published artifact, since
the kickoff machine's tree and the clean-room CI checkout aren't guaranteed identical.

## Release notes

Release notes are **compiled from the ticket record, not composed as prose** (lode-0l1). At
kickoff, the `/release` skill collects every bd ticket that landed on `trunk` in the release
window — first-parent history since the previous tag (the whole history on the first release),
identified by `Merge land/<id>:` subjects and trailing `(lode-…)` markers on direct commits —
resolves each in bd, and produces an itemized list: child tickets grouped under their parent
epic, standalone tickets categorized as features / fixes / internal-workflow. Tickets that
landed but never closed are flagged to the operator instead of listed.

The confirmed list travels as the **annotated tag's body** (`scripts/release.sh X.Y.Z
notes-file`; the script owns the `lode vX.Y.Z` subject) — the tag is the only wire from kickoff
to CI, so the notes need no side channel. The release workflow re-fetches the annotated tag
object (`actions/checkout` demotes it to a lightweight ref, actions/checkout#290) and publishes
the tag body via `gh release create --notes-file`. This replaced `--generate-notes`, which
derives notes from merged PRs and produces nothing useful in this no-PR, `/land`-merge workflow.

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
