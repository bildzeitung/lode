# lode — Stack (decided)

*(§10)* Chosen at founding; rationale where non-obvious. Most choices follow the existing
job-harness ecosystem (Python + Textual + Typer + SQLite) so there's no new framework risk. This
is the storage realization of the ownership boundary and data shape in [storage.md](storage.md).

| Layer | Choice | Notes |
|---|---|---|
| Language | **Python** | Richest LLM/embedding tooling; matches the existing harness |
| Versioning | **setuptools-scm** | The git tag is the version source of truth, no literal to hand-edit — see [release.md](release.md) |
| TUI | **Textual** | Already a proven front-end in the sibling project. Ships with **`textual[syntax]`** as a hard dependency (not an optional extra) for live markdown colouring in the note-body `TextArea`s, pulling in ~15 tree-sitter grammar packages — see [editing.md](editing.md) for the colouring scope (block-level only), the fallback behavior, and the rationale |
| CLI | **Typer** | Repo convention (never argparse). **`is_flag`/`flag_value` tri-state options are non-functional** — typer vendors its own copy of click's argument parser (`typer/_click/`, a module distinct from the real `click` dependency) whose `_get_value_from_state` lacks real click's `_flag_needs_value` fallback. Consequence — verified against this project's installed typer (`0.26.8` at discovery in `lode-l38d.8`, reconfirmed against `0.27.0`), reproduced side by side against real click (which handles both correctly): `typer.Option(is_flag=False, flag_value=...)` errors on a bare trailing option (`Option '--file' requires an argument`), and when a following token *is* present it is swallowed even if it is itself another flag (`--file --all` parses as `file='--all'`, not two separate flags). Typer's own `flag_value`/`is_flag` docstring independently confirms it: "inherited from Click and supported for compatibility ... not fully functional, and will likely be removed in future versions." **Sanctioned alternative: split into two fully-supported options** (a bool flag + a `Path` option) rather than one tri-state option — as `lode-l38d.8` landed (`--file`/`--dir`) |
| CLI rendering | **`rich`** | CLI colour + terminal-width rendering (E-UX2, `lode-l38d.1`). Already a hard runtime dependency *in practice* — pulled in transitively by Textual (built on rich, same authors) and Typer — but undeclared, which breaks silently the day either drops it; now declared explicitly in `[project].dependencies`. One shared `Console` in `cli.py`, so colour is decided once per process instead of hand-rolled per command. **No test seam** (no `force_terminal`, no accessor to monkeypatch): colour tickets assert only the negative path; the positive case is verified by eye. **The detection is frozen at import**, not per command — `Console()` reads its TTY check *and* `NO_COLOR` at construction, and at module scope that is import time. Correct for real use (piping replaces stdout before `cli.py` is imported) but it constrains the tests: colour is off under `CliRunner` because *pytest's default capture* already replaced stdout by import time — not because CliRunner's sink is not a TTY — so `pytest -s` from a terminal freezes the decision the other way and leaks ANSI into captured output; and `monkeypatch.setenv("NO_COLOR", …)` after import is read too late to do anything, so that path must be asserted in a **subprocess** carrying `NO_COLOR=1`. Mechanism verified in `lode-l38d.1`. Accepted residual risk: a regression that silently disables colour everywhere still passes the gates (it is user-visible on first use). The shared `Console` carries one shared rich `Theme` (`CLI_THEME`, `lode-l38d.11`), with SEMANTIC style names (`note_id`, `date`, `warn`, `danger`, `ok`, `table.header`) rather than colour literals — split out from `lode-l38d.1` because its four colour/table consumers (`lode-l38d.4`/`.5`/`.6`/`.10`) all depend only on `.1` and so reach the ready frontier together as parallel, non-coordinating producers; deciding the palette once, here, removes the need for them to coordinate it themselves. The palette is declared as a plain dict (`CLI_STYLES`) that the `Theme` is built *from*, because `Theme.__init__` **destroys the declaration** — it copies rich's `DEFAULT_STYLES` (`inherit=True` is the default, and wanted: rich's own `repr.*`/`progress.*`/traceback styles must keep working underneath ours) and `.update()`s ours on top, so a name whose value equals rich's default is indistinguishable on the constructed `Theme` from one never declared. That is not hypothetical: `table.header` deliberately restates rich's own default (`bold`, which rich's `Table` already applies via its default `header_style="table.header"`), declared anyway so the palette has one source of truth for `lode-l38d.4`, which cannot ask. Consequence for tests: assert the palette against `CLI_STYLES`, never against `CLI_THEME.styles` — the latter is merged over ~150 rich defaults and stays green with an entry deleted (found by `lode-l38d.11`'s technical review, whose tests originally did exactly that). **`highlight=False` is hoisted onto the shared `Console` itself** (`lode-re0s`), not left per-call-site: rich's `Console` runs its `ReprHighlighter` over every plain string by default, injecting `repr.*` styles outside `CLI_STYLES` — verified against rich 15.0.0 to shred a rendered date into mismatched bold-cyan/dim/bold-green spans and to recolour numbers/IPs/etc. inside a note's own text. Every consumer wants it off, rich `Table`s never run it regardless, so centralising it has no blast radius; a per-call `highlight=True` still overrides it if ever needed. Same "no public accessor" shape as the rest of this row — pin it via the private `Console._highlight`, not an assertion on rendered output |
| **SQLite store** (one file) | **SQLite** | A single **container** file. Holds the **irreplaceable** rows — owned content (`notes`/`versions`/`externals`/`snapshots`) **and** user curation (`annotations`/`edges` where `source = user`) — *and*, in the same file, rebuildable cache (**FTS5**, `source = ai` rows, `passages`) + operational `jobs`. The partition is by **rows / value, not by file** (see [below](#the-partition-is-by-rows-not-by-file)). Tiny, durable, **backup = copy the file** (a harmless *superset* of the irreplaceable set) |
| **Regenerable cache** | **LanceDB** (vectors) + **networkx** (graph, in-memory) | Disposable, rebuildable from the notes. LanceDB: columnar on-disk embeddings with a real ANN index and metadata filtering (its native hybrid is **unused** — lexical stays in FTS5; fusion is app-side RRF, see [retrieval.md](retrieval.md)). Graph traversal runs in-memory via networkx over the edge rows — no graph server. AI annotation/edge rows live in SQLite alongside the rest. Behind a thin **repository interface**, so the cache engine is swappable (sqlite-vec is the simpler fallback-down) |
| Embeddings | **Local, on-machine** | Open model via fastembed/ONNX (`nomic-ai/nomic-embed-text-v1.5`, **768-dim** — pinned + verified in `lode-txh.6`) — CPU-only, no torch. **Loaded in-process via `fastembed` (a thin wrapper over `onnxruntime` + tokenizers) — there is no model server or daemon; this is *not* Ollama.** The reranker and faithfulness-NLI models below run the same way, in the same process. **Chosen specifically to honor [privacy](externals.md#privacy-consequence-of-aggregation)**: note/email/ticket content is never sent off-box *for indexing*. The resulting vectors land in LanceDB. Accepts slightly lower retrieval quality + a bundled model file (~100–500MB) in exchange |
| Reranker | **Local cross-encoder** (`BAAI/bge-reranker-base`) | First-class retrieval stage ([retrieval.md](retrieval.md)), wired in v1 behind a toggle. Runs on the **same ONNX runtime** as embeddings via `fastembed` — no new stack, content stays on-box. (`fastembed` does not ship `bge-reranker-v2-m3`; `bge-reranker-base` is the loadable bge-family pick — verified in `lode-txh.6`.) Biggest single quality lever for cited Q&A; model/threshold tuning deferred until there's a corpus ([decisions.md](decisions.md)) |
| Faithfulness NLI | **Local cross-encoder repurposed** (`BAAI/bge-reranker-base` via `fastembed`'s `TextCrossEncoder`) | Entailment leg of the [faithfulness gate](retrieval.md#faithfulness-verify-citations-dont-just-require-them): scores whether cited spans jointly entail a synthesized claim, so multi-note synthesis is answered rather than refused. `fastembed` ships **no** dedicated NLI model, so the cross-encoder is repurposed as the entailment scorer — same **ONNX runtime**, on-box, no separate loader (verified in `lode-txh.6`). Ships in v1 **conservative and fail-closed**; the model + acceptance **threshold ship untuned** and are revisited against the eval harness ([decisions.md](decisions.md)) |
| Enrichment LLM | Provider-selected via `llm_provider` ([LLM provider seam](#llm-provider-seam-decided-lode-568v1), `lode-568v.2`/`.3`); default **Anthropic Claude Haiku 4.5** (`claude-haiku-4-5`, $1/$5 per Mtok), or an OpenAI/Azure deployment when `llm_provider = "openai"` | High-volume background tagging/extraction. Use **structured outputs** so the derived layer gets validated JSON. A **fresh note enriches interactively** (one immediate call) for promptness; **bulk / backfill / re-enrichment** goes through the provider's batch path — Anthropic's **Batches API** (50% off, non-interactive) under the default provider, or serialized sequential calls under a provider with no batch API (`lode-568v.3`). Driven by the durable [work queue](storage.md#the-async-work-queue); submitted batch handles are persisted so a restart resumes rather than resubmits. **`no_egress` notes are skipped** (never enriched); every send is redacted-before-egress and recorded in the [egress log](externals.md#privacy-consequence-of-aggregation) |
| Q&A LLM | Provider-selected via `llm_provider` (same seam); default **Anthropic Claude Sonnet 4.6** (`claude-sonnet-4-6`, $3/$15) with **Opus 5** (`claude-opus-5`, $5/$25) as a "think harder" toggle, or OpenAI/Azure deployments under `qa_llm`/`qa_think_harder_llm` when `llm_provider = "openai"` | Low-volume, interactive, quality-sensitive synthesis. Returns **structured claims**, each pinned to a verbatim span of a specific version; a [faithfulness gate](retrieval.md#faithfulness-verify-citations-dont-just-require-them) verifies the evidence and abstains rather than emit an unsupported claim — citations are enforced by *verification*, not just by the response schema. **`no_egress` passages are excluded from the cloud context** (cited as "withheld from synthesis"); the context sent is redacted-before-egress and recorded in the [egress log](externals.md#privacy-consequence-of-aggregation) |
| Web-fetch HTTP client | **`httpx`** | First connector (E12 web draw-down, `lode-w0h.1`) — synchronous GET with an explicit `follow_redirects`/`max_redirects` cap and a typed exception hierarchy (`TooManyRedirects`/`TimeoutException`/`NetworkError`) that maps onto the fetch-outcome taxonomy ([externals.md](externals.md#draw-down-rules)). Chosen over `requests` purely on maintenance status — `requests` is in long-term maintenance mode, httpx is the actively developed equivalent with the same sync call shape (both have a redirect cap and typed exceptions; that pair differentiates only against stdlib). Chosen over stdlib `urllib.request` because its redirect cap is a hardcoded `HTTPRedirectHandler.max_redirections = 10`, not a per-request knob, so the `fetch_max_redirects` setting could not be honored without subclassing |
| Web-fetch readability extraction | **`trafilatura`** | Same ticket. Named directly in the fetch-outcome decision: `extract()` returns `str \| None`, and `None` on failed/empty extraction *is* the taxonomy's testable "not real content" signal, combined with a configured length floor for short-but-non-`None` teasers (paywalls). Verified locally against synthetic JS-shell/paywall/article fixtures during the build. Chosen over `readability-lxml` (stale, weaker boilerplate removal) and `boilerpy3` (thinner API) |

---

## Why a split store

*(decided after evaluating a unified Oracle AI Database 26ai, plus SQLite+sqlite-vec, SurrealDB,
FalkorDB, Neo4j, Postgres+pgvector+AGE)*

The ownership boundary ([storage.md](storage.md#the-ownership-boundary)) already partitions the data
by *value*, so the storage follows it. The irreplaceable set is **tiny and structurally trivial**
but must be durable and trivial to back up — SQLite is the ideal fit (one file, atomic,
restore-anywhere). The cache is **heavy but disposable**, so it optimizes for *retrieval quality and
feature fit*, not durability — which frees the pick to the best embedded tools (LanceDB + networkx)
with no server, licensing, or unpatched-security risk. (Not *all* cache leaves SQLite: FTS5, the
`source = ai` rows, and `passages` co-reside there for transactional and FTS-next-to-`versions`
reasons — so the boundary is by **value / rows, not strictly by engine**; see
[the partition is by rows](#the-partition-is-by-rows-not-by-file).)

A single unified engine (the original Oracle 26ai choice) was rejected because it inverted that
match: it put the **heaviest, least-durability-critical machinery under the most sensitive data**.
Oracle Free is unsupported/unpatched (security included) on a box aggregating email + tickets + repo
contents; it makes backup a full-DB dump instead of a file copy; and it front-loads the heaviest
yak-shaving onto an MVP (build step 1, [design.md](design.md) §7) that needs none of its
differentiators (the note↔note graph fits in memory; entity extraction is the enrichment LLM's job —
with provenance — not a DB black box).

---

## The derived layer is not uniformly disposable

Three rebuildable tiers, plus one non-regenerable exception that belongs with the irreplaceable set:

| Derived item | Rebuilt by | Regeneration cost |
|---|---|---|
| Embeddings | Local CPU model over head nodes | **Cheap** — minutes for thousands of notes, tens of minutes for ~100k. No dollars, no network. |
| Lexical (FTS5) + explicit edges | Deterministic re-parse | **Trivial** — pure computation, no model. |
| AI annotations + inferred edges | The enrichment LLM (default: Claude Haiku via Anthropic's Batches API; a provider without a batch API serializes instead, [LLM provider seam](#llm-provider-seam-decided-lode-568v1)) | **Real $ + hours** — ~tens of dollars per ~10k notes, non-interactive. Not prohibitive, but not free. |
| **User curation** (`source = user`) | — (not derived from anything) | **Not regenerable.** A fixed tag, a confirmed or deleted link — genuine user decisions. Stored with the irreplaceable set in SQLite. |

So "drop the derived layer and lose nothing" holds only for the first three tiers; user curation is
irreplaceable.

---

## The partition is by rows, not by file

The value boundary (§3, irreplaceable vs regenerable) and the engine boundary (SQLite vs
LanceDB/networkx) do **not** coincide. The SQLite file is a *container* that holds irreplaceable
rows **and** rebuildable cache (FTS5, `source = ai` rows, `passages`) **and** operational `jobs`.
So the partition is **by rows / value, not by file** — and the docs say so rather than implying the
file equals the irreplaceable set.

It stays a **single file** (not split into `core.db` + `cache.db`) for three concrete reasons:

- **Atomic enqueue.** "Write version row + enqueue its derive jobs" must be one transaction
  ([storage.md](storage.md#the-async-work-queue)); across two attached DBs in WAL mode commit is
  **not** atomic, which would break that invariant.
- **FTS5 sits next to `versions`.** An external-content FTS5 index references `versions.body` to
  avoid duplicating text; that reference doesn't cross database files cleanly.
- **Nuking the cache needs no file boundary** — it's a `DROP`/`DELETE` of the cache tables within
  the one file, not a file deletion.

**Backup, stated honestly:**

- **`cp lode.db` is the default** — a *superset* backup: it includes rebuildable cache (harmless
  extra bytes you could have regenerated). Trivial and always correct.
- **A minimal / archival irreplaceable-only dump** is a *row-level* export (owned tables +
  `source = user` rows); restore rebuilds the cache via the reconciliation scan
  ([storage.md](storage.md#the-async-work-queue)) + re-embed/re-enrich. Deferred — the superset copy
  is correct and free; the minimal export is an optimization ([decisions.md](decisions.md)).
- **Restore is robustly sloppy.** A restored file may carry *stale* cache (AI rows from an old
  `prompt_ver`, FTS rows, a dangling `batch_handle`); all of it is absorbed by structural staleness
  + reconciliation, so a superset restore is safe.

---

## Dependency locking (lode-g2741)

Two files, two jobs — **never pin the same thing in both**:

- **`pyproject.toml` is the INTENT layer.** Ranges and floors, not exact versions. A real lower
  (or upper) bound appears here only where a version demonstrably matters — e.g. `trafilatura
  >=2.1,<2.2`, bounded so the lock below always resolves 2.1.0, the version `lode-g274.3`'s
  characterization fixtures assert extraction behavior against. Moving off 2.1.0 is a deliberate
  act: bump the ceiling and re-baseline `lode-g274.3` first, not an incidental side effect of an
  unrelated dependency bump.
- **`requirements.lock` is the ONLY place exact versions live.** A committed, fully-transitive,
  hash-verified (`uv pip compile --generate-hashes`) lock of the **runtime** dependency set only —
  the `dev` extra is deliberately *not* locked (epic `lode-g274` OQ#1: dev-tool drift is not this
  lock's job; the gates themselves, run at HEAD, are the backstop for that). Regenerated only via
  `scripts/compile-lock.sh` (`scripts/update-deps.sh`, `lode-g274.2`) — never hand-edited; the
  hashes make hand-editing impractical anyway.

  **Single-tool exception: `ruff==0.16.0` is pinned in the `dev` extra.** This is a deliberate,
  maintainer-approved *partial rescission* of the unlocked-`dev` policy above, scoped to ruff alone.
  `lode-umh2` established the carve-out at `ruff==0.15.22` as a stopgap; `lode-ju25` then re-pinned
  it to `0.16.0`, and at that point it stopped being a stopgap awaiting a follow-up decision and
  became permanent policy. Ruff 0.16 (released 2026-07-23) enforced a much larger default rule set
  than the version that last certified trunk clean, turning `nox -t fix` red repo-wide for every
  producer with no regression in any one branch's own code; the pin is the only thing that keeps a
  repeat of that from arriving unannounced. *Which* rules that pinned ruff enforces is a separate
  question, settled in [Ruff's lint rule set](#ruffs-lint-rule-set-settled-lode-cs5u) below.

  **The pin only constrains what `uv` installs, not what the gate runs (`lode-0yfn`).**
  `noxfile.py` sets `default_venv_backend = "none"`, so a session inherits whatever PATH the
  invoking shell has. A stale system-wide tool sitting earlier on PATH than the project's own
  `./venv/bin` (e.g. a pip `--user` / pipx `ruff`) then silently shadows the pinned copy — the gate
  runs a *different* ruff than the one pinned above and still reports success, with no signal that
  anything was skipped (reproduced directly: an ambient `~/.local/bin/ruff` 0.15.11 masking the
  then-pinned `0.15.22`, which silently skipped ruff-format's markdown Python-fence reformatting
  while `nox -t fix` still exited 0). Fixed by resolving every dev-extra tool a session shells out
  to — `ruff`, `pytest`, `shellcheck`, `python` — to its explicit on-disk path under `./venv/bin`,
  derived from `noxfile.py`'s own location rather than searched for on PATH (`noxfile.py`'s
  `_venv_tool` helper), so ambient PATH order cannot substitute a different binary; the session
  fails loudly instead if the project venv (or the tool inside it) is missing. Because it fails
  closed, **any CI workflow running one of those sessions must build `./venv` first** — installing
  the dev extra into the runner's ambient interpreter is no longer enough, which is why
  `coverage.yml` calls `scripts/python-init.sh` instead of `pip install -e '.[dev]'`. Deliberately
  **not** applied to the `build` session, which shells out to ambient `python -m build` on purpose —
  `build.yml`/`release.yml` run it with no `./venv` at all, since packaging resolves its own
  isolated PEP 517 env and never touches the dev-extra/lock tools this guarantee exists to pin —
  nor to `lock_currency`, which resolves `uv` itself (a separate, system-wide tool never installed
  into `./venv`, already checked explicitly and failed closed if absent, `lode-sys4`).

`./scripts/python-init.sh` installs from the lock by default, with `--require-hashes` so a hash
mismatch **fails** the install rather than warning. `-e .` (the local package, editable) and
`--require-hashes` are mutually exclusive in one pip/uv invocation, so the dependency install splits
in two: a **lock step** (hash-verified runtime deps from `requirements.lock`), then a **dev-extra
step** (the local package editable together with the `dev` extra, `-e '.[dev]'`, resolved fresh from
`pyproject.toml`). The dev-extra step does re-resolve the whole graph, but the lock step's pins
already satisfy `pyproject.toml`'s ranges and uv keeps an already-installed satisfying version — so
it adds the dev-only packages on top without moving anything the lock step hash-verified. That was
reproduced rather than argued (`lode-xo99`): a locked venv built with and without an extra
`-e . --no-deps` step in between came out with the same package set, the same runtime pins, and the
same resolved `lode` source path either way, so that step was deleted as dead work. `--unlocked`
skips the lock and resolves everything fresh from `pyproject.toml` instead — the deliberate "what
would we get today" escape hatch for regenerating the lock or probing an upstream bump before
committing to it.

**The pip-refresh half of that same install (`uv pip install -U pip`) is different — cosmetic, but
not dead (`lode-hfaz`).** Unlike the deleted `-e . --no-deps` step above, it measurably changes the
installed package set: it bumps the venv's own pip (26.1.1 → 26.1.2 from ensurepip's bundle in the
reproduction run), the one difference in an otherwise byte-identical `uv pip list --format=freeze`
with vs. without it. That holds only while ensurepip's bundle trails the current pip release — the
usual state, not a guaranteed one, so a re-run finding *no* difference means the window closed, not
that the method was wrong. Nothing ever installs *through* the upgraded pip: the venv's pip is
invoked in exactly one place, the `pip install -U uv` that opens this same sequence (and its
`--unlocked` twin in `python-init.sh`, kept for the same reason), so the upgrade's only effect is
suppressing pip's own "a new release is available" notice the next time that opening command runs.
Everything that builds `./venv` builds it from scratch — a first-time `python-init.sh`, both CI legs
that call it (`tests.yml` and `coverage.yml`; neither caches `./venv`), and `update-deps.sh`'s
`rebuild_venv` (`rm -rf ./venv` first, every time) — and so starts from ensurepip's bundle regardless
of history, buying nothing there. It only pays off re-running `python-init.sh` a second time against
a `./venv` that survived from a prior run: verified directly, `python -m venv` on an *existing* venv
directory does not reset an already-upgraded pip. Narrow, but real — kept.

**Both CI legs that install lode's deps install from the lock (`lode-7byn`).** `tests.yml`'s
`tests` job has since `lode-g274.6`; `coverage.yml` was the holdout, for historical reasons only.
It landed a day *before* `requirements.lock` existed (`lode-qxdn.3`), so its fresh
`pip install -e '.[dev]'` was the only option there was, never a decision to measure a different
dependency set — that commit's stated goal was parity with `nox -s tests` on *which tests run*
("no marker filter, slow tests included … the suite the tests badge backs, not a narrower one"),
and it said nothing either way about dependencies. `lode-g274.6` then left the leg alone on scope
grounds ("report-only … neither is in this ticket's scope"), and `lode-0yfn`'s review preserved the
fresh resolve deliberately rather than decide it. With no affirmative reason anywhere on record for
the coverage number to describe a *different* dependency set than the tests badge, `lode-7byn`
dropped `--unlocked`: both legs now run the identical install, so a coverage percentage is
reproducible from committed bytes instead of from whatever resolved on the day it ran.

**What that parity does not cover.** The lock is the runtime set only, so both legs still resolve
`pytest`/`pytest-cov`/`coverage` fresh from the `dev` extra (the dev-extra step above) — `lode-7byn`
pinned the code under measurement, not the tools doing the measuring. The counter-case for resolving
fresh here (an upstream runtime bump moving the coverage number before the lock is bumped) is real but
toothless on this leg: `coverage.yml` enforces no threshold, so such a drift fails nothing and
attributes nothing — and no CI signal fires on an upstream runtime release either way.
[`lock-currency`](#the-lock-gen-command-is-derived-from-python-version-not-hard-coded-lode-sys4)
only catches a lock that has fallen *behind* a `pyproject.toml` constraint change, never one that has
fallen behind PyPI — uv's preference seeding, described in that same section. Moving the runtime set
forward is always a deliberate `scripts/update-deps.sh` run, never something CI notices on its own.

### Ruff's lint rule set (settled `lode-cs5u`)

**The model: ruff's full default set, minus a shrinking `[tool.ruff.lint] ignore` list.**
`lode-ju25` originally proposed the opposite shape — an explicit opt-in
`select = ["E4", "E7", "E9", "F"]` — and is closed with that text still reading as its
authoritative decision; it is superseded. The maintainer's call (2026-07-25): reducing `ignore` is
the simpler route to a codebase that is lint-clean against *all* of ruff's default checks.
Consequences of this model, stated honestly:

- **`ignore` is a work queue, not a policy.** Every entry in `[tool.ruff.lint] ignore`
  (`pyproject.toml`) names a rule with outstanding violations somewhere in the tree, removed as the
  epic that owns adopting it (`lode-cs5u`) fixes those sites. The terminal state is `ignore = []` —
  there are no permanent exclusions.
- **This model does not give back `lode-ju25`'s churn-proofing.** A future ruff default-set
  expansion can turn `nox -t fix` red repo-wide again, exactly as 0.16 did. The `ruff==0.16.0` pin
  above is the only mitigation, and it works only because an expansion can now arrive solely via a
  deliberate version bump, never a `dev`-extra resolve.

**Markdown Python-fence formatting is accepted (`lode-ju25` decision 3).** `ruff format` also
formats the Python code fences embedded in `docs/*.md`; the resulting churn to those fences is
wanted, not something to revert.

**B008 (`function-call-in-default-argument`) on `typer.Option`/`typer.Argument`: adopted with no
carve-out, via the `Annotated` idiom (`lode-up58`).** `lode-cs5u.3` adopted B008 by hoisting the sites
it flagged to module-level singleton defaults — but B008 only flags a default whose parameter
annotation is a known-immutable builtin (`bool`, `str` were skipped; `Path` and enum types were
flagged), so `src/lode/cli.py` ended up split between hoisted and inline
`typer.Option(...)`/`typer.Argument(...)` defaults by a heuristic invisible at the call site, and the
split would have ratcheted with every future `Path`- or enum-annotated option added. The alternative
`extend-immutable-calls = ["typer.Option", "typer.Argument"]` in `pyproject.toml` would have silenced
B008 correctly (ruff's own docs name CLI frameworks as the false-positive case) but only removes the
lint, not the call-site inconsistency, and is a per-rule semantics carve-out this file's own "`ignore`
is a work queue, not a policy" bar was written against.

**The decided fix:** every Typer CLI in this repo uses `Annotated[<type>, typer.Option(...)]` —
Typer's current idiom — so the construction no longer lives in the default-argument position at all
and B008 has nothing left to flag, no hoist and no config carve-out, now or for any option added
later. The previously-hoisted single-use singletons (`_JOBS_STATUS_OPTION`, `_EGRESS_PURPOSE_OPTION`,
`_DUMP_HTML_DIR_OPTION` in `cli.py`; `_ROOT_OPTION` in `check_links.py`) were unwound back to their
call sites. The genuinely-shared `_DEBUG_OPTION`/`_DB_OPTION` (used across many commands, not hoisted
for lint) became shared `Annotated` type aliases (`_DebugOption`, `_DbOption`) — same sharing, same
reason, new idiom. The forward-binding half of this is a style fiat, so it also lives in
[`conventions.md`](conventions.md), which is `@import`ed into every producer and reviewer's context;
the reasoning stays here.

### The lock-gen command is derived from `.python-version`, not hard-coded (lode-sys4)

**Root cause of the original flap (`lode-gyag`):** `uv pip compile` does **not** read
`.python-version` — it resolves against whichever interpreter it happens to discover on the
invoking machine. Some transitive deps carry Python-version markers (`lancedb`'s
`overrides>=0.7 ; python_full_version < "3.12"`; `anyio`'s marker-gated `typing_extensions ; python_version
< "3.13"`), so the SAME `pyproject.toml` resolves a *different* lock depending on whether the
generating machine's default Python was, say, 3.11 or 3.14. CI validates against 3.14 (this repo's
`.python-version`), so a lock generated on an older interpreter diffed red against CI's recompile —
not because a dependency actually changed, but because of *which Python resolved it*.

**The fix:** every lock-gen invocation passes `--python-version "$(cat .python-version)"` explicitly,
so the resolution target is always this repo's single source of truth for its interpreter, regardless
of whatever Python happens to be default on the machine running the command. This lives in exactly
**one** place — `scripts/compile-lock.sh` — which every caller below invokes rather than keeping its
own copy of the `uv pip compile` command string:

- `scripts/update-deps.sh` — the sanctioned way to move `requirements.lock`
  (`lode-g274.2`/`lode-fdjr`). Its two flows are a bare invocation for the whole set and
  `--package NAME` for one package (full usage:
  [onboarding.md](onboarding.md#updating-dependencies)); the corresponding `--upgrade` /
  `--upgrade-package NAME` go *down* to `compile-lock.sh` — not flags `update-deps.sh` accepts.
- **CI enforcement (`lode-g274.6` / `lode-sys4`):** `tests.yml`'s `tests` job installs from
  `requirements.lock` itself (via `scripts/python-init.sh`, the same install path a developer runs),
  and a separate, independent `lock-currency` job in the same workflow verifies the lock is current —
  it runs `scripts/compile-lock.sh -o requirements.lock`, **in place** against the just-checked-out
  committed lock. uv feeds an existing output file's own pins back to the resolver as its *preference*
  set by default (only `--upgrade`/`-U` ignores them), so the resolution only moves when a
  `pyproject.toml` constraint forces it — an upstream release alone reproduces the committed lock
  byte-for-byte, and `git diff --exit-code requirements.lock` catches any real drift. `build.yml`
  never installs lode's runtime deps at all (`python -m build` resolves in its own isolated env), so
  the lock is irrelevant there. `coverage.yml` installs *from* the lock (`lode-7byn`) but does not
  re-verify its currency: being report-only (`lode-qxdn.3`, no merge-gate status), it leaves that to
  the single `lock-currency` job here — one commit, one currency check.
- **Local pre-flight (`lode-sys4`):** `nox -s lock_currency` (`noxfile.py`) is the same check,
  runnable on any dev machine — it seeds a scratch copy with the committed lock (mirroring CI's
  in-place recompile so the preference-seeding behaves identically), recompiles it via
  `scripts/compile-lock.sh`, and diffs the result against the committed file. Deliberately kept out
  of the default `nox` session set (same reasoning as `eval`/`build`: it needs network to resolve
  against PyPI, so a bare `nox` / `nox -s tests` stays offline). **`/land` runs it** as part of its
  combined re-gate (`.claude/skills/land/SKILL.md`, alongside `nox -t fix`/`nox -s tests`) and in its
  per-branch isolation-replay loop — so a stale lock is caught locally, by the single trunk-writer,
  before the public CI badge is the only thing that catches it.
- **Offline / `uv`-absent behaviour: fails closed, and fails *distinguishably*.**
  `scripts/compile-lock.sh` exits non-zero with an explicit message if `uv` is not on `PATH`, rather
  than silently skipping the check. A stale lock landing unnoticed because a local check was quietly
  skipped is worse than a noisy failure that tells a developer to install `uv`. But failing closed is
  only half of `lode-9i2p`'s rule, and the half that is easy to get wrong is the other one — so
  `nox -s lock_currency` splits its own non-zero into two statuses, the same contract
  `scripts/validate-mermaid.sh` already carries:
  - **exit 1 — CONTENT.** The committed lock genuinely disagrees with what `pyproject.toml` resolves
    to. Some diff caused it; `/land` may attribute it to a branch, isolate, and bounce.
  - **exit 2 — MACHINE.** The gate could not run at all: `uv` absent, or `compile-lock.sh` unable to
    resolve (PyPI unreachable, a 5xx, DNS). Nothing about any branch's content failed, so `/land`
    stops the pass and surfaces it as a human decision instead of isolating. Without this split, a
    transient PyPI blip on the lander would bounce — and delete — every reviewed branch in the pass,
    each with a fabricated "stale lock" finding, which is precisely the failure `lode-9i2p` was filed
    to prevent. nox collapses every ordinary session failure to exit 1, so the session leaves the
    process directly (`sys.exit`) for the machine-fault path.

  This gate needs that distinction more than the offline default set does, not less: `nox -t
  fix`/`nox -s tests` are offline once the model cache is warm, whereas `lock_currency` requires `uv`
  and a reachable PyPI on **every** invocation. CI's `lock-currency` job installs `uv` itself first,
  so the uv-absent path only bites a developer machine or `/land`'s local pre-flight — the public CI
  badge still catches a stale lock in that case, just later.
- **Attribution needs a baseline, not just an exit code (`lode-sys4`, extended to `nox -s tests` by
  `lode-kq4v`).** `/land`'s isolation-replay loop finds a culprit by merging the accepted branches
  one at a time and blaming the one that turns the gate red. That is **not** sound for either gate
  taken unconditionally — `nox -s tests` does *not* ask a question about the tree alone, despite
  once being recorded here as if it did: it is sensitive to ambient env vars a landing session's own
  shell can carry, and `lode-kq4v` observed exactly that in production — an ambient `FORCE_COLOR=3`
  in the landing session's environment (never set anywhere in this repo) froze rich's `Console()`
  colour detection at import (`lode-xgaa`'s mechanism) and reddened 6 CLI tests on a bare, unmodified
  `origin/trunk` with no branch involved at all. `lock_currency` fails the same soundness test for a
  different reason: it asks whether the committed lock is a fixed point of the tree **plus the
  ambient `uv` plus today's PyPI** — an answer that can flip with no branch involved (a `uv` release
  that changes the emitted format; `uv` is installed unpinned via `pip install -U uv`, so the
  lander's resolver can differ from the one that produced the committed lock). So `/land` runs
  **both** gates once on bare `origin/trunk` before entering the loop: red on either one there means
  the failure predates every branch in the set and is not attributable to any of them — stop the
  pass, don't isolate. (`tests/conftest.py` also scrubs the specific ambient colour/tty env vars
  rich reads — `FORCE_COLOR`/`NO_COLOR`/`TTY_COMPATIBLE`/`TTY_INTERACTIVE` — at collection time for
  every pytest invocation, closing the root cause `lode-kq4v` found; the baseline here is the
  independent blast-radius fix, so `/land` stays safe even against a *different* source of
  tree-alone-defying redness nobody has scrubbed yet.)

The cache is never *required* in a backup — losing it costs a rebuild, never data. Optionally
snapshot just the LLM tier of the cache to skip the dollars + hours of re-enrichment on restore
([decisions.md](decisions.md)) — an optimization, not a correctness need.

**Keep the cache behind a repository interface.** The [data shape](storage.md#data-shape-sketch) is
engine-agnostic; the access layer hides the cache engine so LanceDB can be swapped (sqlite-vec is
the simpler fallback-down) without touching the core.

**Embeddings reality check:** embeddings are **local-only regardless of which cloud LLM vendor is
configured** — Anthropic in particular has no first-party embeddings API, but even a vendor that does
offer one (e.g. OpenAI) doesn't change the decision: local embedding is a deliberate [privacy
principle](externals.md#privacy-consequence-of-aggregation), not an availability accident, and stays
out of scope for the vendor-neutral seam below (epic scope, decided 2026-07-22). LanceDB just stores
the resulting local vectors.

**Auth:** no hardcoded API key for any provider. Anthropic resolves via the SDK's own chain (env var,
then an `ant auth login` profile, then workload-identity federation), same as the harness; OpenAI/Azure
resolves via `OPENAI_API_KEY` / `AZURE_OPENAI_API_KEY` (see [LLM provider seam](#llm-provider-seam-decided-lode-568v1)
below). If nothing resolves, fail gracefully with an actionable, provider-appropriate message (no
traceback) and log the detail.

**Model-tier split mirrors the harness:** cheap/deterministic high-volume work on the cheaper tier
(default: Claude Haiku); judgment-sensitive synthesis on a stronger tier (default: Claude Sonnet, with
Opus as a "think harder" toggle) — now provider-portable via `ModelTier` ([§6](#6-config-shape)), so
the same split holds under an OpenAI/Azure deployment.

---

## LLM provider seam (decided, lode-568v.1)

`lode-568v` (epic: support LLM vendors outside Anthropic — OpenAI via Azure) needs a vendor-neutral
seam over the three cloud-LLM call surfaces before any provider code lands. This section is that
seam, pinned design-first so `lode-568v.2` (Anthropic behind the seam, zero behavior change) and
`lode-568v.3` (OpenAI-via-Azure behind the seam) build against one decided contract rather than
inventing it under implementation pressure. **LLM only** — embeddings/reranker/NLI stay local-only
and untouched (epic scope, decided 2026-07-22).

### Module home, and Protocol vs ABC

The seam lives in a **new module, `src/lode/llm_provider.py`** — not folded into `src/lode/auth.py`.
`auth.py`'s own docstring already treats staying cheap to import as load-bearing (`lode-4q97`):
`import anthropic` is deferred inside `build_client()` because most callers (e.g.
`lode.worker.drain`, unconditionally) import the module only to *catch* `AuthError`, on paths that
may never touch Anthropic at all. A seam that can construct **either** vendor's SDK client behind
one factory has strictly more reason to keep that import discipline, and a fresh module keeps it
from being re-litigated every time a provider is added. `auth.py`'s exact fate (kept as an internal
credential-resolution helper the `AnthropicProvider` calls into, vs. absorbed wholesale) is left to
`lode-568v.2` — an implementation detail, not a contract question.

**Protocol, not ABC** — matching this repo's existing precedent for exactly this shape of seam: the
`Embedder` Protocol (`src/lode/embedding.py`), cited directly in the epic body as the model for any
future vendor-neutral abstraction ("independent of this epic … the existing `Embedder` Protocol").
Structural typing needs no shared base class, and every current + hypothetical future provider
already satisfies the same shape without inheriting anything, the same way `FastEmbedEmbedder` does
today.

### 1. Client + credential/routing construction

Replaces `auth.build_client()`, which today constructs a bare `anthropic.Anthropic()` from the SDK's
own credential chain with no routing insertion point at all:

```python
def build_provider(settings: Settings) -> LLMProvider:
    """Resolve credentials + routing for settings.llm_provider; return its LLMProvider.

    Raises LLMAuthError (provider-appropriate message) when nothing resolves.
    """
```

- `settings.llm_provider == "anthropic"` → resolves via the same SDK credential chain
  `build_client()` uses today (env var / `ant auth login` profile / workload-identity federation),
  returns an `AnthropicProvider`.
- `settings.llm_provider == "openai"` → resolves `OPENAI_API_KEY`, or — when
  `settings.azure_openai_endpoint` is non-empty — `AZURE_OPENAI_API_KEY` plus the endpoint/
  api-version routing knobs (§6 below); returns an `OpenAIProvider` (`lode-568v.3`'s implementation).
  Azure-vs-direct-OpenAI is a routing detail *under* this one value, never a second provider value
  (epic's resolved decision, 2026-07-22).
- Failure is `LLMAuthError` (below), its message naming the *correct* env var(s) for whichever
  provider is active — generalizing today's Anthropic-worded `MISSING_CREDENTIALS_MESSAGE`.
  **`lode-568v.2` implementation note:** the Anthropic branch does NOT wrap `build_client()`'s
  `AuthError` into `LLMAuthError` — `AuthError` propagates unchanged, preserving `worker.py`'s
  extensively-tested `lode-9yy` permanent-failure handling byte-for-byte. `LLMAuthError` is reserved
  for a future non-Anthropic provider's own credential failures. Full rationale and the tracked
  follow-up: `decisions.md` (`lode-568v.2`).

### 2 & 3. The two immediate structured-output calls — one seam method

`enrich._call_haiku()` (forced tool-use: `tools=[…]`, `tool_choice={"type": "tool", "name": …}`,
reads `content` block `type == "tool_use"`) and `qa._request_claims()` (`messages.parse(...,
output_format=...)`, reads `response.parsed_output`) take **identical inputs** once named generically
— a model, a system prompt, a user prompt, an output Pydantic schema, a token cap, a timeout. There
is no principled reason to keep them as two seam methods; the epic's own work-surface-4 language
("response-shape differences must be normalized at the seam") is exactly this. One generic method:

```python
def structured_call(
    self,
    *,
    model: str,
    reasoning_effort: str | None,
    system: str,
    user_prompt: str,
    output_schema: type[BaseModelT],
    max_tokens: int,
    timeout_s: float,
    tool_name: str | None = None,
    tool_description: str | None = None,
) -> BaseModelT: ...
```

`tool_description` is a `lode-568v.2` addition beyond this ticket's original pin — required for
`AnthropicProvider`'s forced tool-use branch to send the *exact* tool description text `_call_haiku()`
sent pre-seam (byte-for-byte wire equivalence is `lode-568v.2`'s own acceptance bar, and this pin had
no way to carry it). See `decisions.md` (`lode-568v.2`) for the full rationale.

**`AnthropicProvider` maps onto today's exact calls with zero behavior change (the decision this
ticket's acceptance criteria names explicitly) via `tool_name`, not by unifying the underlying wire
mechanism** — asserting that `messages.parse` and forced tool-use are wire-equivalent isn't a claim
this docs-only ticket can verify, and `lode-568v.2`'s own acceptance bar is "byte-for-byte
equivalent." So the two existing mechanisms stay literally distinct, selected by whether the caller
passes `tool_name`:

- **Enrichment** passes `tool_name=_TOOL_NAME` → `AnthropicProvider` forces tool-use exactly as
  `_call_haiku()` does today (same `tools=[…]`, same `tool_choice`, same `tool_use` block read).
- **Q&A** passes no `tool_name` (`None`) → `AnthropicProvider` calls `messages.parse(output_format=
  output_schema)` exactly as `_request_claims()` does today.

`reasoning_effort` reaches `AnthropicProvider` as `output_config.effort` on every wire mechanism
(`lode-wnz1`), subject to the model-support caveat recorded in
[configuration.md](configuration.md#reasoning_effort-wired-to-output_configeffort-decided-lode-wnz1)
(some models reject `effort` outright, so this is a model choice, not just a level choice).
`OpenAIProvider` (`lode-568v.3`) has a single wire mechanism for structured output — the Responses
API's `text.format`/json_schema (see the Azure/OpenAI routing note below) — so it can honor or ignore
`tool_name` as it sees fit; the param is Anthropic-mechanism-selecting, not a cross-provider
requirement.

**`lode-568v.2` note:** `timeout_s` is now threaded into *every* `structured_call` — including Q&A's,
which pre-seam passed no client-side timeout to `messages.parse` at all (only the enrichment/batch
calls read `anthropic_call_timeout_s`). This is the intended effect of unifying the seam ("every LLM
call, immediate and batch alike" — §6 below), not an accidental behavior change: Q&A now gets the same
hung-call protection enrichment already had, bounded by the same (renamed) knob.

### 4. The two-phase batch contract (the trap — pinned precisely)

Anthropic's Batches path (`submit_enrich_batch` / `collect_enrich_batch`) is deliberately
**two-phase across drain passes**: submit persists `batch_handle` on the job rows (survives a
restart, `lode-i05.5`) and collect reaps on a *later* pass, so the drain keeps working while the
batch cooks. A blocking `run_batch(requests) -> results` contract would regress Anthropic (the drain
would block on it). The seam stays two-phase, and `enrich.py` keeps **all** job-row / `egress_log` /
DB bookkeeping — the provider only implements "run this set of requests":

```python
@dataclass(frozen=True)
class BatchRequest:
    custom_id: str  # version_id/snapshot_id, mirrors today's custom_id mapping
    model: str
    reasoning_effort: str | None
    system: str
    user_prompt: str
    output_schema: type[BaseModel]
    max_tokens: int
    tool_name: str | None = None
    tool_description: str | None = (
        None  # lode-568v.2 addition, see structured_call above
    )


@dataclass(frozen=True)
class BatchResult:
    custom_id: str
    outcome: Literal["succeeded", "errored", "expired", "canceled"]
    parsed: BaseModel | None  # set iff outcome == "succeeded" -- the provider's RAW
    # decoded wire payload (a pydantic.RootModel[dict]), NEVER
    # a schema-validated domain object; the caller validates it
    # against whatever output_schema it submitted (lode-568v.2,
    # decisions.md -- keeps the provider generic and preserves
    # lode-i05.5 restart durability with no schema info needing
    # to survive in the persisted batch_handle)
    error: LLMProviderError | None  # set iff outcome != "succeeded"


class LLMProvider(Protocol):
    def submit_batch(
        self, requests: Sequence[BatchRequest], *, timeout_s: float
    ) -> str:
        """Submit; return an opaque, PERSISTABLE handle string (stored as batch_handle)."""
        ...

    def collect_batch(
        self, handle: str, *, timeout_s: float
    ) -> tuple[Literal["pending"], None] | tuple[Literal["ended"], list[BatchResult]]:
        """Poll `handle`; ("pending", None) or ("ended", <results, one per request>)."""
        ...
```

- **`AnthropicProvider`**: `submit_batch` calls `client.beta.messages.batches.create(...)`, returns
  `batch.id` as the handle — identical to `submit_enrich_batch` today. `collect_batch` calls
  `batches.retrieve` + `.results` when ended — identical to `collect_enrich_batch` today.
- **A provider without a batch API (OpenAI/Azure, `lode-568v.3`) satisfies the contract
  degenerately: `submit_batch` runs every request through `structured_call()` synchronously, right
  there** (the epic's own sanctioned "serialize as sequential immediate calls" behavior — a
  long-running `submit_batch` call is the accepted cost, not a bug), **and returns a handle that
  self-encodes the already-computed `BatchResult`s** (e.g. a JSON blob) rather than a server-side
  batch id — there is no such thing to reference. `collect_batch` then just decodes its own handle
  and returns `("ended", …)` immediately; no network call, no actual polling. The caller (`enrich.py`)
  neither knows nor cares which strategy produced the handle — exactly the epic's "the caller asks
  the provider to run the batch and does not know or care which strategy it used."

```mermaid
sequenceDiagram
    participant E as enrich.py (drain pass N)
    participant P as LLMProvider
    participant E2 as enrich.py (drain pass N+1)
    E->>P: submit_batch(requests)
    Note over P: Anthropic: beta.messages.batches.create -> batch_id<br>Serialize: run every request now, encode results
    P-->>E: handle (persisted as jobs.batch_handle)
    E2->>P: collect_batch(handle)
    Note over P: Anthropic: batches.retrieve/.results (may still be running)<br>Serialize: decode handle, always ended
    P-->>E2: ("pending", None) or ("ended", results)
```

### 5. Provenance capture point

A **new, nullable `annotations.provider TEXT` column** (alongside the existing `model TEXT`,
`schema.sql`) — not a composite string encoded into `model`. `annotations.model` keeps recording
exactly what it does today (the bare model/deployment string, unchanged in meaning and format);
`provider` records the short id each `LLMProvider` implementation exposes (`"anthropic"` /
`"openai"`). `NULL` on existing rows means "anthropic" by convention — the only provider that ever
wrote a row before this epic — so no backfill is required, matching the existing
"no separate manifest, aggregate read" provenance pattern
([configuration.md](configuration.md#model-provenance-the-enrichment-llm-decided-lode-g2745)). Same
treatment for **`egress_log`**: a new nullable `provider TEXT` column, populated by `log_egress()`
call sites going forward — the audit trail's whole point is "content left the box," and which vendor
it went to is part of that fact, not less so than for `annotations`. Schema migration + the
`_write_enrichment`/`log_egress` write-path changes are `lode-568v.4`'s scope, not this ticket's.

**Known consequence, scoped elsewhere:** `lode-o9k3`'s staleness comparison
(`_enrichment_model_stale` / `_STALE_ENRICHMENT_LIVE_HEADS_SQL`) currently compares stored `model`
against `settings.enrichment_llm` only; once `provider` is a real per-row fact, a provider switch
with an unchanged model *string* would not otherwise be caught. That read-side update is
`lode-568v.6`'s scope, already split out and tracked — not addressed here.

### 6. Config shape

**One whole-app provider selector** (resolved decision, 2026-07-22): setting a provider sets it for
*every* cloud-LLM surface; there is no per-surface vendor axis.

- `llm_provider: str = "anthropic"` (`Kind.RUNTIME`) — `"anthropic"` | `"openai"`.
- `azure_openai_endpoint: str = ""` (`Kind.RUNTIME`) — the resource **root**, e.g.
  `https://{resource}.openai.azure.com` (do **not** append `/openai`: it is passed to the openai
  SDK's `AzureOpenAI(azure_endpoint=…)`, which appends `/openai` itself, so `.../openai` doubles the
  path and every request 404s — verified against `openai==2.47.0`). Empty means direct OpenAI (or a
  non-`"openai"` provider); its presence is what distinguishes Azure routing from direct OpenAI
  *under* the one `"openai"` provider value, not a second vendor axis.
- `azure_openai_api_version: str = ""` (`Kind.RUNTIME`) — passed as a **query param on every
  request** (verified against a working Azure config, see this ticket's notes), e.g.
  `2025-04-01-preview`, not a header. Required when `azure_openai_endpoint` is set.
- Keys stay **env/SDK-only, never in config.toml** — `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`
  (unchanged) for Anthropic, `OPENAI_API_KEY` / `AZURE_OPENAI_API_KEY` for OpenAI/Azure — mirrors
  lode's existing keys-never-in-config invariant with no change needed to port it to Azure.

**Per-surface tier becomes a `(model, reasoning_effort)` pair, not a bare string** — resolving the
crux this ticket's acceptance criteria names explicitly. `enrichment_llm` / `qa_llm` /
`qa_think_harder_llm` stay as three *separate* knobs (unchanged in count and meaning — each still
selects a tier *within* the active provider), but each is now typed as a small `ModelTier` value:

```python
class ModelTier(BaseModel):
    model: str  # Anthropic model id, or an Azure/OpenAI deployment name
    reasoning_effort: str | None = (
        None  # meaningful only under a reasoning-capable deployment
    )
    max_tokens: int | None = None  # lode-d70n; None = the call site's own constant
```

`max_tokens` is a later addition (`lode-d70n`) on the same rationale — it co-varies with the model
choice exactly as `reasoning_effort` does, and left unset falls back to the call site's own output
budget (`qa.MAX_TOKENS` / `enrich.MAX_TOKENS`); see
[configuration.md](configuration.md#per-tier-max_tokens-override-decided-lode-d70n), which owns that
decision.

A bare TOML string (every existing `config.toml` today, e.g. `enrichment_llm = "claude-haiku-4-5"`)
coerces to `ModelTier(model=<string>)` — back-compat, no migration required for existing configs; an
inline table (`qa_think_harder_llm = { model = "gpt-5.5", reasoning_effort = "high" }`) sets the
fields explicitly. This directly answers the challenge's three-way crux ("does 'think harder' select
a different deployment, a different effort on one deployment, or both?") with **no new abstraction
beyond upgrading each existing knob's type** — since `qa_llm` and `qa_think_harder_llm` are already
two independent `ModelTier` values, a config can set `model` the same on both and vary only
`reasoning_effort` (effort bump on one deployment), vary only `model` (deployment swap, today's
existing Sonnet→Opus behavior — the historical case, preserved as-is), or vary both.

**`anthropic_call_timeout_s` renamed vendor-neutral: `llm_call_timeout_s`, with a back-compat
alias** — the second item this ticket's acceptance criteria names explicitly (the Anthropic-named
knob would otherwise govern the OpenAI/Azure provider too, `config.py:285`). Same default (120.0s),
same meaning (per-call client-side timeout passed to every LLM call, immediate and batch alike).
Back-compat mechanism: `load_settings()` already massages the raw `config.toml` dict before
constructing `Settings` (dropping `None`-valued overrides, `config.py`) — the same spot gains one
more rename: a `config.toml` still carrying the old key is mapped onto the new one
(`file_values.setdefault("llm_call_timeout_s", file_values.pop("anthropic_call_timeout_s", …))`-shape
logic) before validation, so an un-migrated config file keeps working rather than tripping
`extra="forbid"`. Exact implementation is `lode-568v.2`'s.

> **Update (lode-7y6s):** `llm_call_timeout_s` was itself later renamed
> `enrich_call_timeout_s`, once the `qa_call_timeout_s` split (`lode-wfyx`)
> left the general name reaching only `enrich.py`'s call sites. The write-up
> above stands as decided, but the mechanism it describes now runs **more
> than once, oldest-name-first** — each hop's output feeds the next, so the
> oldest key still reaches the current field. That order is load-bearing:
> reversing it strands the oldest key on `extra="forbid"`.

### Error contract — diagnosability over genericness

A provider's failure paths must surface enough to diagnose remotely, not collapse into one generic
lode error — the two residual structural risks doc-reading alone can't close (Azure api-version
skew, Azure content-filtering) are only observable in a real Azure environment, where logs are the
only diagnostic surface this repo can't reproduce locally (challenge addendum, 2026-07-22):

```python
class LLMProviderError(RuntimeError):
    """A provider call failure. Carries enough to diagnose remotely; chains onto
    the underlying SDK exception via __cause__."""

    provider: str
    status_code: int | None
    request_id: str | None


class LLMAuthError(LLMProviderError):
    """No credentials resolved for the active provider — raised by build_provider()."""
```

Every `LLMProvider` implementation converts the SDK's **status** errors (4xx/5xx) into
`LLMProviderError` (or a subclass) rather than letting them escape raw, so callers (`enrich.py`/
`qa.py`'s existing retry/backoff logic) catch one exception type across providers, while
`.status_code`/`.request_id` + the chained `__cause__` still expose whatever the underlying SDK/HTTP
response carried. This generalizes today's credential-only "provider-appropriate error messaging"
(§1) to *runtime* call failures too. The concrete OpenAI/Azure field-by-field mapping (which response
fields populate `status_code`/`request_id` for a Responses API error, an Azure content-filter
rejection, etc.) is `lode-568v.3`'s scope — only the shape is pinned here.

**What still escapes raw.** `AnthropicProvider` wraps all five of its SDK calls — the three that
submit (`lode-90o7`) and `collect_batch`'s two that poll (`lode-i7yr`) — plus, separately,
`collect_batch`'s own JSONL-results iteration (`lode-3gtu`). That last one is not covered by any
`except anthropic.*` clause and never can be: the SDK resolves HTTP status *before* it returns the
lazily-streamed decoder, so a failure while pulling the body is not an `anthropic` type at all.
`collect_batch` converts three such types to `LLMProviderError`:

| Escaping type | Cause |
|---|---|
| `httpx.HTTPError` | the stream dies mid-read |
| `json.JSONDecodeError` | a line is not valid JSON |
| `UnicodeDecodeError` | a line is not decodable at all — `json.loads(bytes)` sniffs an encoding and decodes *before* it parses, so an invalid byte fails one step earlier and lands on a different `ValueError` subclass |

The wrap brackets only the *iteration*, never the loop body, so a genuine bug below it can never be
mistaken for a stream failure and no bare `except Exception` is needed to say so. Whatever was
already decoded is discarded rather than returned partially: `batches.results` re-fetches the same
JSONL from the start on every call (not a resumable cursor), so nothing already-good is permanently
lost, only re-done on the next poll.

**A results line that is *well-formed* JSON but the wrong *shape* (`lode-i821`, rebuild of
`lode-t7en`)** is a different class again — it does not come from the iteration at all. The SDK
builds each line with `construct_type_unchecked`, which by design does **not** validate, so a missing
or malformed field leaves the corresponding attribute simply absent (or `None`) rather than raising a
pydantic `ValidationError` at decode time. The failure this produces — a raw `AttributeError`,
`TypeError`, or (one step later, inside `RootModel`'s own validation) pydantic `ValidationError` —
surfaces from the loop **body**, on attribute access, not from `_stream`'s iteration; deliberately not
swept up by the three types above, since catching it there would also swallow a real bug in the loop
body. Every attribute chain the loop body reads off the unvalidated model is guarded — narrow
`except`, degrading the *one* item to an `errored` `BatchResult` rather than failing the whole
collection, the same treatment the pre-existing "no `tool_use` block" arm already gets:

| Chain | Failure mode | Guard |
|---|---|---|
| `result.result.type` | missing `result` field → `AttributeError` | `except AttributeError` |
| `result.result.message.content` (iterated) | missing/`None` `content` → `TypeError` | `except (AttributeError, TypeError)` |
| `b.type` (each content block, inside the same iteration) | a content item that isn't object-shaped at all (e.g. `null`) → `AttributeError` | same `except (AttributeError, TypeError)` above — one combined arm, since either failure means the line can't be trusted |
| `tool_block.input` | missing `input` → `None`, and `RootModel[dict[str, Any]](None)` → pydantic `ValidationError` | `except ValidationError` |

Two fields round the enumeration out — both unvalidated like the rest, neither able to *raise*
in the loop body, so both would break their declared type silently rather than loudly:

- **`custom_id`** (declared `str`) is normalized by **`_result_custom_id`, the single reader**, which
  substitutes `"<unknown>"` for anything that isn't a non-empty `str`. This is deliberately a
  property of the *field*, not of the wrong-shape lines above: a line whose `result` block is
  well-formed and whose `custom_id` alone is missing passes every guard in the table and takes the
  ordinary `succeeded`/`errored`/no-`tool_use` branch, so *every* `BatchResult` built here takes its
  `custom_id` from that one reader. The result is an invariant consumers can rely on — a
  `BatchResult`'s `custom_id` is always a non-empty `str` — bought without widening the
  vendor-neutral type to `str | None` and obliging every consumer to branch on it. A placeholder is
  merely *unroutable*: it misses `collect_enrich_batch`'s `job_map`, which is an already-handled
  case, and never reaches a DB write.
- **`outcome`** (declared `Literal["succeeded", "errored", "expired", "canceled"]`) is
  `result.result.type` verbatim, which a missing `type` key leaves `None`. Inert rather than
  guarded: an unrecognized value simply takes the failure arm, which is the right handling for a
  result whose type can't be read. Listed so the enumeration is complete, not because it needs a
  guard.

**Why guard chain-by-chain rather than validate the line once up front** (`lode-i821`, decided): a
single `model_validate` at the top of the loop would collapse all of the above into one rule, and it
would *not* be the broad `except Exception` this design rejects — it fires before the body, so it
still couldn't mask a bug there. It is rejected for a different reason: `construct_type_unchecked`'s
leniency is load-bearing forward-compatibility. A content-block type newer than the pinned SDK
decodes fine today and yields a usable result; under strict validation the same line would be
*rejected*, converting a working result into an errored one on an SDK bump. The cost accepted in
exchange is that this enumeration is not closed by construction — a newly-added attribute read is an
unguarded hole by default, and the table above has to be re-derived from the loop body whenever it
changes. That trade is the reason the table is derived rather than asserted.

**One class remains open**, measured against the pinned SDK, not yet bounded:

- `anthropic`'s *non*-status errors (`APITimeoutError`, `APIConnectionError` — a timeout is not a
  rejected request; see `qa.MAX_TOKENS`).

`OpenAIProvider` needs none of this: its `collect_batch` makes no network call and decodes no stream
(`submit_batch` already ran every request and self-encoded the results into the handle), and it
catches bare `Exception` around its single real call. The asymmetry is inherent to the two batch
designs, not an unclosed gap.

**Consumer-side blast radius (`lode-5zqa`, `lode-knnt`).** Whatever this seam raises lands in
`lode.worker.drain`'s batch pre-step. Originally that pre-step caught only `(AuthError,
LLMProviderError)`, so a failure escaping this seam as something else (`lode-t7en`) still aborted the
whole drain — a *consumer-side* reason (on top of the diagnosability one above) the classes this
section names were worth closing at the seam rather than downstream. `lode-knnt` closed that
consumer-side gap instead (see `docs/storage.md` "Transient vs. permanent job failures" for the
mechanism), so a failure arriving as *any* type — including one still escaping this seam raw
— no longer starves the credential-free `embed` jobs or blocks a new enrich submission. Closing a
class named above at the seam is therefore no longer required for that reason; it remains worth doing
for the diagnosability reason this section opens with (`.status_code`/`.request_id`/`__cause__`, and
a message specific enough to act on — `lode-yx1c` landed the `lode work`/`lode ask` handler that
turns a non-auth `LLMProviderError` into a clean line instead of a raw traceback, so what is left at
this seam is the *quality* of that line, not whether one is printed). `docs/storage.md`
"Transient vs. permanent job failures" owns the policy and the limits it leaves standing.

### Implemented: `OpenAIProvider` (`lode-568v.3`)

`src/lode/llm_provider.py::OpenAIProvider` is the second `LLMProvider` implementation, resolved by
`build_provider` when `settings.llm_provider == "openai"`. It fills in the details this section left
open:

- **One wire mechanism regardless of `tool_name`**: the Responses API's `text.format` `json_schema`
  (`client.responses.create(model=, instructions=<system>, input=<user_prompt>, max_output_tokens=,
  text={"format": {...}}, reasoning={"effort": ...} if reasoning_effort else omitted, timeout=)`).
  `tool_name` (when given, e.g. by the enrichment surface) becomes the schema's `name` field;
  `tool_description` becomes its `description` field. Confirmed against the installed `openai==2.47.0`
  SDK's actual `responses.create` signature and `Response`/`IncompleteDetails`/`ResponseOutputRefusal`
  field shapes (not merely assumed from memory) — see the module docstring and `decisions.md`
  (`lode-568v.3`) for what was and wasn't independently verifiable this way.
- **`strict` is deliberately `False`**, not `True`. OpenAI's strict Structured Outputs mode requires
  every object in the schema to set `additionalProperties: false` and list every property as
  `required` (optional fields modeled as nullable) — a transformation `pydantic`'s
  `model_json_schema()` does not perform. Asserting strict-mode compliance without that transform
  would be exactly the wire-shape assumption the epic's challenge review flagged as highest-risk.
  `OpenAIProvider.structured_call` validates the returned JSON against `output_schema` via
  `model_validate` regardless — the real conformance check either way.
- **Credential resolution**: `OPENAI_API_KEY` (direct OpenAI) or, when `azure_openai_endpoint` is
  set, `AZURE_OPENAI_API_KEY` + the endpoint/api-version knobs (§6). Unlike `AnthropicProvider`'s
  branch, a missing credential here raises `LLMAuthError` for real — there was no pre-existing
  exception type to preserve for a provider that didn't exist before this ticket. This required
  widening `lode.worker`'s three `except AuthError` sites to `except (AuthError, LLMAuthError)` so a
  missing OpenAI/Azure credential gets the same permanent (no retry, no dead-letter) treatment
  `lode-9yy` already gives a missing Anthropic credential — the follow-up `lode-568v.2`'s
  implementation notes tracked.
- **Batch = serialize, exactly as pinned above**: `submit_batch` runs every request through the same
  Responses-API call synchronously and self-encodes the computed `BatchResult`s as a JSON blob string
  (the handle); `collect_batch` decodes it with no network call, always `("ended", …)`.
- **Diagnosability**: every failure path (a raised SDK exception, a non-`"completed"` response status,
  a Structured Outputs refusal, unparseable/schema-mismatched JSON) logs the model/endpoint/api-version
  in play plus the raw provider payload — including an Azure `innererror.content_filter_result` when
  present in an error body — before raising, per the challenge addendum above.
- **Acceptance is mock-only** (named risk, `decisions.md` `lode-568v.3`): no live Azure/OpenAI endpoint
  was available to verify the Responses API's actual runtime behavior end-to-end (only its installed
  SDK's *type shapes*, which were checked directly). The diagnostic logging above is the compensating
  control the challenge review asked for — a first real run's failure is diagnosable from logs alone.
