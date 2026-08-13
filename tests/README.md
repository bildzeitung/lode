# tests/

What this suite covers, how it's organized, and — the part that surprises people coming from a
normal Python codebase — why a chunk of it is scanners that parse **Markdown**, not Python.

Run it via `nox`, never a hand-rolled venv invocation (see [`CLAUDE.md`](../CLAUDE.md)):

```bash
./venv/bin/nox -s tests    # full suite -- the landing gate
./venv/bin/nox -s unit     # fast tier: excludes @pytest.mark.slow
```

## Why scanners read the skills' Markdown

lode drives a large share of its own coding/review/land workflow through `.claude/skills/*/SKILL.md`
and `.claude/agents/*.md` — Markdown files containing fenced ```bash/```sh blocks that a Claude Code
agent executes directly, one fenced block at a time, as separate tool invocations. That shell code is
real, load-bearing logic (it claims bd issues, drives git, gates and lands branches) but it is **not
Python and not a `scripts/*.sh` file a linter or shellcheck run over `scripts/` would ever see** — it
lives embedded inside prose, inside a `.md` file. No linter, no `nox -s shellcheck` run, and no
ordinary code-review pass reaches it by construction: shellcheck only sees files it's pointed at, and
nobody points it at documentation.

That gap is exactly what several tests in this directory exist to close. They parse the SKILL.md /
agent-`.md` source text itself — extracting fenced-code-block spans, tracking shell-variable
assignment/use across those spans, matching hook one-liners against `.claude/settings.json` — and
assert properties about the *shell semantics* of that embedded bash, not about prose. Concretely:

- **`tests/test_skill_bash_state.py`** — the flagship example. Each fenced block in a SKILL.md/agent
  `.md` is a *separate* Bash tool call to the harness; shell state (variables, arrays, functions,
  `set -e`) never survives from one fenced block to the next. This test statically flags any `$VAR`
  referenced in a block that isn't *also* assigned somewhere in that same block — the bug class that
  shipped once for real (`lode-sfnb`: a populated `declare -A MSG` in one block, read back two blocks
  later, silently expanded to empty and broke `git merge -m ''` with no error output at all).
- **`tests/test_fence_parsing.py`** — direct unit tests for the one shared, importable fence parser
  (`src/lode/fence_parsing.py`) that `scripts/docs_index_chunker.py` and `tests/conftest.py`'s own
  `fence_scan` helper both build on, so "what counts as a fenced code block" is answered once, not
  reimplemented per gate. `tests/test_no_private_fence_state_machine.py` is the companion AST gate:
  no module may hand-roll a second, private fence-toggle state machine instead of importing this one.
  Several of the Markdown scanners below still carry private copies of similar parsing logic rather
  than importing it; extracting a shared test-side helper is tracked as `lode-s9xe.15`, not here.
- **`tests/test_no_hand_derived_skill_md_path.py`** — a mechanical gate that no test/script hand-types
  a `.claude/skills/*/SKILL.md` path instead of deriving it, so a renamed or moved skill can't silently
  desync from what's actually being scanned.
- **`tests/test_land_skill_guard_coverage.py`**, **`test_bd_list_limit_gate.py`**,
  **`test_epic_completion_check.py`**, **`test_epic_debate_gate.py`**, **`test_code_concurrency_cap.py`**,
  **`test_sweep_*`**, **`test_assert_main_checkout.py`**, **`test_isolation_guard.py`**,
  **`test_land_conflicts_state.py`**, **`test_land_lock.py`**, **`test_land_merge_one.py`**,
  **`test_land_state_load.py`**, **`test_merge_precheck.py`**, **`test_release_bump.py`**,
  **`test_release_latest_tag.py`**, **`test_validate_sha40_call_sites.py`**,
  **`test_worktree_gc_classify.py`** — each pins a specific correctness property of a `/code`,
  `/land`, `/sweep`, `/epic-audit` or `/release` SKILL.md against drift: that a script it invokes is
  actually called with the right arguments, that guard coverage stays complete as new call sites are
  added, that a state variable set in one section is still read correctly downstream, etc. The group
  is a **mix**: some (`test_bd_list_limit_gate.py`, `test_land_lock.py`,
  `test_land_skill_guard_coverage.py`) parse the Markdown and assert on the bash inside it; others
  (`test_isolation_guard.py`, `test_merge_precheck.py`, `test_release_bump.py`) drive the extracted
  `scripts/*.sh` library a SKILL.md delegates to, naming the SKILL.md only as the caller they protect
  — and are listed again under **git/worktree/land-loop infrastructure** below, from that angle.
