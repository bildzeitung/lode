"""Typed settings for every tunable knob in ``docs/configuration.md``.

One typed field per documented knob, each tagged with its **kind** — the same
runtime / tune / build taxonomy the docs use (``docs/configuration.md``):

- ``runtime`` — changeable while running; takes effect on next use.
- ``tune`` — ships with a conservative default, meant to be tuned against the
  eval harness once there is a real corpus.
- ``build`` — fixed at build time; changing it implies a rebuild/migration.

Defaults are the conservative starting points from the docs table, not measured
optima. Descriptive doc defaults ("small", "periodic", "top-N", ...) are
rendered here as concrete conservative values; the source phrasing is noted in
each field's description. Invalid values fail validation at construction
(``Settings()`` / :func:`load_settings`), since pydantic validates every field.

The local-model ids — embedder + vector dim, reranker, and the NLI/entailment
model + loader — are pinned for real in ``lode-txh.6``, each verified to load on
the ``fastembed`` ONNX runtime (``tests/test_models_smoke.py``).
The redaction pattern *sets* are the high-precision seed (``lode-fk8.2``) that
drives :mod:`lode.redact`'s redact-before-index / redact-before-egress controls;
each pattern is validated to compile at load.
"""

import os
import re
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lode.llm_provider import EFFORT_LEVELS_BY_PROVIDER, ModelTier
from lode.lock import lock_path
from lode.no_egress_scope import SCOPED_SOURCE_TYPES, NoEgressScopeRule

# --- Atlassian connector credential env vars (lode-gpzn.1) --------------------
# Documented, env-var-PRIMARY resolution for the JIRA/Confluence Cloud Basic-auth
# credentials (account email + API token) -- checked before the config.toml
# fallback fields declared on Settings below. Named here, at module scope
# (rather than as string literals), so Settings' field descriptions and
# resolve_jira_credentials/resolve_confluence_credentials share one spelling.
JIRA_TOKEN_ENV = "LODE_JIRA_TOKEN"
JIRA_EMAIL_ENV = "LODE_JIRA_EMAIL"
CONFLUENCE_TOKEN_ENV = "LODE_CONFLUENCE_TOKEN"
CONFLUENCE_EMAIL_ENV = "LODE_CONFLUENCE_EMAIL"


class Kind(str, Enum):
    """How a knob may change — mirrors the Kind column in docs/configuration.md."""

    RUNTIME = "runtime"
    TUNE = "tune"
    BUILD = "build"


def _knob(
    default: object, kind: Kind, doc: str, secret: bool = False, **constraints: object
) -> object:
    """A typed field carrying its kind tag (read back via :func:`knob_kinds`).

    ``secret=True`` marks a field whose RAW VALUE must never be echoed back to
    the user (a credential, e.g. an Atlassian API token or account email,
    lode-gpzn.1 / lode-dx4r) -- :func:`knob_rows` renders any field tagged this
    way as a presence indicator (``[REDACTED]`` if resolved from *any* source,
    ``[unset]`` if not) instead of its value, regardless of its ``kind``. This
    is a *show-presence*, not *exclude-the-row*, contract -- the row still
    appears, just never carrying the value. The field is ALSO given
    ``repr=False`` so the raw value never leaks through the Pydantic model's
    own ``repr()`` / ``str()`` either (an incautious ``logger.debug(settings)``
    or ``print(settings)`` would otherwise echo it -- the acceptance is "never
    logged or echoed *anywhere*", lode-gpzn.1).
    """
    extra: dict[str, object] = {"kind": kind.value}
    if secret:
        extra["secret"] = True
    return Field(
        default,
        description=doc,
        json_schema_extra=extra,
        repr=not secret,
        **constraints,
    )


# High-precision secret seed set (docs/configuration.md "Privacy & egress",
# docs/externals.md "Two redactions"). Each pattern is distinctive enough to
# almost never fire on prose, so it can run unattended on every payload; the set
# is meant to be iterated from real misses, not to be exhaustive. Seeds BOTH the
# redact-before-index and redact-before-egress knobs (they read separate fields
# so an operator can tune them apart, but ship identical). Consumed by
# :mod:`lode.redact`.
_SECRET_SEED_PATTERNS: list[str] = [
    # PEM private-key headers (RSA/EC/DSA/OpenSSH/PGP and bare).
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----",
    # AWS access-key id: long-term (AKIA) and temporary (ASIA).
    r"(?:AKIA|ASIA)[0-9A-Z]{16}",
    # GitHub PATs: classic/oauth/user/server/refresh tokens.
    r"gh[pousr]_[0-9A-Za-z]{36}",
    # GitHub fine-grained PAT.
    r"github_pat_[0-9A-Za-z_]{82}",
    # Slack tokens (bot/user/app/refresh/legacy).
    r"xox[baprs]-[0-9A-Za-z-]{10,}",
    # Stripe live secret/restricted keys.
    r"(?:sk|rk)_live_[0-9A-Za-z]{24,}",
    # Google API key.
    r"AIza[0-9A-Za-z_-]{35}",
    # Anthropic API key.
    r"sk-ant-[0-9A-Za-z_-]{20,}",
]


