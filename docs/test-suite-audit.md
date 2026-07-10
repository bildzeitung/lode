# Test-suite audit (lode-b4w.1)

Analysis leg of the [lode-b4w test-suite optimisation epic](../.beads/README.md) (see `bd show
lode-b4w`). Feeds three execution tickets: high-confidence deletions ([lode-b4w.2](#lode-b4w2-checklist-high-confidence-deletereview-list)),
consolidations ([lode-b4w.3](#lode-b4w3-checklist-consolidation-groups)), and the human cutoff
decision ([lode-b4w.4](#lode-b4w4-proposal-cutoff)). **No test files were changed to produce this
document** — analysis only, per lode-b4w.1's acceptance criteria.

Snapshot: **57 test files, 873 test functions (AST-parsed), 952 pytest-collected items** (the delta
is parametrization — one `def test_x(...)` with `@pytest.mark.parametrize` collects as several
items), **19,292 test LOC**. Collected against trunk at the point this audit ran (2026-07-10); grew
slightly from the epic's original count (928 collected / 849 functions) between filing and this pass.

## Headline finding

**This suite does not have a fat low-value tail to prune.** Systematic clustering (see
[Methodology](#methodology)) plus manual spot-verification of every candidate pair found **no exact
duplicate-coverage tests** — the codebase's one-behavior-per-test discipline is real, not aspirational.
What the clustering *does* find, repeatedly, is **structural over-granularity**: symmetric code paths
(most visibly annotation-vs-edge in the curation/display/staleness layer, and job-type/terminal-status
variants elsewhere) tested via copy-pasted parallel test bodies instead of `@pytest.mark.parametrize`.
That is where the real, low-risk test-count reduction lives — not deletion.

Consequently:

- [lode-b4w.2](#lode-b4w2-checklist-high-confidence-deletereview-list) (high-confidence delete) gets a
  **short, conservative list** — most of what looked like a duplicate on first pass turned out to
  guard a genuinely distinct case once read (see the [near-duplicate audit](#near-duplicate-pairs-checked-and-cleared) below).
- [lode-b4w.3](#lode-b4w3-checklist-consolidation-groups) (consolidate) is where the bulk of the
  reduction opportunity sits — concrete groups below, ~90 tests across 10 groups, most collapsible to
  roughly half their current count without losing an assertion.
- [lode-b4w.4](#lode-b4w4-proposal-cutoff) (human cutoff) gets a **narrow, single-item proposal** —
  the audit did not find a broad "cut everything below score N" case to make; forcing one would trade
  real coverage for a speed win this suite doesn't need from deletion (runtime is better addressed by
  [lode-b4w.6](#appendix-c-what-this-audit-deliberately-left-to-lode-b4w6)'s non-destructive levers).

## Methodology

Four data sources, cross-checked against each other and against reading the actual test source (no
step below stands alone as a verdict):

1. **Collection inventory** — `pytest --collect-only -q` (952 items) and an `ast`-based parse of every
   `tests/test_*.py` (873 test functions: name, class, LOC span, decorator list, docstring). AST
   parsing found the same module set pytest collects; the 79-item gap is exactly the parametrized
   fan-out.
2. **Real timing** — a full offline run (`pytest -q --durations=0`, `ANTHROPIC_API_KEY` unset so
   `test_eval_live.py` self-skips, matching what `nox -s tests` does in this environment) and a
   fast-tier run (`pytest -q -m "not slow"`, matching `nox -s unit`). Durations join to AST-derived
   test names by node-id prefix; pytest hides per-test durations under 0.005s, so those show as 0.0 in
   the joined data — negligible at that floor, not literally free.
3. **Per-test raw signal extraction** (mechanical, via a throwaway script, not committed — the *output*
   is committed as `docs/test-suite-audit-data.csv`): for every test function, LOC span, an assertion
   count (`assert` statements plus `pytest.raises`/`pytest.warns`/`pytest.approx`/`assert_called*`
   forms), a mock-density count (`MagicMock`, `mock.patch`, `monkeypatch.setattr`, `@patch`,
   `mocker.*`), whether it carries `@pytest.mark.slow`, and whether it's parametrized.
4. **Structural clustering + manual verification** — group tests within a file by shared name prefix
   and by body-shape similarity (LOC/assert/mock counts matching plus name similarity), then **read**
   every candidate group before calling it a duplicate or a consolidation opportunity. This step is
   what actually drives the .2/.3/.4 checklists — the mechanical score is a triage aid, not the
   verdict (see the next section for why).

### Why the mechanical score is a triage aid, not a ranking

A composite `proxy_score = asserts − 1.5·mocks − 0.15·duration_s − 0.02·max(loc−20, 0)` is computed
per test (in `docs/test-suite-audit-data.csv`) and used to *sort* the raw appendix data so a human
skimming it sees the cheapest-looking tests first. It is deliberately **not** presented as a value
verdict, because it demonstrably gets specific tests wrong. Worked example: five tests initially
scored at the mechanical floor (0 detected assertions). Reading them found:

- `test_lock.py::test_release_is_idempotent`, `test_stale_lock_does_not_raise`,
  `test_enrich.py::test_inferred_edge_confidence_bounds_valid`,
  `test_latency_probe.py::test_probe_never_returns_on_its_own` — all four are legitimate
  **"must not raise" contract tests**: the test body calls the code under test with no exception
  handler, and pytest failing on an uncaught exception *is* the assertion. Cheap, valid, high
  failure-detection power per LOC. The mechanical score would have flagged all four as bottom-tier;
  they are not deletion candidates.
- `test_capture_lag_diagnosis.py::test_related_notes_pass_cost_against_seeded_corpus` — genuinely has
  zero assertions and is self-documented in its own docstring as "Informational... Not a pass/fail."
  This one *is* a real finding — see [lode-b4w.4](#lode-b4w4-proposal-cutoff).

Same lesson from the top and bottom of the mock-penalized tail: `test_cli.py::test_ask_cli_threads_settings_to_gate`
scored near the mechanical floor (loc=62, 3 mocks, 6.63s — it's `@pytest.mark.slow`) but reading it
shows it verifies real CLI→gate settings threading (a config-drift regression class), not thin
wiring — it's expensive, not low-value. Every test below is annotated by hand, not by score.

## Baseline timings

Both target runtimes (2026-07-09 user decision on lode-b4w: *both* the inner loop and the full gate),
measured in this environment as of this pass — offline (`ANTHROPIC_API_KEY` unset), no other
concurrent load:

| Gate | Command | Result | Wall clock |
|---|---|---|---|
| Full landing gate | `nox -s tests` (`pytest`, no marker filter) | 945 passed, 7 skipped, 952 collected | **177.49s** (0:02:57) |
| Fast inner loop | `nox -s unit` (`pytest -m "not slow"`) | 938 passed, 6 skipped, 8 deselected | **142.60s** (0:02:22) |

For context, the epic's originally recorded baseline (`bd show lode-b4w`, 2026-07-09) was **471.66s**
full / **~199s** excl. the live eval — but that run was both load-skewed (two other agent gates running
concurrently on the same machine) *and* included `test_eval_live.py` (273.07s alone) because
`ANTHROPIC_API_KEY` was ambient in that shell. This pass's 177.49s is unloaded and keyless, so it is
not directly comparable to the epic's number as a "did we speed up" delta — it's a fresh, cleaner
baseline for this audit's own before/after tracking (lode-b4w.2/.3/.6 should diff against **this**
177.49s / 142.60s pair, not the epic's original load-skewed figure). The `test_eval_live.py` exclusion
from the everyday gate is already correct (self-skips without a key) — lode-b4w.7 is about making sure
it's never accidentally credentialed into the gate, not about a currently-broken state.

### Top offline-gate offenders (`--durations=0`, `call` phase only; 1534 sub-0.005s entries excluded)

| Rank | Test | Duration |
|---|---|---|
| 1 | `test_skeleton_gate.py::test_gate4_fts_findable_before_lode_work` | 9.53s |
| 2 | `test_skeleton_gate.py::test_gate1_add_ask_yields_cited_claim_with_verbatim_span` | 8.06s |
| 3 | `test_cli.py::test_ask_cli_threads_settings_to_gate` | 6.63s |
| 4 | `test_cli.py::test_ask_retrieves_and_renders_a_cited_claim` | 4.79s |
| 5 | `test_skeleton_gate.py::test_gate2_out_of_corpus_question_abstains` | 4.61s |
| 6 | `test_cli.py::test_retrieve_dense_leg_surfaces_a_vector_only_match` | 3.11s |
| 7 | `test_cli.py::test_ask_abstains_when_no_claim_survives` | 2.88s |
| 8 | `test_tui_browse_screen.py::test_escape_steps_back_version_view_then_history_then_note_view` | 1.62s |
| 9 | `test_tui_browse_screen.py::test_escape_steps_back_note_then_list_then_capture` | 1.51s |
| 10 | `test_skeleton_gate.py::test_gate3_eval_scorer_reports_green_on_golden_fixture` | 1.48s |

Aggregate call-time by file (top contributors; full table in the companion CSV):

| File | Σ duration | # tests w/ measurable duration |
|---|---|---|
| `test_cli.py` | 25.21s | 68 |
| `test_skeleton_gate.py` | 23.95s | 5 |
| `test_tui_browse_screen.py` | 17.96s | 19 |
| `test_tui_edit_screen.py` | 10.41s | 10 |
| `test_tui_quit.py` | 9.67s | 12 |
| `test_tui_capture_save_and_new.py` | 5.29s | 10 |
| `test_eval_harness.py` | 4.17s | 6 |

Only **8 tests** across 3 files carry `@pytest.mark.slow` (`test_cli.py`: 4, `test_skeleton_gate.py`:
3, `test_eval_live.py`: 1) — the matrix's `slow` column, the CSV's `slow=True` rows, and the **8
deselected** in the unit-tier baseline above all agree on 8. (A naive `grep -c '@pytest.mark.slow'`
returns 11 across these three files, but the extra hits are docstring/comment *mentions* of the marker
string — `test_cli.py` line 1304, `test_skeleton_gate.py` line 43, `test_eval_live.py` line 66 — not
decorated tests; `test_eval_live.py` in particular has only one test total. The AST-derived count in
the matrix is the real one.) That accounts for essentially all of the ~35s gap between the unit and
full tiers. The
remaining ~142s unit-tier cost is **not** concentrated in a marker-filterable set — it's spread across
real per-test TUI-pilot startup cost (`test_tui_*` files collectively) and un-mocked CLI/model-load
paths that aren't marked `slow` today. That's squarely [lode-b4w.6](#appendix-c-what-this-audit-deliberately-left-to-lode-b4w6)'s
territory (parallelism, fixture scoping), not this ticket's.

## Feature-coverage matrix

Every `src/lode` module cross-referenced against the test file(s) that import it (via static
`from lode... import`/`import lode...` scan). **No `src/lode` module was found without at least one
dedicated test file** — a genuine positive finding; there is no orphan-module gap to close here.

| Test file | `src/lode` module(s) covered | Tests | LOC | Asserts | Mocks | Duration | slow |
|---|---|---:|---:|---:|---:|---:|---:|
| `test_answer.py` | answer | 9 | 35 | 15 | 0 | 0.00s | |
| `test_auth.py` | auth | 5 | 45 | 11 | 2 | 0.18s | |
| `test_capture_lag_diagnosis.py` | config, embedding, eval.seed, lexical, repository, storage, tui.related | 3 | 130 | 2 | 0 | 0.00s | |
| `test_chunking.py` | chunking, config | 19 | 111 | 33 | 0 | 0.00s | |
| `test_cited_answer.py` | answer, cited_answer, config, egress, qa, retrieval, storage | 10 | 175 | 28 | 0 | 0.00s | |
| `test_cli.py` | answer, cited_answer, **cli**, config, egress, embedding, enrich, enrichment_view, hashing, jobs, lock, storage, versions, worker | 78 | 1602 | 240 | 23 | 25.21s | 4 |
| `test_config.py` | config | 11 | 63 | 29 | 0 | 0.00s | |
| `test_curation.py` | curation, storage | 10 | 64 | 20 | 0 | 0.05s | |
| `test_display.py` | display, storage | 14 | 100 | 27 | 0 | 0.05s | |
| `test_drawdown.py` | config, **drawdown**, repository, storage, webfetch | 37 | 348 | 69 | 1 | 0.15s | |
| `test_egress.py` | config, egress, storage | 16 | 178 | 46 | 0 | 1.05s | |
| `test_embedding.py` | config, embedding, externals, repository, storage, versions | 12 | 291 | 31 | 0 | 1.71s | |
| `test_enrich.py` | config, curation, enrich, reconcile, storage | 58 | 928 | 140 | 0 | 0.72s | |
| `test_enrichment_view.py` | display, **enrichment_view**, staleness, storage | 18 | 292 | 49 | 0 | 0.27s | |
| `test_eval_golden.py` | answer, eval.golden, eval.seed, faithfulness | 10 | 68 | 17 | 0 | 0.00s | |
| `test_eval_harness.py` | answer, cited_answer, config, eval.golden, **eval.harness**, retrieval, storage | 6 | 136 | 26 | 0 | 4.17s | |
| `test_eval_live.py` | cited_answer, config, embedding, eval.harness, storage | 1 | 61 | 3 | 0 | 0.00s* | 1 |
| `test_eval_seed.py` | **eval.seed**, hashing | 7 | 34 | 8 | 0 | 0.00s | |
| `test_externals.py` | config, embedding, **externals**, hashing, storage, webfetch | 21 | 307 | 61 | 0 | 0.28s | |
| `test_faithfulness.py` | answer, config, **faithfulness** | 26 | 109 | 34 | 0 | 0.00s | |
| `test_gate.py` | answer, config, **gate** | 16 | 153 | 31 | 0 | 0.00s | |
| `test_hashing.py` | config, **hashing** | 8 | 65 | 16 | 0 | 0.00s | |
| `test_ids.py` | **ids** | 2 | 5 | 3 | 0 | 0.00s | |
| `test_jobs.py` | **jobs**, storage | 10 | 112 | 10 | 0 | 0.15s | |
| `test_latency_probe.py` | tui.latency_probe | 2 | 13 | 2 | 0 | 0.22s | |
| `test_lexical.py` | chunking, config, embedding, **lexical**, repository, storage | 14 | 214 | 23 | 0 | 0.28s | |
| `test_lock.py` | **lock** | 12 | 95 | 14 | 0 | 0.01s | |
| `test_logconfig.py` | **logconfig** | 12 | 131 | 21 | 0 | 0.00s | |
| `test_models_smoke.py` | config (opt-in, `LODE_SMOKE_MODELS=1`) | 3 | 26 | 5 | 0 | 0.00s | |
| `test_notes_read.py` | **notes_read**, storage, versions | 21 | 261 | 25 | 0 | 3.12s | |
| `test_qa.py` | answer, config, **qa**, storage | 8 | 106 | 22 | 0 | 0.00s | |
| `test_reconcile.py` | **reconcile**, storage | 19 | 189 | 34 | 0 | 0.19s | |
| `test_redact.py` | config, **redact** | 13 | 64 | 23 | 0 | 0.01s | |
| `test_repository.py` | hashing, jobs, lexical, redact, **repository**, storage, versions | 37 | 467 | 75 | 1 | 0.66s | |
| `test_retrieval.py` | config, embedding, externals, lexical, repository, **retrieval**, storage, vectorstore | 58 | 767 | 122 | 0 | 2.46s | |
| `test_skeleton_gate.py` | answer, cited_answer, cli, config, eval.golden, eval.harness, faithfulness, storage (integration, no single module) | 5 | 275 | 30 | 7 | 23.95s | 3 |
| `test_staleness.py` | **staleness**, storage | 20 | 322 | 37 | 0 | 0.34s | |
| `test_storage.py` | **storage** | 7 | 125 | 9 | 0 | 0.59s | |
| `test_tui_app.py` | config, lexical, repository, storage, **tui.app**, tui.related, tui.screens.capture, tui.screens.reconcile | 9 | 171 | 13 | 2 | 2.10s | |
| `test_tui_ask.py` | answer, cited_answer, config, egress, storage, **tui.ask**, versions | 9 | 142 | 18 | 2 | 0.33s | |
| `test_tui_ask_screen.py` | answer, cited_answer, egress, tui.app, tui.ask, **tui.screens.ask** | 4 | 84 | 9 | 2 | 1.49s | |
| `test_tui_browse_screen.py` | notes_read, storage, tui.app, tui.dates, **tui.screens.browse**, tui.screens.capture, versions | 20 | 560 | 57 | 0 | 17.96s | |
| `test_tui_capture.py` | hashing, storage, tui, **tui.capture**, versions | 5 | 72 | 11 | 1 | 0.54s | |
| `test_tui_capture_confirm.py` | tui.app, **tui.screens.capture** | 7 | 134 | 19 | 0 | 3.16s | |
| `test_tui_capture_save_and_new.py` | config, storage, tui, tui.app, tui.related, **tui.screens.capture**, tui.screens.reconcile, versions | 10 | 244 | 21 | 9 | 5.29s | |
| `test_tui_config.py` | config, tui.app, **tui.screens.config** | 4 | 55 | 11 | 0 | 1.58s | |
| `test_tui_dates.py` | **tui.dates** | 9 | 22 | 9 | 0 | 0.00s | |
| `test_tui_edit.py` | storage, **tui.edit**, versions | 8 | 101 | 19 | 0 | 1.13s | |
| `test_tui_edit_screen.py` | storage, tui.app, tui.screens.browse, tui.screens.capture, **tui.screens.reconcile**† | 10 | 309 | 29 | 0 | 10.41s | |
| `test_tui_quit.py` | storage, tui.app, tui.screens.browse, tui.screens.capture, tui.screens.config, tui.screens.reconcile | 12 | 306 | 36 | 0 | 9.67s | |
| `test_tui_reconcile.py` | storage, **tui.reconcile**, versions | 4 | 105 | 18 | 0 | 0.28s | |
| `test_tui_reconcile_screen.py` | hashing, storage, tui, tui.app, tui.screens.capture, **tui.screens.reconcile** | 3 | 75 | 10 | 3 | 1.56s | |
| `test_tui_related.py` | config, embedding, lexical, repository, storage, **tui.related** | 12 | 122 | 15 | 0 | 0.25s | |
| `test_vectorstore.py` | config, **vectorstore** | 7 | 79 | 14 | 0 | 0.18s | |
| `test_versions.py` | hashing, storage, **versions** | 23 | 251 | 75 | 0 | 0.18s | |
| `test_webfetch.py` | config, **webfetch** | 15 | 174 | 40 | 4 | 0.35s | |
| `test_worker.py` | config, enrich, jobs, reconcile, storage, **worker** | 64 | 1018 | 151 | 13 | 0.88s | |

Bold marks each file's primary/eponymous module. `test_skeleton_gate.py` has none — it's a deliberate
end-to-end integration gate (see [below](#test_skeleton_gatepy-is-not-redundant)), not a unit-test file
for one module. \* `test_eval_live.py` shows 0.00s because it self-skips without `ANTHROPIC_API_KEY`
in this measurement — its real cost when credentialed is ~273s (see [Baseline timings](#baseline-timings)).
† `test_tui_edit_screen.py`'s own primary module (`tui.edit_screen` under a different name, or folded
into `tui.screens.reconcile`/`tui.app`) is a naming mismatch worth a quick look but not a coverage gap
— the screen it tests is exercised, just not under a 1:1 filename match; not actioned here (out of
scope — analysis only).

**Read on `test_cli.py`, `test_worker.py`, `test_enrich.py`, `test_retrieval.py`, `test_repository.py`,
`test_drawdown.py` being the largest files by test count**: all six are either the CLI surface (which
legitimately cross-cuts most of the domain — every subcommand needs its own coverage) or a
job-queue/worker layer with a genuinely large state space (claim/run/retry/backoff/batch × per job
type). Their size tracks real surface area, not padding — confirmed by the consolidation-group scan
below finding real opportunities *within* several of them (esp. `test_worker.py`'s registry trio and
`test_repository.py`'s prefix-resolution quartet) without needing to touch most of their bulk.

### `test_skeleton_gate.py` is not redundant

Despite being the single most expensive file per test (23.95s / 5 tests, avg 4.79s), its own module
docstring frames it precisely: it is the **Phase-A exit gate** (lode-6w1.1 / lode-x6r.5 / lode-xyb) —
the one place that exercises the real CLI (`lode add` → `lode work` → `lode ask`) end-to-end with only
the two genuinely non-deterministic seams stubbed (the embedder and the Anthropic client), while the
faithfulness gate runs for real. No unit test elsewhere in the suite covers this integration path; deleting
it would remove the only test that would catch a wiring break between `add`/`work`/`ask` that each
module's own unit tests can't see. Its cost is a `nox -s unit`/parallelism problem
([lode-b4w.6](#appendix-c-what-this-audit-deliberately-left-to-lode-b4w6)), not a value problem.

## Near-duplicate pairs: checked and cleared

Structural clustering flagged ~55 same-file test pairs with high name-similarity and matching
LOC/assert/mock counts (candidates for either the .2 delete list or the .3 consolidate list). Reading
representative pairs across the full spread — not just the top hits — found **zero exact duplicates**.
Two worked examples (full reasoning in each test's own docstring, not invented here):

- `test_jobs.py::test_reenqueue_after_done_is_allowed` vs. `test_reenqueue_after_dead_is_allowed`
  (94% name-similar, identical LOC/assert shape): each verifies the partial dedup index excludes a
  *different* terminal status (`done` vs `dead`) from blocking re-enqueue — a real regression risk if
  someone edits the index's `WHERE` clause to only cover one. Distinct coverage; consolidation
  candidate (parametrize over status), not a duplicate.
- `test_worker.py::test_embed_is_registered_by_default` / `test_enrich_is_registered_by_default` /
  `test_refresh_is_registered_by_default` (87-90% similar, identical 5-line shape): each checks a
  *different* job-type string is present in the registry at import time. Distinct assertions,
  trivially parametrizable — the single cleanest consolidation candidate in the suite.

This pattern repeats across every candidate pair spot-checked (`test_curation.py`'s
annotation/edge pairs, `test_display.py`'s tag/entity/edge visibility tests, `test_staleness.py`'s
`reanchor_annotations_*`/`reanchor_edges_*` mirror sets, `test_notes_read.py`'s
`note_body_*`/`version_body_*` trio): same shape, different target, real independent coverage. That is
precisely the signature of **over-granularity**, not **redundancy** — which is why the .2 list below is
short and the .3 list is long.

## lode-b4w.2 checklist (high-confidence delete/review list)

Per lode-b4w's design note, this list needs no further human sign-off — it's for exact-duplicate or
truly redundant coverage. The systematic search (all ~55 clustered candidate pairs, spot-verified)
**found none** that qualify. Rather than pad this list to have something to delete, the honest
execution instruction for lode-b4w.2 is:

- **No unconditional deletions identified.** Re-verify against trunk at execution time (a few days may
  have passed) by re-running the clustering pass in [Methodology](#methodology) — a genuinely new
  duplicate could have landed since this audit. If none turn up, close lode-b4w.2 with that note; it's
  a legitimate "nothing to do" outcome, not a failure to find work.

## lode-b4w.3 checklist (consolidation groups)

Every group below is a **structural** finding (symmetric code paths tested via parallel, near-identical
bodies) verified by reading the actual test source, not just name/shape matching. "Combine" means
`@pytest.mark.parametrize` over the varying axis (job type, entity kind, terminal status, search
function, …) with one shared body — per lode-b4w.3's acceptance criteria, **no loss of assertion
coverage**: every distinct case the originals checked must still be checked by the combined
parametrization, just as separate parameter rows, not dropped.

1. **`test_staleness.py` annotations/edges mirror set — biggest single win.** `reanchor_annotations`
   and `reanchor_edges` (`src/lode/staleness.py`) are two independently-callable functions over two
   tables with identical re-anchor semantics, tested via 9+9 parallel bodies differing only in which
   function/table is exercised:
   `{unchanged_quote_is_fresh, changed_context_is_stale, missing_both_is_orphaned,
   no_quoted_text_*_present_is_fresh, no_quoted_text_*_absent_is_orphaned, user_source_not_touched,
   returns_counts, empty_returns_zeros}` × `{annotations, edges}`. Plus
   `test_quoted_text_column_exists_in_annotations`/`_in_edges` (2 tests, same shape). Parametrize over
   `(reanchor_fn, table_name)` — **20 tests → ~10**, this file's `mocks=0`/`asserts=37` profile means
   the combined tests lose nothing.

2. **`test_curation.py` annotation/edge pairs** — same root symmetry (`delete_annotation`/`delete_edge`,
   `is_annotation_suppressed`/`is_edge_suppressed` in `src/lode/curation.py`):
   `test_delete_annotation_converts_to_user_orphaned_tombstone`/`test_delete_edge_converts_to_user_orphaned_tombstone`,
   `test_delete_annotation_missing_id_raises_keyerror`/`test_delete_edge_missing_id_raises_keyerror`,
   `test_annotation_not_suppressed_when_no_user_row`/`test_edge_not_suppressed_when_no_user_row`,
   `test_annotation_suppressed_after_delete`/`test_edge_suppressed_after_delete`,
   `test_annotation_suppressed_by_user_authored_row` (edge-side counterpart not present — check
   whether that's a real coverage gap or intentional while consolidating). Parametrize over
   `(entity_kind, delete_fn, is_suppressed_fn)` — **~8 of 10 tests → ~5**.

3. **`test_display.py` visibility-by-kind trio** — `test_tag_is_always_visible(status)`,
   `test_entity_is_always_visible(status)`, `test_ai_edge_is_always_visible(status)` are each *already*
   parametrized over `status`, but duplicated across three entity kinds. Add `kind` as a second
   parametrize axis — **3 tests (each already N-way) → 1 (now N×3-way)**. Likewise
   `test_user_fresh_annotation_is_visible_not_stale`/`test_user_fresh_edge_is_visible_not_stale` (2→1)
   and `test_user_orphaned_annotation_is_a_hidden_tombstone`/`test_user_orphaned_edge_is_a_hidden_tombstone` (2→1).

4. **`test_worker.py` registry-default trio** — `test_embed_is_registered_by_default`,
   `test_enrich_is_registered_by_default`, `test_refresh_is_registered_by_default` (5-line bodies,
   identical shape, differ only by the registered-type string literal). Parametrize over job type —
   **3 → 1**. Cleanest, lowest-risk item on this list; do this one first.

5. **`test_jobs.py` terminal-status reenqueue pair** — `test_reenqueue_after_done_is_allowed` /
   `test_reenqueue_after_dead_is_allowed` (see [above](#near-duplicate-pairs-checked-and-cleared)).
   Parametrize over `status in ("done", "dead")` — **2 → 1**.

6. **`test_enrich.py` gap-skip-reason group (partial)** — `test_enrich_gap_skips_tombstone`,
   `test_enrich_gap_skips_purged`, `test_enrich_gap_skips_no_egress` share an identical 4-line
   "seed one disqualifying condition, assert gap count is 0" shape; `test_enrich_gap_skips_live_job`
   has a materially different setup (an in-flight job, not a static disqualifying row) — **verify
   before merging it in**, it may not fit the same parametrize table cleanly. Also
   `test_inferred_edge_confidence_below_zero_rejected`/`_above_one_rejected` alongside
   `_bounds_valid` — three boundary checks on `InferredEdge.confidence`, parametrize over
   `(confidence, should_raise)` — **3 → 1**.

7. **`test_retrieval.py` search-cap pair + match-query trio** —
   `test_lexical_search_caps_at_k`/`test_vector_search_caps_at_k` (parametrize over search fn, 2→1);
   `test_build_match_query_ors_quoted_word_tokens`/`_quotes_terms_colliding_with_fts5_operators`/`_empty_when_no_word_tokens`
   (3 tests, same function under test, different input shapes — parametrize over `(tokens, expected)`,
   **3 → 1**).

8. **`test_notes_read.py` `note_body`/`version_body` trio (partial)** —
   `test_note_body_returns_the_live_head_body`/`test_note_body_returns_none_for_a_deleted_note` share
   shape and could parametrize over note state; `test_version_body_returns_a_specific_non_head_version`
   is a related but distinct call (`version_body` not `note_body`) — group for review, not a blind
   merge. Also `test_list_notes_falls_back_to_first_line_when_unenriched`/`_first_non_blank_line` (2→1,
   parametrize over body content).

9. **Lower-confidence — flag for the .3 executor's own judgment, do not force:**
   `test_config.py`'s `test_lode_home_defaults_to_dot_lode`/`_honours_env_var`/`_expands_user` trio
   (three genuinely different resolution mechanisms — default vs. env var vs. `~`-expansion — may not
   share a clean parametrize table); `test_repository.py`'s `test_resolve_note_prefix_*` quartet (valid
   vs. error-path cases may want to stay as two separate parametrize groups rather than one); and
   `test_worker.py`'s 8-member `test_reclaim_*` group (mostly distinct behaviors — read each before
   merging any two).

10. **Speed-adjacent, not this ticket** — the TUI screen files (`test_tui_browse_screen.py`,
    `test_tui_edit_screen.py`, `test_tui_quit.py`, `test_tui_capture_save_and_new.py`) dominate the
    unit-tier's non-slow-marked runtime (~45s combined) via per-test Textual `Pilot` app startup, not
    duplicate logic — each test drives a distinct interaction. Consolidating *within* a single pilot
    session (multiple assertions per app boot instead of one test = one boot) would cut real wall-clock
    but changes test isolation semantics enough that it belongs in
    [lode-b4w.6](#appendix-c-what-this-audit-deliberately-left-to-lode-b4w6) (speed levers) or its own
    ticket, not silently folded into a same-behavior consolidation here.

## lode-b4w.4 proposal: cutoff

Per the epic's design note, cutoff-based deletion trades real coverage for speed and needs explicit
human sign-off. This audit's proposal is deliberately **narrow** — one item, not a percentile score
cutting across the suite (see [why the mechanical score isn't a ranking](#why-the-mechanical-score-is-a-triage-aid-not-a-ranking)
for why a broader mechanical cutoff would be untrustworthy):

- **`test_capture_lag_diagnosis.py::test_related_notes_pass_cost_against_seeded_corpus`** — zero
  assertions; its own docstring states *"Informational: how long does `find_related_notes` itself
  take? Not a pass/fail."* It cannot fail and therefore has zero regression-detection power; it exists
  to print timing data during a manual run of the historical lode-0wj.2 GIL-lag spike. Its two sibling
  tests in the same file (`test_event_loop_lag_during_related_notes_pass`,
  `test_event_loop_lag_isolated_to_onnx_embed_call`) **do** carry hard `assert p95 < LATENCY_TARGET_P95_MS`
  checks and are **not** part of this proposal — they guard a real regression (the async pass silently
  starting to hold the GIL) and should stay.
  - **Decision needed**: delete outright, or convert to a standalone benchmark script outside the
    pytest gate (if the timing telemetry itself has ongoing diagnostic value beyond the closed
    lode-0wj.2 investigation)? Both are reasonable; this audit doesn't have a strong opinion — it's a
    genuine judgment call about whether the timing data is still wanted.

No other candidate met the bar (a test with essentially no regression-detection power *and* no
ongoing informational purpose worth relocating). `test_models_smoke.py` was checked and found to
already be correctly isolated (opt-in via `LODE_SMOKE_MODELS=1`, skipped by default) — not actionable.

## Appendix A: reproducing this data

```bash
. ./venv/bin/activate
unset ANTHROPIC_API_KEY

# Baseline timings
pytest -q --durations=0 tests/          # full gate (matches nox -s tests)
pytest -q -m "not slow" tests/          # fast tier (matches nox -s unit)

# Collection inventory
pytest --collect-only -q
```

Per-test raw signal extraction (assert/mock counts, LOC span, `proxy_score`) used an ad hoc `ast`-based
script, not committed (throwaway tooling for this audit) — its *output* is committed as
[`docs/test-suite-audit-data.csv`](test-suite-audit-data.csv): one row per test function (873 rows),
columns `node_id, file, class, name, loc, asserts, mocks, slow, param, duration_s, proxy_score`, sorted
by file then ascending `proxy_score`. Re-run the methodology in [Methodology](#methodology) to
regenerate if a future pass needs fresher numbers.

## Appendix B: companion data file

[`docs/test-suite-audit-data.csv`](test-suite-audit-data.csv) — the full per-test matrix referenced
throughout this doc ("per-test value weights" in lode-b4w.1's acceptance criteria). `proxy_score` is
the triage heuristic from [Methodology](#methodology); treat it as a sort key for skimming, not a
verdict — every deletion/consolidation claim in this doc is backed by reading the actual test, not by
this column alone.

## Appendix C: what this audit deliberately left to lode-b4w.6

Runtime observations that are about **speed**, not **value**, and so don't belong in a delete/consolidate
checklist:

- `test_skeleton_gate.py` and the `@pytest.mark.slow`-tagged `test_cli.py` tests pay real
  un-mocked `FastEmbedCrossEncoder` model-load cost — a candidate for narrower mocking or session-scoped
  model fixtures, not deletion (see [why it's not redundant](#test_skeleton_gatepy-is-not-redundant)).
- The TUI screen test files' ~45s combined unit-tier cost is per-test `Pilot` app-boot overhead — a
  fixture-scoping or `pytest-xdist` parallelism candidate (see [group 10](#lode-b4w3-checklist-consolidation-groups)
  above).
- `nox -s unit` (142.60s) and `nox -s tests` (177.49s) are both this audit's fresh, unloaded,
  keyless baselines — lode-b4w.6 and lode-b4w.2/.3's "wall-clock delta vs. baseline" closing notes
  should diff against these two numbers, not the epic's original load-skewed 471.66s.
