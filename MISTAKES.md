# MISTAKES.md

Log of mistakes made while working on this repo (CLAUDE.md, General Directive 9).
Each entry: what happened / root cause / consequence / the rule that prevents a repeat.
Newest first.

## 2026-11-21 — `/land`'s union merge detached a correction marker from the entry it corrects

- **What happened:** `land/lode-ucpu` appended its `**Update (lode-ucpu` marker directly after
  the `lode-vlok` entry, which was at the time the last entry in `docs/decisions.md`. `lode-1pnt`
  landed first and appended two `##` sections at the same place. `/land`'s merge of
  `land/lode-ucpu` (`67bacb9d`) put the marker after those sections, under the unrelated
  `lode-sflg` heading. The `lode-smpk` build and its technical review found it a day later.
- **Root cause:** both sides added lines at the same position, and the `merge=union` driver
  (`lode-4jtc.1`) keeps both with no conflict, trunk's lines first. That is right for two new
  entries and wrong for a marker that must stay attached to the entry above it; the `lode-4jtc.1`
  entry names two failure modes of the driver, and this is not one of them. The branch was
  correct in isolation at both reviews, `check-decisions-no-silent-rewrite.sh` fires only on a
  removed line, and the marker test checks a marker's shape, not where it sits.
- **Consequence:** trunk carried the `lode-vlok` entry with no marker on it, so its "Open in
  `lode-ucpu`" claim read as current, and the docs index returns the ruling under the `lode-sflg`
  section. `lode-smpk`'s ruling said to record its Update "below the `lode-ucpu` Update", and the
  builder followed that into the wrong section; the review moved it onto the entry.
- **Rule that prevents a repeat:** a marker appended to the last entry of `docs/decisions.md`
  can be detached by any merge. Whoever merges a branch that adds a `**Update (` line to that
  file, whether trunk into the branch or the branch into trunk, reads the merged file around
  the line and confirms it still follows the entry it names. A builder placing a new marker
  "below" an existing one first checks that the existing one is on the entry. The merge half of
  this rule is interim: the stages that merge do not read this file, so `lode-tikr` asks the
  maintainer whether to gate it or put the step in their instruction files, and whether to move
  the detached marker back.
  - **Update (lode-tikr, 2026-11-21):** ruled. No gate; the step goes into the merging stages'
    instruction files (`lode-l8b7`), and the merge half of the rule above stays interim until
    that lands. The detached marker is not moved back. Record: `docs/decisions.md`, search
    `lode-tikr`.

## 2026-11-20 — A decision ticket stated a failure that the code cannot reach, and the maintainer ruled on it

- **What happened:** the `code-reviewer` on `lode-vlok` filed `lode-ucpu` asking whether
  `lode-4wgq` should also carry the submit-time provider identity. Its description stated as fact
  that a batch submitted under one provider and collected after a switch "records the refusal as
  Azure's, so reconcile never sends the note to Azure". The maintainer ruled to widen `lode-4wgq`
  on that description, and the builder copied the claim into `docs/decisions.md`. The
  `lode-ucpu` technical review traced the path: `collect_enrich_batch` polls with the collect-time
  provider's client, so the old provider's handle fails the poll and no refusal or success is
  stamped at all.
- **Root cause:** the ticket traced where the stamp is read (`provider_identity(settings)` at
  collect) but not whether a result can arrive there with a mismatched provider. The builder
  recorded the ticket's mechanism without tracing it either.
- **Consequence:** a maintainer ruling spent on a failure that cannot happen, and a false claim
  caught one review before it landed in the decision record. The record now states what the code
  does, and `lode-smpk` asks the maintainer whether the provider half stands.
- **Rule that prevents a repeat:** before filing a ticket that asks the maintainer to decide about
  a failure, trace that failure in the code from the trigger to the wrong outcome. In the ticket,
  mark any step that was not traced "not checked against the code", the way `lode-4wgq`'s
  description does for its two mechanisms. A builder recording a ruling writes the ruling, and
  writes a claimed mechanism only after tracing it or labelled as the ticket's claim. `lode-low0`
  tracks putting both into the instruction files.

## 2026-11-19 — A negative assertion on `lode status` output could never fail, because Rich wraps the line

- **What happened:** `lode-5try`'s builder covered the acceptance criterion "lode status prints no
  'run lode reenrich' hint" with `assert "lode reenrich" not in status.stdout`. Rich wraps that
  hint at the test runner's terminal width, splitting the phrase across lines, so the assertion
  held whether or not the fix was present. The `code-reviewer` caught it by running the test
  against trunk's unfixed scan: the status assertion passed and only the later reenrich assertion
  failed.