class Settings(BaseModel):
    """Every ``docs/configuration.md`` knob as a typed, validated field.

    Unknown keys are rejected (``extra="forbid"``) so a typo'd override fails
    loudly rather than being silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Retrieval and ranking ------------------------------------------------
    retrieval_top_k: int = _knob(
        20,
        Kind.TUNE,
        "Passages surviving fusion into rerank / reaching Q&A context.",
        gt=0,
    )
    rrf_k: int = _knob(
        60, Kind.TUNE, "Reciprocal-Rank-Fusion smoothing constant.", gt=0
    )
    rerank_enabled: bool = _knob(
        True,
        Kind.RUNTIME,
        "Toggle the cross-encoder rerank stage (the seam is permanent).",
    )
    rerank_model: str = _knob(
        "BAAI/bge-reranker-base",
        Kind.TUNE,
        "Local cross-encoder reranker via fastembed TextCrossEncoder, ONNX. "
        "Swappable. (Doc default bge-reranker-v2-m3 is not in fastembed's "
        "supported set; bge-reranker-base is the loadable bge-family pick — "
        "lode-txh.6.)",
    )
    rerank_keep_n: int = _knob(
        10,
        Kind.TUNE,
        "Reranked hits proceeding to graph expansion (doc default 'top-N').",
        gt=0,
    )

    # --- Chunking (passages) --------------------------------------------------
    chunk_threshold_tokens: int = _knob(
        512,
        Kind.TUNE,
        "Structure-aware split sub-splits any block over N tokens (doc range ~256-512).",
        gt=0,
    )
    chunk_overlap_tokens: int = _knob(
        64,
        Kind.TUNE,
        "Overlap between fallback sub-chunks (doc default 'small').",
        ge=0,
    )

    # --- Faithfulness gate ----------------------------------------------------
    entailment_model: str = _knob(
        "BAAI/bge-reranker-base",
        Kind.TUNE,
        "Local NLI / cross-encoder for citation entailment. fastembed ships no "
        "dedicated NLI model, so the cross-encoder reranker is repurposed as the "
        "entailment scorer (its raw logit, sigmoid'd by the gate) — lode-txh.6.",
    )
    entailment_loader: str = _knob(
        "fastembed-cross-encoder",
        Kind.BUILD,
        "How the NLI/entailment model is loaded. fastembed's TextCrossEncoder "
        "runs it on the bundled ONNX runtime in-process (no separate "
        "optimum/onnxruntime loader needed) — lode-txh.6.",
    )
    entailment_threshold: float = _knob(
        0.9,
        Kind.TUNE,
        "Entailment acceptance threshold. Ships fail-closed (conservative), untuned.",
        ge=0.0,
        le=1.0,
    )
    llm_judge_enabled: bool = _knob(
        False,
        Kind.RUNTIME,
        "Optional high-assurance LLM-judge second pass (costs egress + $).",
    )

    # --- TUI (passive connection surfacing, E11) ------------------------------
    related_notes_enabled: bool = _knob(
        True,
        Kind.RUNTIME,
        "Master on/off switch for the passive related-notes pass. Off skips "
        "find_related_notes entirely, so no FTS5/embedder/LanceDB work runs on "
        "the input path. This is a user preference, not a lag fix -- lode-0wj.2 "
        "confirmed the pass already runs off the UI thread (fastembed/ONNX "
        "releases the GIL), so disabling it does not change keystroke latency.",
    )
    related_notes_debounce_ms: int = _knob(
        500,
        Kind.RUNTIME,
        "Idle-typing delay in the capture screen before a passive "
        "connection-surfacing pass runs, so it never fires on every keystroke.",
        gt=0,
    )
    related_notes_limit: int = _knob(
        5,
        Kind.RUNTIME,
        "Max related past notes shown per passive connection-surfacing pass.",
        gt=0,
    )
    related_notes_min_chars: int = _knob(
        20,
        Kind.RUNTIME,
        "Minimum draft length (stripped) before a passive surfacing pass "
        "runs; skips near-empty buffers without touching the DB.",
        ge=0,
    )
    ask_context_chars: int = _knob(
        80,
        Kind.RUNTIME,
        "Characters of a cited note/external body shown before and after a "
        "citation's quoted_span in the ask screen's grouped-by-note view "
        "(lode-35nu.3).",
        ge=0,
    )

    # --- Async work queue -----------------------------------------------------
    reconciliation_scan_interval_s: int = _knob(
        300,
        Kind.RUNTIME,
        "Self-healing scan interval re-enqueueing missing derived work.",
        gt=0,
    )
    retry_max_attempts: int = _knob(
        5, Kind.RUNTIME, "Max attempts before dead-lettering a job.", ge=1
    )
    vectorstore_optimize_interval: int = _knob(
        200,
        Kind.RUNTIME,
        "How many replace_vectors() calls a shared VectorStore makes before "
        "pruning its held Table's version history, which bounds that Table's "
        "memory growth over a long-running process (lode-2brb; "
        "docs/configuration.md).",
        gt=0,
    )
    stale_running_timeout_s: int = _knob(
        900,
        Kind.RUNTIME,
        "A job stuck in status='running' this long (no claim update) is "
        "reclaimed as a crash -- same attempts/backoff/dead-letter accounting "
        "as a transient handler failure. Excludes batch-backed enrich jobs "
        "(batch_handle set), which own their own long-lived 'running' state.",
        gt=0,
    )
    retry_backoff_base_s: float = _knob(
        1.0, Kind.RUNTIME, "Base delay for exponential retry backoff.", gt=0.0
    )
    retry_backoff_cap_s: float = _knob(
        60.0, Kind.RUNTIME, "Cap on exponential retry backoff delay.", gt=0.0
    )
    enrichment_batch_flush_size: int = _knob(
        50,
        Kind.RUNTIME,
        "Accumulated enrich jobs submitted as one Claude Batch (size policy).",
        gt=0,
    )
    batch_collect_failure_budget: int = _knob(
        5,
        Kind.RUNTIME,
        "Consecutive collect_enrich_batch() failures (the poll call itself "
        "raising, not an individual result outcome) at which one "
        "batch_handle's still-running jobs are dead-lettered -- so N-1 are "
        "tolerated and the Nth is fatal. A per-result failure is unaffected: "
        "it goes through the ordinary attempts/backoff/dead-letter accounting "
        "instead (lode-u6he; docs/storage.md owns the rationale).",
        ge=1,
    )
    enrichment_batch_flush_interval_s: int = _knob(
        60,
        Kind.RUNTIME,
        "Max wait before flushing an enrich batch (time policy).",
        gt=0,
    )
    work_wait_timeout_s: int = _knob(
        1800,
        Kind.RUNTIME,
        "Max time 'lode work --wait' blocks polling for the queue to fully "
        "drain (incl. batch enrich results collected) before exiting non-zero "
        "and naming the still-pending/running jobs. The Batches API can take "
        "minutes-to-hours (SLA <=24h, docs/storage.md) so --wait may "
        "legitimately time out on a large enrich load -- that is accepted, not "
        "a bug; --wait suits embed-heavy or small-batch cases, and a big async "
        "enrich backlog may need a plain re-run of 'lode work' instead.",
        gt=0,
    )
    progress_heartbeat_interval_s: float = _knob(
        15.0,
        Kind.RUNTIME,
        "How often 'lode work' logs a 'still running' heartbeat line "
        "(lode.progress.op_progress) for a named long-running op -- a "
        "reconcile() step, a drain() batch pre-step, or the main claim/run "
        "loop -- that hasn't finished yet (lode-olmi.15). Makes a genuinely "
        "stuck op visible instead of silent, even when it can't be safely "
        "aborted outright.",
        gt=0.0,
    )
    enrich_call_timeout_s: float = _knob(
        120.0,
        Kind.RUNTIME,
        "Per-call client-side timeout (seconds) passed to every ENRICHMENT "
        "cloud-LLM call through the LLMProvider seam (lode-568v.2), immediate "
        "and batch alike: the calls reachable from 'lode work' (enrich.py -- "
        "the Batches API pre-steps and the immediate Haiku call a residual "
        "enrich job can take in drain()'s main loop) -- bounds a hung network "
        "call rather than letting it block forever (lode-olmi.15). Renamed "
        "vendor-neutral from anthropic_call_timeout_s (lode-568v.1/.2), then "
        "renamed again from llm_call_timeout_s to enrich_call_timeout_s "
        "(lode-7y6s) once the qa_call_timeout_s split (lode-wfyx) left the "
        "general name covering only this enrichment subset. A config.toml "
        "still carrying either old key is remapped by load_settings(). "
        "Distinct from fetch_timeout_s, which governs web "
        "draw-down HTTP fetches, not LLM provider calls. Does NOT reach "
        "the Q&A synthesis call (qa.py) -- that call has its own "
        "qa_call_timeout_s below, split off in lode-wfyx because one shared "
        "value couldn't serve both a foreground TUI call with adaptive "
        "thinking and background enrichment work without either loosening "
        "enrichment's hang-detection or under-timing Q&A.",
        gt=0.0,
    )
    qa_call_timeout_s: float = _knob(
        300.0,
        Kind.RUNTIME,
        "Per-call client-side timeout (seconds) for the Q&A synthesis call "
        "ONLY (qa.py's structured_call) -- split off enrich_call_timeout_s "
        "(lode-wfyx), which still governs every enrich.py call site "
        "unchanged. Needed because lode-3dlt let the think-harder tier "
        "(qa_think_harder_llm, Opus 5 by default) run adaptive thinking it "
        "previously never did, while enrich_call_timeout_s stayed at 120s -- "
        "plausibly too short once thinking shares qa.MAX_TOKENS with the "
        "claims response. The default is DERIVED, NOT a measured p95 (a live "
        "p95 benchmark was deliberately declined on cost/value, not for lack "
        "of capability). The derivation, the SDK-retry interaction it was "
        "chosen alongside, and the ModelTier.max_tokens override that "
        "invalidates it all live in ONE place -- docs/configuration.md 'Q&A "
        "call timeout split from llm_call_timeout_s' -- deliberately not "
        "restated here, because the numbers have already drifted once across "
        "the copies.",
        gt=0.0,
    )
    llm_provider: Literal["anthropic", "openai"] = _knob(
        "anthropic",
        Kind.RUNTIME,
        "Which LLMProvider implementation every cloud-LLM call site resolves "
        "against (lode-568v.2/.3) -- whole-app, not per-surface: setting this "
        "sets it for enrichment AND Q&A together. 'openai' routes to either "
        "direct OpenAI or Azure OpenAI depending on azure_openai_endpoint -- "
        "Azure-vs-direct-OpenAI is a routing detail under this one value, not "
        "a second provider value (docs/stack.md).",
    )
    azure_openai_endpoint: str = _knob(
        "",
        Kind.RUNTIME,
        "Azure OpenAI resource endpoint, e.g. "
        "https://{resource}.openai.azure.com (lode-568v.3) -- the resource "
        "ROOT only; do NOT append '/openai', the openai SDK's AzureOpenAI "
        "client adds that path segment itself (passing '.../openai' doubles "
        "it and every request 404s). Empty means direct OpenAI (or a "
        "non-'openai' llm_provider) -- its presence is what distinguishes "
        "Azure routing from direct OpenAI under the one 'openai' llm_provider "
        "value (docs/stack.md). Only meaningful when llm_provider == 'openai'. "
        "Requires azure_openai_api_version to also be set.",
    )
    azure_openai_api_version: str = _knob(
        "",
        Kind.RUNTIME,
        "Azure OpenAI api-version query param, e.g. '2025-04-01-preview' "
        "(lode-568v.3) -- sent as a query param on every request, not a "
        "header (verified against a working Azure config, docs/stack.md). "
        "Required when azure_openai_endpoint is set; validated at Settings "
        "construction.",
    )

    # --- Externals (with connectors) -----------------------------------------
    refresh_ttl_s: int = _knob(
        3600,
        Kind.RUNTIME,
        "Default per-source revalidation TTL for external snapshots.",
        gt=0,
    )
    reenrichment_materiality_threshold: float = _knob(
        0.2,
        Kind.TUNE,
        "Embedding-similarity delta (1 - cosine) gating paid re-enrichment of a "
        "changed snapshot.",
        ge=0.0,
        le=1.0,
    )
    drawdown_hop_limit: int = _knob(
        1, Kind.BUILD, "Follow explicit links this many hops, then stop.", ge=0
    )
    fetch_timeout_s: float = _knob(
        10.0,
        Kind.RUNTIME,
        "Per-fetch HTTP timeout (lode-w0h.1) before treated as a transient "
        "(retryable) failure.",
        gt=0.0,
    )
    fetch_max_redirects: int = _knob(
        5,
        Kind.RUNTIME,
        "Max 3xx redirects a single web-fetch follows (lode-w0h.1) before "
        "tombstoning as unresolvable. Distinct from drawdown_hop_limit, which "
        "governs crawling a fetched page's own outbound links, not redirects "
        "within one fetch.",
        ge=0,
    )
    fetch_min_extract_chars: int = _knob(
        200,
        Kind.TUNE,
        "Readability-extracted text shorter than this (lode-w0h.1) is treated "
        "as a JS-scaffold/paywall/empty page and tombstoned rather than "
        "snapshotted, even when the extractor returned non-None text.",
        ge=0,
    )
    url_tracking_param_blocklist: list[str] = _knob(
        ["utm_*", "fbclid", "gclid"],
        Kind.RUNTIME,
        "Query params stripped during URL canonicalization (lode-w0h.3) before "
        "an external_id dedup key is computed. A trailing '*' matches a "
        "prefix (case-insensitive), e.g. 'utm_*' matches utm_source, "
        "utm_medium, utm_campaign, ... Everything else matches exactly.",
    )

    # --- Externals: Atlassian connectors (JIRA + Confluence Cloud, lode-gpzn) -
    # Locked decisions (lode-gpzn epic): Cloud-only, Basic auth (account email +
    # API token), feature-flagged per product default OFF, token resolved
    # env-var PRIMARY with an optional config.toml fallback (no secret required
    # to live in config.toml). See resolve_jira_credentials /
    # resolve_confluence_credentials / jira_active / confluence_active below.
    jira_enabled: bool = _knob(
        False,
        Kind.RUNTIME,
        "Feature flag: JIRA Cloud API connector. Off by default -- a JIRA "
        "link falls through to the generic web connector until flagged on "
        "AND credentials resolve (lode-gpzn.1).",
    )
    confluence_enabled: bool = _knob(
        False,
        Kind.RUNTIME,
        "Feature flag: Confluence Cloud API connector. Off by default -- a "
        "Confluence link falls through to the generic web connector until "
        "flagged on AND credentials resolve (lode-gpzn.1).",
    )
    jira_base_url: str = _knob(
        "",
        Kind.RUNTIME,
        "JIRA Cloud API base URL override, e.g. 'https://acme.atlassian.net'. "
        "Empty (default) means infer from the pasted link at detection time. "
        "Validated as a well-formed http(s) URL when non-empty.",
    )
    confluence_base_url: str = _knob(
        "",
        Kind.RUNTIME,
        "Confluence Cloud API base URL override. Empty (default) means infer "
        "from the pasted link at detection time. Validated as a well-formed "
        "http(s) URL when non-empty.",
    )
    jira_email: str = _knob(
        "",
        Kind.RUNTIME,
        f"JIRA Cloud Basic-auth account email, config.toml FALLBACK only -- "
        f"the {JIRA_EMAIL_ENV} env var is checked first (lode-gpzn.1). Empty "
        "means unresolved from this source. NEVER echoed verbatim -- shown "
        "as a presence indicator in the lode config / TUI knob table "
        "(secret=True, lode-dx4r).",
        secret=True,
    )
    confluence_email: str = _knob(
        "",
        Kind.RUNTIME,
        f"Confluence Cloud Basic-auth account email, config.toml FALLBACK "
        f"only -- the {CONFLUENCE_EMAIL_ENV} env var is checked first "
        "(lode-gpzn.1). Empty means unresolved from this source. NEVER "
        "echoed verbatim -- shown as a presence indicator in the lode "
        "config / TUI knob table (secret=True, lode-dx4r).",
        secret=True,
    )
    jira_token: str = _knob(
        "",
        Kind.RUNTIME,
        f"JIRA Cloud API token, config.toml FALLBACK only -- the "
        f"{JIRA_TOKEN_ENV} env var is checked first (lode-gpzn.1). No secret "
        "is required to live here. NEVER logged or echoed -- shown as a "
        "presence indicator in the lode config / TUI knob table "
        "(secret=True).",
        secret=True,
    )
    confluence_token: str = _knob(
        "",
        Kind.RUNTIME,
        f"Confluence Cloud API token, config.toml FALLBACK only -- the "
        f"{CONFLUENCE_TOKEN_ENV} env var is checked first (lode-gpzn.1). No "
        "secret is required to live here. NEVER logged or echoed -- shown "
        "as a presence indicator in the lode config / TUI knob table "
        "(secret=True).",
        secret=True,
    )

    # --- Privacy & egress -----------------------------------------------------
    no_egress_default: bool = _knob(
        False,
        Kind.RUNTIME,
        "Default no_egress for new notes/sources (indexed locally only).",
    )
    redact_before_egress_patterns: list[str] = _knob(
        _SECRET_SEED_PATTERNS,
        Kind.RUNTIME,
        "High-precision secret regexes stripped before content is sent to Claude "
        "(enrich + Q&A); iterate from real misses. Drives lode.redact.",
    )
    redact_before_index_patterns: list[str] = _knob(
        _SECRET_SEED_PATTERNS,
        Kind.RUNTIME,
        "High-precision secret regexes kept out of the local vector/FTS index; "
        "iterate from real misses. Drives lode.redact.",
    )
    no_egress_scopes: list[NoEgressScopeRule] = _knob(
        [],
        Kind.RUNTIME,
        "no_egress SCOPE rules (lode-35nu.11.8): each entry covers every "
        "external whose (source_type, external_id) matches, including one "
        "with no externals row yet -- evaluated live, never materialized "
        "onto a row. source_type='jira': match is a project key, matched "
        "against the issue-key prefix. source_type='web': match is a URL "
        "host, matched exactly. source_type='confluence' is REJECTED at "
        "load (see the field validator below) -- drawdown.py's "
        "_CONFLUENCE_PAGE_RE discards the space key at detection time, so a "
        "space-scoped rule is structurally unmatchable, not just unbuilt. "
        "Composes with the per-row externals.no_egress flag: either denying "
        "is a denial.",
    )
    ask_tools_enabled: bool = _knob(
        False,
        Kind.RUNTIME,
        "Feature flag (lode-8hsk / lode-35nu.11.2): offer the read-only "
        "JIRA/Confluence search tools and the generic fetch tool to the Ask "
        "synthesis call. Off by default -- notes-only behaviour is unchanged "
        "either way, since lode.tool_dispatch.build_ask_tools returns no "
        "tools at all while this is False, regardless of what a caller "
        "passes as answer_question's own tools_enabled argument. "
        "NOT YET REACHABLE FROM A REAL 'lode ask': cited_answer.ask -- the "
        "single path both the CLI and the TUI take -- does not pass "
        "tools_enabled to qa.answer_question, so turning this on has no "
        "effect on a real ask today. This ticket ships the substrate; "
        "lode-8vvp wires the production path (and makes a "
        "tool-fetched snapshot gate-eligible). Until then this flag is "
        "exercised only from the qa layer directly.",
    )
    ask_tool_budget: int = _knob(
        6,
        Kind.RUNTIME,
        "Per-ask tool-call budget (lode-8hsk): search and fetch calls share "
        "ONE counter, enforced by lode.tool_dispatch.ToolBudget -- a call "
        "past the budget is refused (the model is told so) rather than "
        "dispatched. Distinct from llm_provider._DEFAULT_MAX_TOOL_TURNS (a "
        "provider-level free-turn cap, one call per turn is not assumed).",
        gt=0,
    )

    # --- Models ---------------------------------------------------------------
    embedding_model: str = _knob(
        "nomic-ai/nomic-embed-text-v1.5",
        Kind.BUILD,
        "Local ONNX embedder via fastembed; a change re-keys the vector space "
        "(full re-embed + re-index) — lode-txh.6.",
    )
    embedding_vector_dim: int = _knob(
        768,
        Kind.BUILD,
        "Output dimension of embedding_model. LanceDB table creation needs this "
        "fixed; re-keying it implies a full re-embed. Must match the model "
        "(nomic-embed-text-v1.5 → 768) — lode-txh.6.",
        gt=0,
    )
    hf_probe_timeout_s: float = _knob(
        5.0,
        Kind.RUNTIME,
        "Per-call timeout (seconds) for resolve_model_revision's live HF "
        "revision probe (lode.embedding) -- bounds a black-holed network to "
        "this instead of the OS TCP connect timeout (lode-w5nr). Matches "
        "httpx's own default, not fetch_timeout_s (10s, page fetches): a "
        "small metadata GET. Full reasoning and the measurement: "
        "docs/decisions.md, the lode-w5nr entry.",
        gt=0.0,
    )
    enrichment_llm: ModelTier = _knob(
        ModelTier(model="claude-haiku-4-5"),
        Kind.RUNTIME,
        "High-volume background extraction LLM (Claude Haiku 4.5). A "
        "(model, reasoning_effort, max_tokens) tier (lode-568v.2; max_tokens "
        "lode-d70n) -- a bare TOML string still coerces to a ModelTier with "
        "reasoning_effort=None and max_tokens=None (falls back to "
        "enrich.MAX_TOKENS).",
    )
    qa_llm: ModelTier = _knob(
        ModelTier(model="claude-sonnet-4-6"),
        Kind.RUNTIME,
        "Default interactive Q&A synthesis LLM (Claude Sonnet 4.6). A "
        "(model, reasoning_effort, max_tokens) tier (lode-568v.2; max_tokens "
        "lode-d70n) -- a bare TOML string still coerces to a ModelTier with "
        "reasoning_effort=None and max_tokens=None (falls back to "
        "qa.MAX_TOKENS).",
    )
    qa_think_harder_llm: ModelTier = _knob(
        ModelTier(model="claude-opus-5"),
        Kind.RUNTIME,
        "Higher-quality 'think harder' Q&A LLM on demand (Claude Opus 5). A "
        "(model, reasoning_effort, max_tokens) tier (lode-568v.2; max_tokens "
        "lode-d70n) -- a bare TOML string still coerces to a ModelTier with "
        "reasoning_effort=None and max_tokens=None (falls back to "
        "qa.MAX_TOKENS).",
    )

    # --- Build constants (chosen once) ---------------------------------------
    content_hash: str = _knob(
        "xxh3-128",
        Kind.BUILD,
        "Content-address hash H. blake2b-128 is the stdlib no-dep fallback.",
    )
    single_instance_lock: bool = _knob(
        True,
        Kind.BUILD,
        "Single-instance advisory lockfile beside the DB (single owner).",
    )

    @field_validator("redact_before_egress_patterns", "redact_before_index_patterns")
    @classmethod
    def _redaction_patterns_compile(cls, patterns: list[str]) -> list[str]:
        """Fail loudly at load if any redaction pattern is not a valid regex.

        These run on every egress/index payload, so a bad regex must surface at
        config-load time, not at the first (precondition) Claude call.
        """
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid redaction regex {pattern!r}: {exc}") from exc
        return patterns

    @field_validator("no_egress_scopes")
    @classmethod
    def _no_egress_scopes_must_be_matchable(
        cls, rules: list[NoEgressScopeRule]
    ) -> list[NoEgressScopeRule]:
        """Fail loudly at load on any scope rule that could never match.

        One governing rule, three cases: a privacy rule that silently matches
        nothing is worse than no rule at all, because the user believes they
        are covered. This ticket's acceptance criteria forbid it explicitly
        (``lode-35nu.11.8``), so an empty ``match``, an unsupported
        ``source_type``, and ``source_type="confluence"`` are all refused here
        rather than accepted as silent no-ops.

        Confluence gets its own message because its reason is structural, not
        a typo: ``drawdown.py``'s ``_CONFLUENCE_PAGE_RE`` discards the space
        key at detection time, so ``external_id`` for a Confluence external
        carries only the numeric page id and a space-scoped rule has nothing
        to match against. See ``docs/externals.md`` "No-egress scope rules".
        """
        for rule in rules:
            if not rule.match.strip():
                raise ValueError(
                    "no_egress_scopes: 'match' must not be empty -- an empty "
                    "rule cannot express any scope, and a privacy rule that "
                    "matches nothing must fail loudly at load rather than "
                    "silently withhold nothing."
                )
            if rule.source_type == "confluence":
                raise ValueError(
                    "no_egress_scopes: source_type='confluence' is not "
                    "supported -- Confluence space-key scoping is "
                    "structurally impossible (the space key is discarded at "
                    "detection time and stored nowhere; see "
                    "docs/externals.md 'No-egress scope rules'). Supported "
                    "source_type values: 'jira', 'web'."
                )
            if rule.source_type not in SCOPED_SOURCE_TYPES:
                raise ValueError(
                    f"no_egress_scopes: unsupported source_type "
                    f"{rule.source_type!r} -- a rule declared for it could "
                    f"never match any external. Supported source_type "
                    f"values: {', '.join(repr(t) for t in SCOPED_SOURCE_TYPES)}."
                )
        return rules

    @field_validator("jira_base_url", "confluence_base_url")
    @classmethod
    def _base_url_valid_or_empty(cls, value: str) -> str:
        """Fail loudly at load if a non-empty base-URL override is malformed.

        Empty is the documented "infer at detection time" default (lode-gpzn.1)
        and always passes; anything else must be a well-formed http(s) URL, so
        a typo'd override surfaces at ``Settings()`` construction rather than
        as an opaque request failure from the fetch unit later.
        """
        if value == "":
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                f"invalid base URL {value!r}: must be a well-formed http(s) URL"
            )
        return value

    @model_validator(mode="after")
    def _azure_api_version_required_with_endpoint(self) -> Settings:
        """Fail loudly at load if azure_openai_endpoint is set with no api-version.

        docs/stack.md "6. Config shape": azure_openai_api_version is "required
        when azure_openai_endpoint is set" -- an Azure endpoint with no
        api-version would otherwise surface as an opaque request failure from
        the OpenAI SDK/HTTP layer at the first call, rather than at config load
        (lode-568v.3).
        """
        if self.azure_openai_endpoint and not self.azure_openai_api_version:
            raise ValueError(
                "azure_openai_api_version is required when azure_openai_endpoint is set"
            )
        return self

    @model_validator(mode="after")
    def _reasoning_effort_legal_for_provider(self) -> Settings:
        """Fail loudly at load if a tier's reasoning_effort isn't legal for llm_provider.

        Every ``ModelTier`` knob is ``Kind.RUNTIME`` and comes from a static
        ``config.toml``, so a typo'd ``reasoning_effort`` is knowable at load.
        Left to the provider seam it instead surfaced at the first API call,
        where ``worker.run_one``'s generic arm books it as *transient* and
        dead-letters the job -- see ``docs/configuration.md`` for the full
        rationale (lode-tvps). Checks the effort *value* only: the value/model
        *pairing* stays deliberately unpredicted (lode-3dlt option 1), and a
        rejected pairing still surfaces as an ``LLMProviderError`` at the seam,
        not here.

        Tiers are found by *type*, using the same ``isinstance(..., ModelTier)``
        predicate :func:`knob_rows` uses, rather than by a hard-coded name list
        -- so a tier added later is covered without touching this method, which
        is exactly the drift that would silently reopen this bug for it.
        """
        legal_levels = EFFORT_LEVELS_BY_PROVIDER[self.llm_provider]
        for tier_name in type(self).model_fields:
            tier = getattr(self, tier_name)
            if not isinstance(tier, ModelTier):
                continue
            effort = tier.reasoning_effort
            if effort is not None and effort not in legal_levels:
                raise ValueError(
                    f"{tier_name}.reasoning_effort={effort!r} is not legal for "
                    f"llm_provider={self.llm_provider!r} -- must be one of "
                    f"{list(legal_levels)}"
                )
        return self


def knob_kinds() -> dict[str, str]:
    """Map each knob name to its kind tag (runtime/tune/build)."""
    return {
        name: field.json_schema_extra["kind"]
        for name, field in Settings.model_fields.items()
        if isinstance(field.json_schema_extra, dict)
        and "kind" in field.json_schema_extra
    }


def load_settings(**overrides: object) -> Settings:
    """Construct and validate settings from every configured source.

    Layers, lowest to highest precedence: the field defaults declared above,
    then the optional ``$LODE_HOME/config.toml`` file if one is present
    (``docs/configuration.md`` "Paths & locations" -- the one user-editable
    file for runtime knobs), then ``overrides`` supplied by the caller (a CLI
    flag, a test fixture). An explicit override therefore always wins over
    whatever the file says -- unless its value is ``None``, which is treated as
    *not supplied* and dropped before merging. ``None`` is the shape a Typer
    flag takes when the user didn't pass it (``top_k: int | None = None``), so
    passing it straight through as ``**overrides`` would silently clobber a
    ``config.toml`` value back to the field default every time that flag is
    left off the command line (lode-n8n). No current knob's meaningful value is
    ``None`` (no field is ``Optional``), so this is unambiguous; if one ever
    needs to accept ``None`` as a deliberate value, this filter is the thing to
    revisit.

    A missing file is a valid, fully-working state -- every knob just uses its
    default -- so this never raises on that account; an invalid value, from the
    file or from ``overrides``, raises at construction either way, since
    pydantic validates every field. ``extra="forbid"`` still rejects an
    unrecognized key from either source, ``None``-valued or not: the drop above
    applies only to keys ``Settings`` actually declares, so a typo'd override
    name surfaces as a ``ValidationError`` instead of vanishing silently.

    This is the one place a caller should resolve settings for a whole
    command/session and thread the result down -- see ``lode.cli``'s
    ``ask``/``work``/``add``/``tui`` entry points (lode-40g); everywhere else a
    bare ``Settings()`` fallback (``settings = settings or Settings()``) is the
    correct default for a function accepting an *optional* caller-supplied
    override, not a second place to resolve the file/overrides layering.
    """
    path = config_path()
    file_values: dict[str, object] = {}
    if path.is_file():
        with path.open("rb") as handle:
            file_values = tomllib.load(handle)
    # Back-compat rename chain (lode-568v.2, then lode-7y6s): a config.toml
    # still carrying either old key keeps working rather than tripping
    # extra="forbid" -- docs/stack.md "Config shape". The hops run
    # oldest-first so each one's output feeds the next; that ORDER IS
    # LOAD-BEARING, not incidental. Only applies to the file layer; overrides
    # (CLI flags, tests) are expected to already use the current name.
    if "anthropic_call_timeout_s" in file_values:
        file_values.setdefault(
            "llm_call_timeout_s", file_values.pop("anthropic_call_timeout_s")
        )
    if "llm_call_timeout_s" in file_values:
        file_values.setdefault(
            "enrich_call_timeout_s", file_values.pop("llm_call_timeout_s")
        )
    supplied = {
        k: v
        for k, v in overrides.items()
        if v is not None or k not in Settings.model_fields
    }
    return Settings(**{**file_values, **supplied})


# --- Atlassian connector credentials (JIRA + Confluence Cloud, lode-gpzn.1) --
# Resolution order per product: the documented env var (JIRA_TOKEN_ENV /
# JIRA_EMAIL_ENV / CONFLUENCE_TOKEN_ENV / CONFLUENCE_EMAIL_ENV above) first,
# then the matching Settings/config.toml field as fallback. A missing token OR
# email resolves to None -- a clean "connector inactive" state (locked
# decision 5, lode-gpzn epic), never an exception; the connector falls through
# to the generic web fetcher. Deliberately NOT modeled on lode.auth.build_client
# (the Anthropic SDK credential chain): that chain never reads config.toml at
# all, whereas this ticket's locked design explicitly wants a config.toml
# fallback, so it needed its own resolver rather than reusing that one.


@dataclass(frozen=True)
class AtlassianCredentials:
    """Resolved Basic-auth credentials for one Atlassian Cloud product.

    ``__repr__`` is overridden so the token never appears in a traceback, a
    ``repr()``, or an incautious ``logger.debug(creds)`` -- the acceptance
    criterion (lode-gpzn.1) is "never logged or echoed anywhere", not just
    "never logged deliberately".
    """

    email: str
    token: str

    def __repr__(self) -> str:
        return f"AtlassianCredentials(email={self.email!r}, token='***redacted***')"


def _resolve_atlassian_credentials(
    token_env: str, email_env: str, config_token: str, config_email: str
) -> AtlassianCredentials | None:
    """Env-var-primary, config.toml-fallback resolution shared by both products."""
    token = os.environ.get(token_env) or config_token or None
    email = os.environ.get(email_env) or config_email or None
    if token is None or email is None:
        return None
    return AtlassianCredentials(email=email, token=token)


def resolve_jira_credentials(settings: Settings) -> AtlassianCredentials | None:
    """Resolve JIRA Cloud Basic-auth credentials, or ``None`` if unresolved.

    ``None`` is the clean "connector inactive" state -- not an error -- for
    either a missing token or a missing account email, from either source.
    """
    return _resolve_atlassian_credentials(
        JIRA_TOKEN_ENV, JIRA_EMAIL_ENV, settings.jira_token, settings.jira_email
    )


def resolve_confluence_credentials(settings: Settings) -> AtlassianCredentials | None:
    """Resolve Confluence Cloud Basic-auth credentials, or ``None`` if unresolved.

    ``None`` is the clean "connector inactive" state -- not an error -- for
    either a missing token or a missing account email, from either source.
    """
    return _resolve_atlassian_credentials(
        CONFLUENCE_TOKEN_ENV,
        CONFLUENCE_EMAIL_ENV,
        settings.confluence_token,
        settings.confluence_email,
    )


def jira_active(settings: Settings) -> bool:
    """True iff JIRA is flagged on AND credentials resolve (locked decision 5).

    The single check gpzn.2's link-detection routing and gpzn.3's fetch unit
    are expected to make before treating a JIRA link as API-connector
    territory rather than falling through to the generic web fetcher.
    """
    return settings.jira_enabled and resolve_jira_credentials(settings) is not None


def confluence_active(settings: Settings) -> bool:
    """True iff Confluence is flagged on AND credentials resolve (locked decision 5).

    The single check gpzn.2's link-detection routing and gpzn.4's fetch unit
    are expected to make before treating a Confluence link as API-connector
    territory rather than falling through to the generic web fetcher.
    """
    return (
        settings.confluence_enabled
        and resolve_confluence_credentials(settings) is not None
    )


# --- On-disk layout (docs/configuration.md "Paths & locations") --------------
# Everything lode persists lives under one user-controllable root, $LODE_HOME
# (default ~/.lode): the DB (+ its sibling lock), the LanceDB vector store, and
# the log directory. These helpers are the single place the layout is resolved,
# so the CLI/TUI surface (lode-ftc) reads it rather than re-deriving it. $LODE_HOME
# replaces the older $LODE_DB env binding (lode-qd9); --db stays an explicit
# per-invocation override of just the DB file (used by tests).

#: Env var naming the single on-disk root for all of lode's state.
LODE_HOME_ENV = "LODE_HOME"

_DEFAULT_HOME = "~/.lode"


def lode_home() -> Path:
    """Resolve the on-disk root: ``$LODE_HOME`` (default ``~/.lode``), expanded."""
    raw = os.environ.get(LODE_HOME_ENV) or _DEFAULT_HOME
    return Path(raw).expanduser()


def default_db_path() -> Path:
    """The SQLite DB path under the root: ``$LODE_HOME/lode.db``.

    The single-instance advisory lock lives beside it as ``lode.db.lock``.
    """
    return lode_home() / "lode.db"


def lance_dir(db_path: Path) -> Path:
    """The LanceDB vector store as a ``lancedb/`` sibling of the DB file.

    Derived from the resolved DB path rather than the root directly, so an
    explicit ``--db`` override co-locates its vector store beside the chosen DB
    (and the default ``$LODE_HOME/lode.db`` still yields ``$LODE_HOME/lancedb/``).
    Capture-side embed and read-side dense search therefore read/write the *same*
    store. (Where vectors physically land once the embed leg is wired into the
    capture path is lode-1f9's cache-composition territory; this just keeps the
    path one documented value, lode-qd9 / lode-bkc.)
    """
    return db_path.parent / "lancedb"


def log_dir() -> Path:
    """The application log directory under the root: ``$LODE_HOME/logs/``."""
    return lode_home() / "logs"


def model_cache_dir() -> Path:
    """The local-model weights cache under the root: ``$LODE_HOME/models/``.

    Passed as ``cache_dir`` to every ``fastembed`` model loader (embedder,
    reranker, NLI/entailment cross-encoder — all three load through the same
    ``fastembed`` model-management path). Without an explicit ``cache_dir``,
    ``fastembed`` defaults to ``tempfile.gettempdir()/fastembed_cache`` — a
    directory WSL wipes on reboot (and ``systemd-tmpfiles`` clears on many
    distros) — so the pinned ~500MB of ONNX weights would otherwise be
    silently re-downloaded from HuggingFace on a semi-regular basis instead of
    paying that cost once (lode-gmo). Living under ``$LODE_HOME`` keeps the
    weights cache on the same durable, user-controllable root as the
    DB/vectors/logs. ``fastembed`` creates the directory itself
    (``mkdir(parents=True, exist_ok=True)``) on first load, so nothing here
    needs to pre-create it.
    """
    return lode_home() / "models"


#: On-disk cache identity for lode's pinned local models (``embedding_model``
#: and the shared ``rerank_model``/``entailment_model`` id, lode-txh.6) --
#: ``fastembed``'s own ``list_supported_models()`` entry for each, reduced to
#: just what a filesystem cache probe needs: the HuggingFace repo id its
#: downloader actually caches weights under (``sources.hf``, which can differ
#: from the friendly model id lode's settings carry -- e.g.
#: ``BAAI/bge-small-en-v1.5`` caches under ``qdrant/bge-small-en-v1.5-onnx-q``)
#: and the specific weight file within it (``model_file``).
#:
#: Pinned here so ``lode status``'s cold-cache probe (lode-l38d.6) can answer
#: "is this repo already on disk" with a pure
#: ``huggingface_hub.try_to_load_from_cache`` filesystem lookup and NEVER
#: ``import fastembed`` -- that import drags in onnxruntime + numpy (~830
#: modules, ~740ms warm), which measurably slowed a pure-sqlite-read command
#: just to print "No action needed." (the lode-l38d.6 review escalation this
#: pin resolves, decided 2026-07-16).
#:
#: Measured on the WARM path -- the steady state -- as ``lode status`` min-of-11,
#: two interleaved passes on one machine (the only fair shape: figures taken on
#: different machines/loads are not comparable, which is what made the
#: escalation and the implementing run disagree 2.4x about this very cost):
#:
#: ===========================  =========  =========
#: ``lode status``              pass 1     pass 2
#: ===========================  =========  =========
#: trunk, no probe               864ms      919ms
#: probe via ``import fastembed``  1320ms   1244ms
#: probe via this pin            1019ms     1039ms
#: ===========================  =========  =========
#:
#: So the probe's real cost was ~+330-460ms (~1.4x, NOT the "2-4x" the
#: escalation reported and this comment once repeated -- that figure came from a
#: 3.03s outlier no later run reproduced), and the pin removes ~65-70% of it.
#: The residual ~+120-155ms is ``huggingface_hub``'s own import and is the
#: accepted price of the hint: the pin does NOT make ``lode status`` as fast as
#: trunk, and cannot -- it strictly adds work to a pure DB read.
#:
#: Keyed by the model id LOWERCASED, matching fastembed's own
#: case-insensitive resolution (``ModelManagement._get_model_description``
#: compares ``model_name.lower()``) -- a ``config.toml`` override spelled in a
#: different case still probes correctly.
#:
#: DRIFT GUARD: a fastembed upgrade could in principle change a pinned id's
#: ``sources.hf``/``model_file``. ``tests/test_model_cache_identity.py``
#: asserts this dict still matches the installed fastembed's
#: ``list_supported_models()`` -- that test may import fastembed; this module
#: and ``cli.status``'s cold-cache probe must not.
_MODEL_CACHE_IDENTITY: dict[str, tuple[str, str]] = {
    "nomic-ai/nomic-embed-text-v1.5": (
        "nomic-ai/nomic-embed-text-v1.5",
        "onnx/model.onnx",
    ),
    "baai/bge-reranker-base": ("BAAI/bge-reranker-base", "onnx/model.onnx"),
}


def model_cache_identity(model_name: str) -> tuple[str, str] | None:
    """Pinned ``(hf_source_repo_id, model_file)`` for ``model_name``, or ``None``.

    Case-insensitive lookup against :data:`_MODEL_CACHE_IDENTITY`, matching
    ``fastembed``'s own resolution. Returns ``None`` for any model id outside
    lode's two pinned models (e.g. a user's custom ``config.toml`` override) --
    callers fall back to importing ``fastembed`` to resolve those, same as
    before this id set was pinned.
    """
    return _MODEL_CACHE_IDENTITY.get(model_name.lower())


def hf_hub_offline() -> bool:
    """Mirror ``fastembed``'s own ``HF_HUB_OFFLINE`` truthiness check.

    Read directly rather than imported -- ``fastembed`` does not expose this
    as a reusable helper (``fastembed/common/model_management.py:398-401``
    inlines it) -- and must stay the same truthy set ``fastembed`` itself
    checks, or a caller's offline branch would misclassify a failure
    ``fastembed`` would not actually have treated as offline.

    Shared by :mod:`lode.cli` (:func:`_warm`'s offline/cold-cache branch,
    ``lode-96t``) and :mod:`lode.embedding` (:func:`resolve_model_revision`'s
    offline short-circuit, ``lode-r4r2``) -- moved here rather than kept as a
    ``cli.models``-private helper once a second module needed the identical
    check.
    """
    return os.environ.get("HF_HUB_OFFLINE", "").strip().upper() in {
        "1",
        "TRUE",
        "YES",
        "ON",
    }


def config_path() -> Path:
    """The optional user config file under the root: ``$LODE_HOME/config.toml``.

    **Optional** — if absent, every knob uses its documented default; no config
    file is a valid, fully-working state (``docs/configuration.md``). Resolved
    here so the CLI/TUI surface (lode-ftc) reads the layout rather than
    re-deriving it.
    """
    return lode_home() / "config.toml"


def config_rows(db_path: Path) -> list[tuple[str, str, str]]:
    """Return ``(label, value, note)`` triples for the resolved on-disk locations.

    THE one row computation behind both surfaces (lode-l38d.4): the CLI's
    ``lode config`` renders these triples straight into a rich ``Table``, and
    :func:`config_lines` formats the same triples into the pre-padded text the
    TUI's Ctrl+O screen shows. Two shapes of one list, not two independently
    maintained ones.

    ``note`` is the parenthetical annotation (``$LODE_HOME``/``default``,
    ``present``/``absent``) for the two rows that carry one, and ``""`` for the
    rest — bare, without parens, so each renderer decides how to present it
    (the CLI gives it a real ``Note`` column; ``config_lines`` bakes it into the
    text as ``(...)``).
    """
    lock_file = lock_path(db_path)
    cfg = config_path()
    home_source = "$LODE_HOME" if os.environ.get(LODE_HOME_ENV) else "default"
    config_state = "present" if cfg.exists() else "absent"
    return [
        ("LODE_HOME", str(lode_home()), home_source),
        ("database", str(db_path), ""),
        ("db lock", str(lock_file), ""),
        ("vector store", str(lance_dir(db_path)), ""),
        ("model cache", str(model_cache_dir()), ""),
        ("logs", str(log_dir()), ""),
        ("config", str(cfg), config_state),
    ]


def config_lines(db_path: Path) -> list[str]:
    """Render the resolved on-disk locations as aligned ``label  path`` lines.

    The text shape of :func:`config_rows` — the ONE shared row computation behind
    both ``lode config`` (:mod:`lode.cli`) and
    the TUI's Ctrl+O diagnostics screen (:mod:`lode.tui.screens.config`) — lode-u5gh
    collapsed what used to be two independently-maintained copies (lode-3r4,
    lode-ak6) after they had already drifted once (lode-ak6 added the model-cache
    row to the CLI by hand; the TUI's mirrored screen did not get it, because
    nothing connected the two lists). The human decision on lode-u5gh: there is
    no product reason for the TUI to show fewer rows than the CLI, so this is a
    single list both views render — a row added here reaches both, structurally,
    not by remembering to update two places.

    Takes an **already-resolved** ``db_path`` — callers own their own ``--db``
    (CLI) / ``self.app.db_path`` (TUI) resolution; this only derives the lock
    and vector-store paths that live beside it. The root, model cache, log dir,
    and ``config.toml`` come from ``$LODE_HOME``. Whether ``$LODE_HOME`` is set
    in the environment (vs the ``~/.lode`` default) and whether the optional
    ``config.toml`` is present are surfaced inline.

    lode-l38d.4: the TUI's Ctrl+O screen keeps rendering this pre-padded text
    unchanged (via ``Static``); the CLI's ``lode config`` moved to a
    terminal-width-aware rich ``Table`` fed by :func:`config_rows` directly, so
    the two surfaces' exact output text is no longer identical byte-for-byte
    (their underlying DATA still comes from the one computation, ``config_rows``).
    """
    rows = config_rows(db_path)
    formatted = [
        (label, f"{value}  ({note})" if note else value) for label, value, note in rows
    ]
    width = max(len(label) for label, _ in formatted)
    return [f"{label:<{width}}  {value}" for label, value in formatted]


#: Presence-indicator placeholders a ``secret=True`` knob row renders instead
#: of its raw value (lode-dx4r) -- never the value itself, from any source.
REDACTED_PLACEHOLDER = "[REDACTED]"
UNSET_PLACEHOLDER = "[unset]"

#: The env var that resolves each ``secret=True`` credential field, env-var
#: PRIMARY over the field's own config.toml value (mirrors
#: ``_resolve_atlassian_credentials``'s per-field formula). Used by
#: :func:`knob_rows` to compute presence WITHOUT reading the raw value back
#: out of ``settings`` for display -- only ever to test truthiness.
_CREDENTIAL_ENV_VARS: dict[str, str] = {
    "jira_email": JIRA_EMAIL_ENV,
    "jira_token": JIRA_TOKEN_ENV,
    "confluence_email": CONFLUENCE_EMAIL_ENV,
    "confluence_token": CONFLUENCE_TOKEN_ENV,
}


def _credential_resolved(settings: Settings, name: str) -> bool:
    """True iff the named ``secret=True`` credential field resolves from any source.

    Env var first, then the ``config.toml``-fallback field on ``settings`` --
    the same env-primary/config-fallback shape ``_resolve_atlassian_credentials``
    uses per field, deliberately re-derived here (rather than calling the
    resolver) so a *partial* resolution still reports presence per key: the
    resolver only returns non-``None`` when BOTH email and token resolve
    together, but this ticket's acceptance is per-field ("export the token
    only" must still show a row for the token) -- see lode-dx4r.
    """
    env_var = _CREDENTIAL_ENV_VARS[name]
    return bool(os.environ.get(env_var)) or bool(getattr(settings, name))


def knob_rows(settings: Settings) -> list[tuple[str, str, str]]:
    """Return ``(name, current value, kind)`` for every runtime/tune knob.

    The ONE shared builder behind both ``lode config`` (:mod:`lode.cli`) and
    the TUI's ``ConfigScreen`` knob table (:mod:`lode.tui.screens.config`) --
    lode-juz8.6's widening of the config surface past the on-disk paths
    :func:`config_lines` already covers, following the same "one row list,
    not two, feeds both surfaces" architecture lode-u5gh established for
    paths. Iterates ``Settings.model_fields`` in declaration order, filtered
    to :attr:`Kind.RUNTIME` / :attr:`Kind.TUNE` via :func:`knob_kinds` --
    :attr:`Kind.BUILD` knobs (the local-model ids, ``content_hash``, ...) are
    excluded, since changing one implies a rebuild/migration rather than a
    live retune (docs/configuration.md).

    Reads the CURRENT resolved value off the given ``settings`` (already
    layered defaults <- config.toml <- overrides by :func:`load_settings`),
    so this works with no ``config.toml`` present -- ``settings`` then just
    holds every field's default (``Settings()``). A list-valued knob
    (``url_tracking_param_blocklist``, the redaction pattern sets) renders
    comma-joined on one line, so the table stays one row per knob.

    Only the row DATA is shared -- each surface renders it in its own idiom
    (the CLI as aligned text, the TUI as a ``DataTable`` widget), per the
    ticket's design: "TUI renders it in a table widget ... CLI prints
    aligned rows. Both call the one shared builder."

    A field declared ``secret=True`` (the four Atlassian credential fields --
    ``jira_email`` / ``jira_token`` / ``confluence_email`` /
    ``confluence_token``, lode-gpzn.1 / lode-dx4r) still gets a row, but its
    value is NEVER the raw setting -- it's :data:`REDACTED_PLACEHOLDER` when
    the credential resolves from *either* the env var or the ``config.toml``
    fallback (:func:`_credential_resolved`), else :data:`UNSET_PLACEHOLDER`.
    Presence is deliberately computed from the env var / resolver inputs, not
    from ``settings``'s field value alone -- reading only the ``Settings``
    value would show "unset" for an env-only credential (env vars never flow
    into ``Settings``), silently reproducing the exact gap this ticket closes.
    """
    rows: list[tuple[str, str, str]] = []
    for name, kind in knob_kinds().items():
        if kind not in (Kind.RUNTIME.value, Kind.TUNE.value):
            continue
        field = Settings.model_fields[name]
        is_secret = isinstance(
            field.json_schema_extra, dict
        ) and field.json_schema_extra.get("secret", False)
        if is_secret:
            value: object = (
                REDACTED_PLACEHOLDER
                if _credential_resolved(settings, name)
                else UNSET_PLACEHOLDER
            )
        else:
            value = getattr(settings, name)
            if isinstance(value, list):
                value = ", ".join(str(item) for item in value)
            elif isinstance(value, ModelTier):
                # Render the bare model/deployment id (matching the pre-seam
                # str-valued knobs, lode-568v.2), appending effort/max_tokens
                # only when set -- str(ModelTier) would otherwise print the
                # pydantic repr "model='...' reasoning_effort=None
                # max_tokens=None" in `lode config` + the TUI ConfigScreen
                # (both feed knob_rows straight to display). max_tokens
                # (lode-d70n) follows the same "only when set" rule effort
                # already established.
                parts = []
                if value.reasoning_effort is not None:
                    parts.append(f"effort={value.reasoning_effort}")
                if value.max_tokens is not None:
                    parts.append(f"max_tokens={value.max_tokens}")
                value = f"{value.model} ({', '.join(parts)})" if parts else value.model
        rows.append((name, str(value), kind))
    return rows
