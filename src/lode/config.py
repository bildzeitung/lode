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
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lode.lock import lock_path


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
    anthropic_call_timeout_s: float = _knob(
        120.0,
        Kind.RUNTIME,
        "Per-call client-side timeout (seconds) passed to the Anthropic "
        "enrichment calls reachable from 'lode work' (enrich.py): the Batches "
        "API pre-steps (client.beta.messages.batches.create/retrieve/results) "
        "and the immediate Haiku call (client.messages.create) a residual "
        "enrich job can take in drain()'s main loop -- bounds a hung network "
        "call rather than letting it block 'lode work' forever (lode-olmi.15). "
        "Distinct from fetch_timeout_s, which governs web draw-down HTTP "
        "fetches, not Anthropic API calls.",
        gt=0.0,
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
    supplied = {
        k: v
        for k, v in overrides.items()
        if v is not None or k not in Settings.model_fields
    }
    return Settings(**{**file_values, **supplied})


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
#: and ``cli.py``'s cold-cache probe must not.
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


def config_path() -> Path:
    """The optional user config file under the root: ``$LODE_HOME/config.toml``.

    **Optional** — if absent, every knob uses its documented default; no config
    file is a valid, fully-working state (``docs/configuration.md``). Resolved
    here so the CLI/TUI surface (lode-ftc) reads the layout rather than
    re-deriving it.
    """
    return lode_home() / "config.toml"


def config_lines(db_path: Path) -> list[str]:
    """Render the resolved on-disk locations as aligned ``label  path`` lines.

    The ONE shared row-builder behind both ``lode config`` (:mod:`lode.cli`) and
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
    """
    lock_file = lock_path(db_path)
    cfg = config_path()
    home_source = "$LODE_HOME" if os.environ.get(LODE_HOME_ENV) else "default"
    config_state = "present" if cfg.exists() else "absent"
    rows = [
        ("LODE_HOME", f"{lode_home()}  ({home_source})"),
        ("database", str(db_path)),
        ("db lock", str(lock_file)),
        ("vector store", str(lance_dir(db_path))),
        ("model cache", str(model_cache_dir())),
        ("logs", str(log_dir())),
        ("config", f"{cfg}  ({config_state})"),
    ]
    width = max(len(label) for label, _ in rows)
    return [f"{label:<{width}}  {value}" for label, value in rows]


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
    """
    rows: list[tuple[str, str, str]] = []
    for name, kind in knob_kinds().items():
        if kind not in (Kind.RUNTIME.value, Kind.TUNE.value):
            continue
        value = getattr(settings, name)
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        rows.append((name, str(value), kind))
    return rows