- **Root cause:** a `not in` substring check on rendered console output proves nothing unless the
  same substring is known to appear when the condition holds. The existing positive test for this
  hint (`test_status_hints_enrichment_stale_uniform_disagreement`) already asserts on short
  fragments for exactly this reason.
- **Consequence:** none landed. Unreviewed, one of the ticket's three acceptance criteria would
  have shipped with a test that could not detect a regression.
- **Rule that prevents a repeat:** a negative assertion on rendered CLI or TUI output uses a
  fragment that a positive test already proves is printed. Confirm the new test fails without the
  fix before trusting it.

## 2026-11-17 — A docs-nits batch dropped a nit as "anchor-gone" when its anchor had never reached trunk

- **What happened:** the builder draining collector `lode-rrpg` dropped nit 1
  (`docs/configuration.md:352`, a misplaced trailing "on the same deployment") as anchor-gone and
  recorded it as already fixed. The nit's anchor was the text `lode-5x01`'s branch produced, and a
  needs-rebase pickup had discarded that edit (the pickup entry of the same date, below), so the
  anchor never landed. The clause the nit corrects is still in trunk's sentence. The successor
  collector `lode-0z02` already held a carry-over nit on the same line whose replacement is that
  anchor, with a note to reconcile the two. The `code-reviewer` caught it.
- **Root cause:** the anchor-gone exception reads "the verbatim anchor is absent" as "the nit is
  fixed". An anchor is also absent when the text it was written against never reached trunk.
- **Consequence:** none landed. Unreviewed, a reviewed wording fix would have closed with the batch
  under a false "already fixed" record, and the successor's note would have pointed at a nit that
  no longer existed. The reviewer carried the nit over to `lode-0z02` by hand.
- **Rule that prevents a repeat:** before dropping a nit as anchor-gone, read the current sentence
  for the defect the nit names and check the open collector for a nit whose replacement is this
  anchor. Drop only when the defect is gone; otherwise carry the nit over. `lode-vlyj` tracks
  putting this into the instruction files.
  - **Update (lode-edyl, 2026-11-20):** "otherwise carry the nit over" no longer holds when the
    defect is visible in the current text. By maintainer ruling the builder applies the fix to the
    current text in that drain (recorded as applied (re-anchored)) or, when it does not apply
    mechanically, files it as its own ticket. The rule in force is the "Declined nits carry over"
    bullet in `docs/agents-workflow.md`.

## 2026-11-17 — `/land` rebuilt its accepted set from the live queue and picked up a ticket it had never vetted

- **What happened:** a `/land` pass vetted and semantically reviewed three branches (`lode-gro8`,
  `lode-mzuk`, `lode-5x01`). At Section 3a the lander filled `$ACCEPTED` by re-running
  `bd list --label ready-for-land` instead of listing the three ids it held accept verdicts for.
  `lode-1wwi` had reached `ready-for-land` mid-pass, so it entered `.git/land-state/accepted` and
  got a merge message with no drift check, no conflict precheck and no `land-review`. The lander
  saw the fourth id in its own output and removed it before `scripts/land-merge-batch.sh` ran.
- **Root cause:** the queue was treated as equal to the reviewed set. It is only equal at the
  instant Section 1 reads it; producers add to it for the whole length of the pass.
- **Consequence:** none landed. One command further and an unreviewed branch would have merged to
  `trunk` under a green re-gate, with nothing downstream to catch it.
- **Rule that prevents a repeat:** the accepted set is written only from the ids that received a
  `land-review` accept this pass. Never re-read the `ready-for-land` queue after Section 1; a
  ticket that arrives mid-pass waits for the next pass. Before the merge, compare the accepted
  file's ids against the verdicts in hand.

## 2026-11-17 — A needs-rebase pickup took trunk's side of a conflict and dropped a reviewed edit whose target text survived

- **What happened:** the second needs-rebase pickup on `land/lode-5x01` (merge `e59b630`) hit
  conflicts in `docs/configuration.md` and `docs/settings.md` against `lode-188q`. It took trunk's
  text and dropped the branch's applied, reviewed nit2 edit. Trunk's rewrite still contained the
  exact sentence that nit corrected. The ticket's bd note kept saying "nit2 APPLIED".
  `land-review` caught it.