- **`tests/_hookharness.py`** — the shared harness for the second Markdown-adjacent surface: the
  committed `PreToolUse(Bash)` hooks in `.claude/settings.json` (JSON, not Markdown, but the same
  "not reachable by a normal linter" problem — a hook is a shell one-liner embedded as a JSON string
  value). It extracts a hook's command by matching a substring and runs it through `/bin/sh -c`
  (**never** `bash -c` — the harness itself runs every hook under `/bin/sh`, which is `dash` on Linux,
  not bash; a test that used `bash -c` instead already shipped a `dash`-breaking construct once
  without catching it, `lode-9gm2`). `test_bd_deps_guard.py`, `test_gh_write_guard.py`,
  `test_sha_fabrication_guard.py`, and `test_trunk_write_guard.py` drive real hook guards through
  this harness rather than reimplementing the extraction three times over.
- **`tests/test_hook_guards_inventory.py`** — the global cross-check that every hook guard actually
  installed in `.claude/settings.json` has a corresponding test driving it through `_hookharness.py`,
  so a new hook can't be added without test coverage the same way an ordinary Python module would need
  a test file.
- **`tests/test_keybindings_doc.py`**, **`tests/test_check_docstring_refs.py`**,
  **`tests/test_check_links.py`**, **`tests/test_cli_help_corpus_gate.py`**,
  **`tests/test_decisions_supersession_markers.py`**, **`tests/test_decisions_no_silent_rewrite_guard.py`**,
  **`tests/test_docs_index_never_tracked.py`** — the general family of doc/prose-as-source-of-truth
  gates: these parse `docs/*.md` (not SKILL.md bash specifically) to keep a table, a link, a citation,
  or a supersession marker honest against the code or tickets it describes. Same underlying reason as
  the SKILL.md scanners — Markdown that encodes a real invariant is not something `ruff`/`mypy`/pytest's
  normal collection touches on its own; a dedicated parser is the only thing that can assert on it.

The common thread: **wherever prose carries an executable or load-bearing invariant, a scanner reads
that prose directly** rather than trusting it stays in sync by convention. `docs/agents-workflow.md`
and `docs/conventions.md` are the design-level record of *why* these invariants exist; the tests here
are what keeps them true as the files change.

## What else is here

The remaining ~130 files are ordinary tests, organized by what they cover:

- **Core save/retrieve pipeline** — `test_storage.py`, `test_versions.py`, `test_hashing.py`,
  `test_repository.py`, `test_chunking.py`, `test_embedding.py`, `test_lexical.py`,
  `test_retrieval.py`, `test_vectorstore.py`, `test_sql_ids.py`, `test_ids.py`,
  `test_notes_read.py` — the
  content-addressed, event-sourced note/version storage layer and the two-leg (lexical + vector)
  retrieval read side.
- **Cited Q&A** — `test_qa.py`, `test_cited_answer.py`, `test_answer.py`, `test_faithfulness.py`,
  `test_gate.py`, `test_citations_read.py`, `test_eval_harness.py`, `test_eval_golden.py`,
  `test_eval_seed.py`, `test_eval_live.py`, `test_skeleton_gate.py` — the citation-faithfulness gate
  and the eval harness that scores it (`test_eval_live.py` is a live-credentials integration test,
  self-skipping when `ANTHROPIC_API_KEY` is unset).
- **Enrichment / curation / staleness** — `test_enrich.py`, `test_enrichment_view.py`,
  `test_curation.py`, `test_display.py`, `test_staleness.py`, `test_redact.py`, `test_drawdown.py`,
  `test_capture_lag_diagnosis.py` — the Haiku-driven enrichment pass and the annotation/edge
  suppression, staleness, and redaction rules layered on top of it.
- **Async work queue** — `test_worker.py`, `test_jobs.py`, `test_reconcile.py`, `test_lock.py`,
  `test_backfill.py`, `test_progress.py`, `test_latency_probe.py` — the durable job queue, its worker
  loop, single-instance locking, and per-connector backfill.
- **External connectors** — `test_confluence.py`, `test_confluence_backfill.py`, `test_jira_fetch.py`,
  `test_jira_backfill.py`, `test_webfetch.py`, `test_fetch_outcome.py`, `test_externals.py`,
  `test_auth.py`, `test_no_egress_scope.py`, `test_egress.py` — Atlassian (Confluence/JIRA) and web
  connectors, the mirrored-snapshot write path, and the `no_egress` privacy tier.
- **LLM provider seam** — `test_llm_provider.py`, `test_tool_dispatch.py`, `test_tools.py`,
  `_anthropic_rig.py` — the vendor-neutral LLM call surface and the Ask tool set, plus the shared rig
  for driving real-SDK-shaped Anthropic Batches API responses in tests.
