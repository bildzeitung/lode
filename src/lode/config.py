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

The local-model ids (embedder vector dim, reranker, NLI loader) are pinned for
real in ``lode-txh.6``; the placeholders here keep config loadable until then.
The redaction pattern *sets* are the high-precision seed (``lode-fk8.2``) that
drives :mod:`lode.redact`'s redact-before-index / redact-before-egress controls;
each pattern is validated to compile at load.
"""

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Kind(str, Enum):
    """How a knob may change — mirrors the Kind column in docs/configuration.md."""

    RUNTIME = "runtime"
    TUNE = "tune"
    BUILD = "build"


def _knob(default: object, kind: Kind, doc: str, **constraints: object) -> object:
    """A typed field carrying its kind tag (read back via :func:`knob_kinds`)."""
    return Field(
        default, description=doc, json_schema_extra={"kind": kind.value}, **constraints
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
        "bge-reranker-v2-m3",
        Kind.TUNE,
        "Local cross-encoder reranker, ONNX. Swappable.",
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
        "bge-reranker-v2-m3",
        Kind.TUNE,
        "Local NLI / cross-encoder for citation entailment; loader pinned in lode-txh.6.",
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
    enrichment_batch_flush_interval_s: int = _knob(
        60,
        Kind.RUNTIME,
        "Max wait before flushing an enrich batch (time policy).",
        gt=0,
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
        "Size/similarity delta gating paid re-enrichment of a changed snapshot.",
        ge=0.0,
        le=1.0,
    )
    drawdown_hop_limit: int = _knob(
        1, Kind.BUILD, "Follow explicit links this many hops, then stop.", ge=0
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

    # --- Models ---------------------------------------------------------------
    embedding_model: str = _knob(
        "nomic-embed-text-v1.5",
        Kind.BUILD,
        "Local ONNX embedder; a change re-keys the vector space. Pinned + dim in lode-txh.6.",
    )
    enrichment_llm: str = _knob(
        "claude-haiku-4-5",
        Kind.RUNTIME,
        "High-volume background extraction LLM (Claude Haiku 4.5).",
    )
    qa_llm: str = _knob(
        "claude-sonnet-4-6",
        Kind.RUNTIME,
        "Default interactive Q&A synthesis LLM (Claude Sonnet 4.6).",
    )
    qa_think_harder_llm: str = _knob(
        "claude-opus-4-8",
        Kind.RUNTIME,
        "Higher-quality 'think harder' Q&A LLM on demand (Claude Opus 4.8).",
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


def knob_kinds() -> dict[str, str]:
    """Map each knob name to its kind tag (runtime/tune/build)."""
    return {
        name: field.json_schema_extra["kind"]
        for name, field in Settings.model_fields.items()
        if isinstance(field.json_schema_extra, dict)
        and "kind" in field.json_schema_extra
    }


def load_settings(**overrides: object) -> Settings:
    """Construct and validate settings; invalid overrides raise at load."""
    return Settings(**overrides)