- **Root cause:** "take trunk's side" was treated as finishing the resolution. The pickup did not
  check whether the edit it discarded still applied to trunk's version of the text.
- **Consequence:** a reviewed docs fix was lost from the landed branch and the ticket's disposition
  record was false. `/land` carried the nit over to the docs-nits collector by hand.
- **Rule that prevents a repeat:** when a pickup takes trunk's side of a conflicting hunk, grep the
  merged text for the anchor of each branch edit it discards. If the anchor survives, re-apply the
  edit on trunk's text. If it is gone, record the edit as superseded in `land_summary`, never as
  "dropped".

## 2026-11-15 — The last gate before trunk ran in a more lenient environment than the gate before it

- **What happened:** `lode-6874` moved the `code-reviewer` (and the rebase pickup) to gating
  unactivated, by `./venv/bin/nox`. `/land`'s combined re-gate and `scripts/land-replay.sh` kept
  `. ./venv/bin/activate` plus a bare `nox`. When `.python-version` moved to 3.14.10 (`fd817d5`), two
  tests in `tests/test_check_links.py` that spawned a bare `python` went red under the reviewer's
  gate on `trunk` itself (`lode-p3ks`); the same tree would have passed `/land`'s activated re-gate.
- **Root cause:** two gates certified the same tree in two environments. Activation puts
  `./venv/bin` first on `PATH`, so anything a test spawns by bare name resolves to the venv's copy
  and the defect is invisible; `noxfile.py`'s `_venv_tool()` only pins the tools nox itself launches.
  The lenient environment belonged to the gate that owns `trunk`.
- **Consequence:** the `tests` bucket was red for every reviewer on that machine while the one
  stage that writes `trunk` could not see it; `lode-d6t7`, a wording-only branch, was escalated on a
  red it did not cause. No work was lost.
- **Rule that prevents a repeat:** every gate before `trunk` runs in the same environment, and a
  later gate is never more lenient than an earlier one. Gates call the venv's tools by explicit
  path and never activate (`lode-xx90`); a test that launches an interpreter uses `sys.executable`,
  never a bare name. When one stage's gate invocation changes, change every stage's in the same
  ticket.

## 2026-09-14 — A needs-rebase pickup resolved the conflicting hunks and left the branch's own prose describing the pre-merge world