- **CLI** — `test_cli.py`, `test_cli_backfill.py`, `test_cli_console.py`, `test_cli_theme.py`,
  `test_cli_verify.py`, `test_cli_help_corpus_gate.py`, `test_config.py`, `test_logconfig.py` — the
  Typer CLI surface, its shared console/theme, and the settings module; `test_cli_help_corpus_gate.py`
  is a corpus gate enforcing `docs/conventions.md`'s `help=` length/format rules across every
  registered command.
- **TUI** — every `test_tui_*.py` file, plus `test_link_open.py` (pure-function tests for the
  open-link-under-cursor helper behind the TUI screens) — one test module per Textual screen or seam widget
  (capture, edit, browse, ask, tags, reconcile, config, help, quit-confirm, related-notes panel, the
  shared `LodeDataTable`/`LodeStatic` widgets, markdown syntax colouring, footer-width and dialog
  styling corpus gates), following `docs/conventions.md`'s one-screen/one-widget-per-module rule.
- **git/worktree/land-loop infrastructure** — `test_land_lock.py`, `test_land_merge_one.py`,
  `test_land_conflicts_state.py`, `test_land_state_load.py`, `test_merge_precheck.py`,
  `test_worktree_gc_classify.py`, `test_worktree_lock_stale.py`, `test_recycled_worktree_guard.py`,
  `test_isolation_guard.py`, `test_source_tree_guard.py`, `test_assert_main_checkout.py`,
  `test_trunk_write_guard.py`, `test_gh_write_guard.py`, `test_sha_fabrication_guard.py`,
  `test_bd_deps_guard.py`, `test_bd_dolt_push_guard.py`, `test_validate_sha40.py`,
  `test_validate_sha40_call_sites.py`, `test_precondition_guards.py`, `test_gate_lib.py`,
  `test_shell_quote_split_lib.py`, `test_dep_churn_lib.py`, `test_workflow_concurrency.py`,
  `test_beads_passive_exports.py` — tests for
  the `scripts/*.sh` libraries the `/code`/`/land`/`/sweep` skills delegate reusable logic to (see
  above), plus the committed `PreToolUse` hook guards.
- **`/sweep`, `/code`, `/epic-audit` skill plumbing** — `test_sweep_digest_id.py`,
  `test_sweep_new_ids_ordering.py`, `test_sweep_pipeline_label_roster_gate.py`,
  `test_sweep_source_query_failure.py`, `test_sweep_state_load.py`,
  `test_sweep_stranded_age_filter.py`, `test_code_concurrency_cap.py`, `test_epic_children_closed.py`,
  `test_epic_completion_check.py`, `test_epic_debate_gate.py`, `test_blocks_dependents.py` — regression
  pins for specific bug fixes to those skills' bash and for the bd dependency-edge invariants they
  rely on.
- **Release tooling** — `test_release_bump.py`, `test_release_latest_tag.py`,
  `test_model_cache_key_script.py`, `test_model_cache_identity.py`, `test_models_smoke.py`,
  `test_dep_churn_lib.py`, `test_deps_declared.py`, `test_noxfile_venv_tool.py`,
  `test_nox_session_inventory.py` — `scripts/release.sh`'s pieces, the fastembed model-cache identity
  pinned against drift, and dependency/nox-session hygiene gates.
- **docs-index tool** — `test_docs_index_build.py`, `test_docs_index_chunker.py`,
  `test_docs_index_query.py`, `test_docs_index_never_tracked.py` — the on-demand FTS5 lookup index
  over `docs/*.md` (`scripts/docs_index_*.py`) that `CLAUDE.md` directs "what did we decide about X"
  questions through, plus the gate keeping its index file untracked.
- **`docs/decisions.md` machinery** — `test_decisions_supersession_markers.py`,
  `test_decisions_no_silent_rewrite_guard.py`, `test_decisions_union_merge_driver.py` — the
  append-only supersession convention that file's own preamble mandates, and the git union-merge
  driver that makes concurrent appends to it merge without conflict.
- **Infrastructure the tests themselves depend on** — `conftest.py` (shared autouse fixtures: an
  isolated `$LODE_HOME` per test, ambient-env scrubbing, the network-egress guard, the jobs clock
  anchor, `pytest_configure`'s wrong-source-tree check), `test_conftest_color_scrub.py`,
  `test_conftest_jobs_clock_anchor.py`, `test_network_guard.py`, `test_source_tree_guard.py`,
  `_gitrepo.py` (shared helper for driving real throwaway git repos), `_hookharness.py` (above),
  `test_precondition_guards.py`, `test_validate_mermaid_gate.py`.

If a file isn't obviously covered by one of the groups above, its own module docstring is the
authoritative one-line description — every test module in this directory carries one; that's the
fastest way to confirm current coverage without re-deriving it from this file, which will drift.