- **What happened:** the `/land` needs-rebase pickup on `lode-dozi` merged `trunk` @ `e3437e3` into
  `land/lode-dozi` and resolved the `scripts/docs_index_query.py` conflict correctly at the code level
  (kept trunk's `lode-qcp0` AND->OR fallback, wired `fallback_fired` to the real per-result flag). It
  left three prose statements untouched — one in `docs/decisions.md` (the `lode-dozi` log-fields
  entry) and two docstrings in `scripts/docs_index_log.py` — each asserting that no AND->OR fallback
  exists yet and that `fallback_fired` is always `False`. The `code-reviewer` and `land-review` both
  passed the branch; `land-review` caught it as non-blocking, and it landed on `trunk` with the
  merge (`lode-dozi`, 2026-09-14).
- **Root cause:** a trunk-merge pickup resolves the conflicting HUNKS. Prose elsewhere in the branch
  that describes the conflicting file's behaviour is not a conflict, so nothing surfaces it, and the
  merge silently turns true statements false. Every gate downstream reads the code, and the code was
  right.
- **Consequence:** `docs/` — this repo's source of truth — shipped a false description of a live
  field's semantics, and a docstring under `docstringcheck` repeats it. No behaviour impact.
- **Rule that prevents a repeat:** a trunk-merge resolution is not complete until the branch's own
  prose still describes the post-merge world. After a needs-rebase pickup, grep the branch's diff
  (`git diff origin/trunk...HEAD`) for prose naming the conflicting file, the feature that caused the
  conflict, or the sibling ticket that landed it — phrases like "does not exist yet", "in this diff",
  "concurrently", and the sibling's bd id — and re-read each hit against the merged result before
  pushing. The `code-reviewer` that follows a pickup owes the same grep: the pickup's `land_summary`
  names the conflicting file, so the search terms are already on the ticket.
  - **Update (lode-jb2q, 2026-11-17):** the pickup no longer writes `land_summary`. The conflicting
    files are named in its `REBASE PICKUP` note on the ticket instead.
  - **Update (lode-0z02, 2026-11-18):** the update above is too broad. The pickup still writes
    `land_summary` in one case: to correct a summary its conflict resolution made false
    (`.claude/agents/coding.md`, pickup step 5).

## 2026-09-08 — A promotion gate certified the artifact it was about to replace, not the one it would ship

- **What happened:** `scripts/update-deps.sh` ran its gates (`nox -t fix`, `nox -s tests`) with the
  CANDIDATE lock installed into `./venv` but the OLD `requirements.lock` still on disk, copying the
  candidate over the committed lock only after the gates went green. A full-lock recompile moved
  `typer` 0.27.1 -> 0.27.2; `tests/test_build_docs_site.py::test_workflow_pins_match_their_sources`
  (which asserts `.github/workflows/docs.yml`'s duplicated typer pin equals the lock's) was green
  inside the script -- it compared `docs.yml` against the still-old lock -- and red on the very next
  run.
- **Root cause:** the gate's inputs were not the artifact the run was about to ship. The script held
  the new pins in one place (`./venv`) and the old pins in another (`requirements.lock`), so every
  test that reads the committed lock from disk was structurally blind to the change being certified.
  "Gates green" was therefore exactly the wrong signal: it was true, and it meant nothing.
- **Consequence:** a lock commit whose own test suite is red on bare `trunk`. `/land` runs
  `nox -s tests` on bare `origin/trunk` before its isolation loop and reads a red there as "predates
  every branch, stop the pass", so this stalls the whole landing loop; CI's tests job reddens on the
  push too. Caught only because that session happened to re-run the suite after promotion.
- **Rule that prevents a repeat:** a gate that certifies an artifact must run against that artifact
  in its FINAL location, not against a staged copy alongside the version it is replacing. When a
  script gates-then-promotes, invert it: promote first, gate, and roll back on red -- and make the
  rollback hang off process EXIT, not off the handled-failure path alone, or a signal mid-gate
  strands an UNGATED artifact on disk that is indistinguishable from a gated one. Corollary: a pin
  duplicated between two files (a lock and a CI workflow) needs a test that reads BOTH from disk, so
  siting that test on the wrong side of a promotion is what makes it useless.

## 2026-08-18 — `--no-deps` on a mixed pip install line starved the docs-CI toolchain's dependency trees

- **What happened:** lode-fhql.23 collapsed docs.yml's toolchain install into one line,
  `pip install --no-deps -e . "mkdocs-material==9.5.44" "typer==0.27.1"`, and landed it. The
  "trunk docs" workflow went red on the next run at `import typer` →
  `ModuleNotFoundError: No module named 'annotated_doc'`.
- **Root cause:** pip's `--no-deps` applies to EVERY requirement on the command line, not just the
  one it is written next to. The flag was intended solely to keep lode's heavy unpinned runtime
  tree out of the docs CI leg (`-e .`), but it silently also starved the pinned toolchain packages
  of their own dependency trees. The step's comment asserted `--no-deps` was load-bearing without
  noting its scope, so the collapse read as a safe tidy-up.
- **Consequence:** `trunk`'s docs workflow was red — the docs site could not build until lode-xx46
  fixed it. Caught only by CI on trunk, after landing, because no local gate exercises the
  workflow's install line.
- **Rule that prevents a repeat:** A pip flag that changes RESOLUTION behavior (`--no-deps`,
  `--only-binary`, `--no-build-isolation`, `--pre`) is per-command, never per-requirement — never
  put such a flag on a line that mixes requirements wanting different treatment. When a workflow
  step needs one requirement treated differently from the rest, split into separate `pip install`
  commands and say in the comment that the split is load-bearing. Corollary: a change to a CI-only
  install line is untested by the local gates, so treat "it's just a one-line install tidy-up" as a
  change requiring the workflow to be reasoned through, not merged on shape.

## 2026-08-14 — An extraction replaced a fenced computation with `script | tee`, swallowing its machine-fault exit

- **What happened:** `land/lode-s9xe.6` moved `/land`'s stacked-branch graph derivation out of a
  fenced bash block and into `scripts/stacked-graph.sh`, wiring it as
  `scripts/stacked-graph.sh --base-ref origin/trunk --report-unordered | tee "$STATE_DIR/graph"`.
  A pipeline reports its LAST command's status, so the block's exit status was `tee`'s — always 0 —
  and the script's documented exit 2 (machine fault) was discarded. The prose four lines above the
  fence says exit 2 "is a machine fault, never 'no stacks'" and names the consequence exactly.
  Caught in technical review; nothing landed.
- **Root cause:** The extraction's whole point was to give the computation a real exit-code
  contract (0/1/2, lode-9i2p), and the call site that consumed it then threw that contract away
  for the convenience of seeing the output. lode-b8sr had already recorded this exact hazard for
  gates piped into `tail`/`grep`, but nothing pinned this call site — no test referenced
  `stacked-graph.sh` outside the script's own module — so neither the gates nor a scanner could
  have caught it.
- **Consequence:** Had it landed, a faulted graph run would have left a truncated or empty
  `$STATE_DIR/graph` that `land-merge-batch.sh`/`land-replay.sh` read as a valid "no stacks"
  graph — a dependent branch merged before its base, and a conflicting base failing to take its
  dependents with it, silently, in the file that owns every write to `trunk`.
- **Rule that prevents a repeat:** When an extraction replaces inline logic with a script call,
  the call site must PRESERVE the script's exit status: redirect (`> file || exit 1`) rather than
  pipe, and if the output must also be visible, `cat` it afterwards. Never `| tee`, `| tail`,
  `| grep` a command whose exit code carries a verdict. And every newly-wired call site gets at
  least one pin of its own — an extraction that leaves its consumer untested moves the code
  somewhere lintable while leaving the wiring exactly as unchecked as the markdown was.

## 2026-08-14 — A `docs_dir` config change left a default `nox` session red, with all named gates green

- **What happened:** `land/lode-fhql.9` repointed `mkdocs.yml`'s `docs_dir` from `docs` to
  `.docs-site-src` — a gitignored directory produced on demand by `scripts/build_docs_site.py`.
  `noxfile.py`'s `docs` session is in `nox.options.sessions` (runs on a bare `nox`) and runs
  `mkdocs build --strict` with no staging step, so it aborts with "Config value 'docs_dir': The
  path '.docs-site-src' isn't an existing directory" on any tree where the staging script has
  not run. The branch reached `ready-for-land` with gates reported green; `/land`'s semantic
  review caught it (confirmed mechanically) and escalated.
- **Root cause:** The gates actually run before hand-off are `nox -t fix` and `nox -s tests`;
  the `docs` session is in the default set but in neither named gate. A change to a config file
  consumed by a session outside those two is unexercised by the whole build-and-review
  pipeline, so builder and reviewer had no signal.
- **Consequence:** Had it landed, a bare `nox` — the documented way to run the full default
  set — would have gone red on trunk for every developer and agent immediately, with the cause
  (a gitignored directory) invisible in a fresh clone. Caught at the last gate; nothing shipped.
- **Rule that prevents a repeat:** When a change edits a file that a nox session outside
  `nox -t fix` / `nox -s tests` consumes (`mkdocs.yml`, shellcheck config, link-check config,
  …), run that session explicitly before hand-off and name it in the hand-off. `grep` the
  noxfile for the changed filename to find which sessions consume it; never treat "the two
  named merge gates are green" as covering it.

## 2026-08-14 — `nox -s tests` hard-reset the developer's live checkout (observed three times)

- **What happened:** While building `lode-s9xe.13` (the `scripts/land-replay.sh` extraction),
  every `nox -s tests` run executed `git reset --hard origin/trunk` **in the developer's own
  live checkout**, resetting the worktree — branch ref included. Observed three times on
  2026-08-14 before the cause was found.
- **Root cause:** `tests/test_gate_lib.py::_run_script()` omitted `cwd`, inheriting pytest's
  cwd: the live checkout. The gate-lib sabotage tests deliberately run consumer scripts with
  the fail-closed guard removed, so `gate_could_not_run` was undefined, each
  `... || gate_could_not_run ...` merely yielded 127, and (no `set -e`) execution continued
  to whatever came next. `land-replay.sh` was the first consumer whose "next" statement is an
  unconditional cwd-resolved `git reset --hard origin/trunk`.
- **Consequence:** The developer's checkout was hard-reset on every test run — uncommitted
  work and branch position destroyed with nothing in reflog to recover the uncommitted half —
  until the test harness was fixed. It also made the branch landing-blocking: merging it
  unfixed would have made every future `nox -s tests` run do the same.
- **Rule that prevents a repeat:** Any test that executes a repo script which can reach a
  cwd-resolved mutating command MUST pass an explicit `cwd` that is not inside any git
  repository (e.g. `tmp_path`). `_run_script()` now makes `cwd` a required parameter, so a
  new call site cannot silently inherit the live checkout. (Fixed on `land/lode-s9xe.13`,
  landed 2026-08-14, merge `08ca1bc`; full account in the `_run_script` docstring,
  `tests/test_gate_lib.py`.)
