"""Haiku structured-output enrichment + provenance (lode-npx.1 / lode-npx.2).

Extracts tags, entities, and inferred concept edges from a note version OR an
external snapshot (lode-7qi) via Claude Haiku structured outputs (tool-use) +
Pydantic validation. Records full provenance on every result. Inferred edges
are AI suggestions with confidence scores, stored as ``source='ai'`` -- never
asserted facts.

**Polymorphic target (lode-7qi):** every entry point (:func:`enrich_version`,
:func:`submit_enrich_batch`, :func:`collect_enrich_batch`) takes the same
``target_version`` a ``jobs`` row carries -- a note ``version_id`` or an
external ``snapshot_id``, with no flag riding along to say which (mirrors
:func:`lode.embedding._version_body`'s blind versions-then-snapshots
resolution: an enrich job enqueued by :func:`lode.externals.gate_reenrich`
looks identical to one enqueued by :mod:`lode.reconcile`'s enrich-gap step).
:func:`_resolve_enrich_target` is the one place that resolution happens;
everything downstream writes against the resolved ``owner_id`` (``note_id`` or
``external_id`` -- the same polymorphic id ``annotations.target`` /
``edges.from_id`` already accept, ``src/lode/schema.sql``).

Architecture (docs/storage.md):

- Tags + entities + a one-line summary → ``annotations`` table, ``source='ai'``,
  ``kind='tag'`` / ``'entity'`` / ``'summary'``, whole-item scope
  (``target = owner_id``). ``summary`` (lode-0wj.9) is a single Haiku-derived
  sentence describing the head version/snapshot -- same row shape, same
  provenance, same idempotent replace as tag/entity.
- Inferred edges → ``edges`` table, ``source='ai'``, with ``confidence``,
  ``reason``, and ``source_version``. These are Haiku suggestions; user curation
  (``source='user'``) is separate and irreplaceable (pinned to ``owner_id``).
- Full provenance on every row: ``model``, ``prompt_ver`` (:data:`ENRICH_PROMPT_VER`),
  ``source_version`` (the version_id/snapshot_id enriched), ``created`` (ISO-8601 UTC).
- Egress gate: ``no_egress`` notes/externals are never sent to Haiku;
  ``redact_before_egress`` strips secrets before the payload leaves the box.
  One ``egress_log`` row is written per enrichment call (``purpose='enrich'``).
- Idempotent: deletes existing ``source='ai'`` rows keyed to the enriched
  ``source_version`` before writing new results, so re-running on the same version
  converges cleanly.
- **User pinning (lode-npx.4):** before inserting a suggestion, checks
  :func:`lode.curation.is_annotation_suppressed` / ``is_edge_suppressed`` --
  if a ``source='user'`` row already exists for the exact same item (a prior
  user edit, or a tombstone left by :func:`lode.curation.delete_annotation` /
  ``delete_edge``), the AI duplicate is skipped. This is what makes a
  user-deleted link stay deleted across re-enrichment.

Two enrichment routes (lode-npx.2, docs/storage.md §"Enrichment latency"):

- **Immediate** (:func:`enrich_version`): fresh note on the capture path — one
  direct Haiku call, results land before the CLI returns.
- **Batch** (:func:`submit_enrich_batch` + :func:`collect_enrich_batch`): bulk /
  backfill / re-enrichment via the 50%-off Anthropic Batches API. The worker's
  drain loop pre-submits pending ``enrich`` jobs as a single Batch, persists the
  ``batch_handle`` on each job row, and collects results on the next pass (or
  after restart — lode-i05.5 durability). Either way the embedding lands in the
  async worker, regardless of enrichment latency.

The ``enrich`` job type (now used only for bulk / backfill) is claimed by the
worker batch-submit step; the reconciliation scan (lode-i05.4 ``enrich_gap`` step,
wired in :mod:`lode.reconcile`) re-enqueues any head version missing a live enrich
job so gaps are self-healing.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from lode import jobs
from lode.config import Settings
from lode.curation import is_annotation_suppressed, is_edge_suppressed
from lode.egress import log_egress
from lode.ids import short_version_id
from lode.llm_provider import (
    BatchRequest,
    LLMProvider,
    build_provider,
    provider_identity,
)
from lode.redact import redact_before_egress_counting

# `lode.llm_provider`'s own `import anthropic` is deferred inside
# AnthropicProvider's construction path (via lode.auth.build_client), never at
# module level -- this module never imports the SDK directly, keeping
# `import lode.enrich` cheap (lode-4q97): lode.reconcile imports
# ENRICH_PROMPT_VER from here at module level, and `lode work` runs a
# reconcile pass on EVERY invocation, so an eager SDK import here would make a
# credential-free, embed-only drain pay for the SDK on every run, to do
# nothing with it.

log = logging.getLogger(__name__)

#: Prompt version -- baked into every enrichment provenance row so a model or
#: prompt change can trigger corpus-wide re-enrichment via the reconcile scan.
#: Bumped to v2 for the ``summary`` field (lode-0wj.9); bumped to v3 to
#: front-load the lede in ``summary`` so a 1-line-truncated browse view still
#: carries the note's point (lode-juz8.5).
ENRICH_PROMPT_VER = "npx1-v3"

#: Tool name used to force Haiku into structured output via tool-use calling.
_TOOL_NAME = "extract_enrichment"

#: Output cap for the forced tool-use extraction call -- shared by both routes
#: (:func:`_call_haiku` and :func:`_build_batch_request`, which must send the
#: byte-for-byte identical value, ``lode-568v.2``'s wire-equivalence bar).
#: Raised 1024 -> 2048 (lode-jgus) for the same reason
#: :data:`lode.qa.MAX_TOKENS` was raised 4096 -> 8192 (lode-3dlt): the forced
#: tool-use branch of :meth:`~lode.llm_provider.AnthropicProvider.structured_call`
#: never sends ``thinking`` at all -- a property it already had before
#: lode-d1sr/lode-3dlt ever touched the ``messages.parse`` branch, so it never
#: hit the Fable-class 400 that fix exists to dodge. But ``enrichment_llm`` is
#: ``Kind.RUNTIME``, and omitting ``thinking`` does not disable it -- each
#: model still runs its own default. A user override to a thinking-capable
#: model (Opus 5, Sonnet 5, Fable-class) therefore runs adaptive thinking on
#: this call too, sharing ``max_tokens`` with the forced tool-call JSON
#: payload -- the identical truncation hazard lode-3dlt's Q&A fix exists to
#: avoid, on a path lode-3dlt named as a real but then-unreachable follow-up
#: (this ticket).
#:
#: **The two routes are bounded differently** -- neither by the Anthropic SDK's
#: non-streaming timeout guard, which never applies here for the reason
#: :data:`lode.qa.MAX_TOKENS` documents (it owns that claim; do not restate
#: it). On the IMMEDIATE route :func:`_call_haiku` passes
#: :attr:`~lode.config.Settings.enrich_call_timeout_s` (120s), so a runaway
#: thinking budget there tends to surface as a timeout before it exhausts this
#: cap. The BATCH route has no equivalent bound:
#: :class:`~lode.llm_provider.BatchRequest` carries no per-item timeout (the
#: ``timeout_s`` on ``submit_batch``/``collect_batch`` bounds only their own
#: HTTP calls) and generation runs server-side, so this cap is the *only*
#: thing bounding a batch item -- **truncation, not a timeout, is the
#: realistic failure mode there.**
#:
#: Either way this value is headroom, not a hard truncation guarantee.
#: Exhausting it raises :class:`~lode.llm_provider.LLMProviderError` from the
#: provider: on the immediate route in place of a raw ``StopIteration``
#: escaping the seam (the guard lode-jgus added), on the batch route as one
#: ``errored`` :class:`~lode.llm_provider.BatchResult` rather than failing the
#: whole collection. See :class:`~lode.llm_provider.AnthropicProvider`'s
#: docstring for both.
#:
#: **This is the fallback, not the last word (lode-d70n) -- closes the gap
#: named above**, most reachably on the BATCH route, which has no
#: escape-hatch bound besides this cap. Both routes resolve the active tier's
#: :attr:`~lode.llm_provider.ModelTier.max_tokens` over this constant through
#: :meth:`~lode.llm_provider.ModelTier.resolve_max_tokens`, so the
#: byte-for-byte equivalence above survives the override. See
#: ``docs/configuration.md`` "Models" for the decision.
MAX_TOKENS = 2048

_SYSTEM = (
    "You are a knowledge-extraction assistant. Extract structured information from "
    "personal notes concisely and accurately."
)

_PROMPT_TMPL = (
    "Extract structured enrichment from the note below.\n\n"
    "- tags: 3-8 short lowercase topic tags (e.g. 'python', 'auth', 'api-design').\n"
    "- entities: Named people, organizations, technologies, or tools mentioned.\n"
    "- inferred_edges: Topics or concepts this note relates to that are not explicitly "
    "stated. Each suggestion needs a to_id (concept/topic label), a reason (why you "
    "inferred this link), and a confidence score (0.0-1.0). "
    "These are suggestions only -- not asserted facts.\n"
    "- summary: One concise plain-English sentence summarizing the note's main point. "
    "Put the single most important fact or conclusion FIRST, so a truncated first "
    "clause still conveys the main point. Empty string if the note has no meaningful "
    "content.\n\n"
    "Note body:\n---\n{body}\n---"
)


# ---------------------------------------------------------------------------
# Structured-output models
# ---------------------------------------------------------------------------


class InferredEdge(BaseModel):
    """An AI-suggested relationship from this note to a concept or topic.

    Stored as a ``source='ai'`` edge in the ``edges`` table -- a suggestion with
    confidence, never an asserted fact. ``to_id`` is a concept/topic label; it need
    not be an existing note ID. The graph-expansion step (lode-72m.5) resolves
    suggestions to actual notes; this layer only captures what Haiku inferred.
    """

    to_id: str = Field(
        description="Concept, topic, or note reference this note relates to."
    )
    reason: str = Field(
        description="Why this relationship was inferred from the note content."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in this suggestion, between 0.0 (low) and 1.0 (high).",
    )


class EnrichmentResult(BaseModel):
    """Haiku's structured extraction from a single note version.

    The three list fields default to empty lists, and ``summary`` defaults to
    the empty string, so a note with no extractable content still produces a
    valid, storable result. Validated by Pydantic before persistence.
    """

    tags: list[str] = Field(
        default_factory=list,
        description="Short lowercase topic tags for this note.",
    )
    entities: list[str] = Field(
        default_factory=list,
        description="Named entities: people, organizations, technologies, tools.",
    )
    inferred_edges: list[InferredEdge] = Field(
        default_factory=list,
        description="Suggested related concepts or topics with reason and confidence.",
    )
    summary: str = Field(
        default="",
        description=(
            "One concise plain-English sentence summarizing the note's main "
            "point, with the single most important fact/conclusion first so a "
            "truncated first clause still conveys the main point. Empty string "
            "if the note has no meaningful content."
        ),
    )


# ---------------------------------------------------------------------------
# Haiku call
# ---------------------------------------------------------------------------


#: Tool description sent to the provider's forced tool-use call -- pinned here
#: (rather than inline) so :func:`_call_haiku` and :func:`_build_batch_request`
#: send the byte-for-byte identical string (lode-568v.2).
_TOOL_DESCRIPTION = "Extract structured enrichment from a note body."


def _call_haiku(
    body: str,
    settings: Settings,
    provider: LLMProvider,
) -> EnrichmentResult:
    """Call Haiku with structured output via forced tool-use; return a validated result.

    Routed through the :class:`~lode.llm_provider.LLMProvider` seam
    (lode-568v.2) -- ``provider.structured_call`` forces a single tool call
    (``tool_name`` given) so the response is always a structured extraction
    parseable by Pydantic, byte-for-byte identical to the direct SDK call this
    replaced. Raises on any API error -- the worker's retry/backoff layer
    handles transient failures.
    """
    prompt = _PROMPT_TMPL.format(body=body)
    tier = settings.enrichment_llm
    # Bounded client-side (lode-olmi.15): this immediate Haiku call is reachable
    # from `lode work`'s drain loop (a residual `enrich` job claimed by the main
    # claim/run loop, not the batch route -- see lode.worker.drain) as well as
    # from the capture path, and with no timeout it can otherwise hang the drain
    # indefinitely -- the same unbounded-hang the Batches API calls below are
    # bounded against, via the same knob.
    return provider.structured_call(
        model=tier.model,
        reasoning_effort=tier.reasoning_effort,
        system=_SYSTEM,
        user_prompt=prompt,
        output_schema=EnrichmentResult,
        max_tokens=tier.resolve_max_tokens(MAX_TOKENS),
        timeout_s=settings.enrich_call_timeout_s,
        tool_name=_TOOL_NAME,
        tool_description=_TOOL_DESCRIPTION,
    )


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------


def _write_enrichment(
    conn: sqlite3.Connection,
    owner_id: str,
    version_id: str,
    result: EnrichmentResult,
    model: str,
    ts: str,
    provider: str | None = None,
) -> None:
    """Write tags, entities, the whole-item summary, and inferred edges to the DB.

    ``owner_id`` is the polymorphic id the derived rows anchor to -- a
    ``note_id`` when ``version_id`` is a note version, or an ``external_id``
    when it is a snapshot (:func:`_resolve_enrich_target`, lode-7qi); the SQL
    below never distinguishes the two, since ``annotations.target`` /
    ``edges.from_id`` accept either by design (``src/lode/schema.sql``).

    ``provider`` is the LLM vendor identity to record alongside ``model``
    (lode-568v.4, design pinned lode-568v.1) -- ``None`` means "anthropic" by
    convention; see :func:`lode.llm_provider.provider_identity`, which both
    call sites use to compute it.

    Idempotent: deletes existing ``source='ai'`` annotations and edges keyed by
    ``source_version = version_id`` before inserting new rows. All writes commit
    in a single transaction.

    Tags + entities + summary go to ``annotations`` (``kind='tag'`` / ``'entity'``
    / ``'summary'``, ``target = owner_id``). Inferred edges go to ``edges``
    (``from_id = owner_id``, ``source='ai'``, ``status='fresh'``).

    The summary (lode-0wj.9) is a single whole-item row, written only when
    Haiku returned a non-empty ``result.summary`` -- an empty summary produces
    no row, mirroring how an empty tag/entity list produces no rows.

    User pinning (lode-npx.4): a suggestion whose ``(target, kind, payload)``
    (or ``(from_id, to_id)`` for edges) already has a ``source='user'`` row is
    skipped -- the user already decided about this exact item, whether that
    means they added it themselves or explicitly deleted an earlier AI
    suggestion (:mod:`lode.curation`). This applies identically to ``summary``.
    """
    with conn:
        # Clear existing AI-derived rows for this source_version (idempotency).
        conn.execute(
            "DELETE FROM annotations WHERE source = 'ai' AND source_version = ?",
            (version_id,),
        )
        conn.execute(
            "DELETE FROM edges WHERE source = 'ai' AND source_version = ?",
            (version_id,),
        )

        # Tag + entity + summary annotations -- whole-item rows carry no
        # per-item confidence and share one row shape; only `kind` and the
        # source list differ. The summary is a single whole-item value, so it
        # joins the loop as a 0-or-1-element list -- an empty summary yields no
        # row, exactly like an empty tag list (lode-0wj.9).
        for kind, values in (
            ("tag", result.tags),
            ("entity", result.entities),
            ("summary", [result.summary] if result.summary else []),
        ):
            for value in values:
                payload = json.dumps(value)
                if is_annotation_suppressed(conn, owner_id, kind, payload):
                    continue
                conn.execute(
                    "INSERT INTO annotations "
                    "(target, source_version, kind, payload, source, status, "
                    "model, provider, prompt_ver, created) "
                    "VALUES (?, ?, ?, ?, 'ai', 'fresh', ?, ?, ?, ?)",
                    (
                        owner_id,
                        version_id,
                        kind,
                        payload,
                        model,
                        provider,
                        ENRICH_PROMPT_VER,
                        ts,
                    ),
                )

        # Inferred edges -- AI suggestions with confidence; stored source='ai',
        # never as asserted facts.
        for edge in result.inferred_edges:
            if is_edge_suppressed(conn, owner_id, edge.to_id):
                continue
            conn.execute(
                "INSERT INTO edges "
                "(from_id, to_id, source, reason, confidence, source_version, status) "
                "VALUES (?, ?, 'ai', ?, ?, ?, 'fresh')",
                (owner_id, edge.to_id, edge.reason, edge.confidence, version_id),
            )


# ---------------------------------------------------------------------------
# CLI outcome formatting (lode-1gr.4)
# ---------------------------------------------------------------------------


def format_enrich_outcome(version_id: str, result: EnrichmentResult) -> str:
    """Render a one-line human-readable enrich outcome for ``lode work``'s echo.

    Shared by both enrich routes so they surface identical wording: the
    immediate path (:func:`lode.worker._enrich_handler`, wrapping
    :func:`enrich_version`) and the batch path (:func:`collect_enrich_batch`,
    on a later drain pass that collects a completed Batches API result).
    """
    return (
        f"enriched {short_version_id(version_id)}: {len(result.tags)} tags, "
        f"{len(result.entities)} entities, {len(result.inferred_edges)} edges, "
        f"summary {'set' if result.summary else 'empty'}"
    )


# ---------------------------------------------------------------------------
# Polymorphic target resolution (lode-7qi)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EnrichTarget:
    """A resolved enrichable target -- a note version or an external snapshot.

    ``owner_id`` is the polymorphic id enrichment rows write against
    (``note_id`` for a version, ``external_id`` for a snapshot -- whatever
    :func:`_write_enrichment`'s ``owner_id`` parameter expects). ``live`` is
    ``False`` for a soft-deleted/purged version or a tombstoned snapshot --
    callers skip enrichment for those without treating them as "not found".
    """

    owner_id: str
    body: str
    no_egress: bool
    live: bool


def _resolve_enrich_target(
    conn: sqlite3.Connection, version_id: str
) -> _EnrichTarget | None:
    """Resolve ``version_id`` to its owner, body, ``no_egress``, and liveness.

    ``version_id`` is polymorphic -- a note ``version_id`` or an external
    ``snapshot_id`` -- with no flag riding along to say which (an enqueued
    ``enrich`` job carries only ``target_version``, mirroring
    :func:`lode.embedding._version_body`'s blind versions-then-snapshots
    resolution). Tries ``versions`` first, then falls back to ``snapshots``.

    Returns ``None`` when ``version_id`` is unknown to both tables. Otherwise
    returns an :class:`_EnrichTarget` with ``live=False`` for a note's
    soft-delete tombstone (``op='delete'``) or hard-purge (``purged_at``
    set), or for a snapshot's link-rot tombstone (``status='tombstone'``,
    ``src/lode/schema.sql``) -- the three "this exists but there's nothing to
    enrich" cases callers gate on before ever calling Haiku.
    """
    row = conn.execute(
        """
        SELECT v.body, v.op, v.purged_at, n.note_id, n.no_egress
        FROM versions v
        JOIN notes n ON n.note_id = v.note_id
        WHERE v.version_id = ?
        """,
        (version_id,),
    ).fetchone()
    if row is not None:
        body, op, purged_at, note_id, no_egress = row
        live = op != "delete" and purged_at is None
        return _EnrichTarget(note_id, body, bool(no_egress), live)

    row = conn.execute(
        """
        SELECT s.body, s.status, e.external_id, e.no_egress
        FROM snapshots s
        JOIN externals e ON e.external_id = s.external_id
        WHERE s.snapshot_id = ?
        """,
        (version_id,),
    ).fetchone()
    if row is not None:
        body, status, external_id, no_egress = row
        return _EnrichTarget(external_id, body, bool(no_egress), status != "tombstone")

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enrich_version(
    conn: sqlite3.Connection,
    version_id: str,
    settings: Settings,
    *,
    provider: LLMProvider | None = None,
) -> EnrichmentResult | None:
    """Enrich a single note version or external snapshot with Haiku extraction.

    Loads the target's body (:func:`_resolve_enrich_target`, polymorphic --
    a note ``version_id`` or an external ``snapshot_id``, lode-7qi), gates on
    ``no_egress`` / tombstone / purged, redacts secrets before egress, calls
    Haiku with structured outputs, writes results to the DB (against the
    resolved owner -- ``note_id`` or ``external_id``), and audits the egress.

    Returns the :class:`EnrichmentResult` on success, or ``None`` when enrichment
    is skipped (target not found, ``no_egress``, soft-delete/purge, or a
    snapshot tombstone).

    :param conn: Open SQLite connection.
    :param version_id: The version or snapshot to enrich.
    :param settings: Resolved settings (enrichment model, redaction patterns, ...).
    :param provider: Optional :class:`~lode.llm_provider.LLMProvider`
        (credential-resolved via :func:`~lode.llm_provider.build_provider` if
        omitted). Inject in tests to avoid a live API call.
    """
    target = _resolve_enrich_target(conn, version_id)

    if target is None:
        log.warning(
            "enrich_version: version %s not found", short_version_id(version_id)
        )
        return None

    if not target.live:
        log.debug(
            "enrich_version: skip tombstone/purged version=%s",
            short_version_id(version_id),
        )
        return None

    if target.no_egress:
        log.debug(
            "enrich_version: skip no_egress owner_id=%s version=%s",
            target.owner_id,
            short_version_id(version_id),
        )
        return None

    # Redact secrets before sending to Haiku.
    redacted_body, redaction_count = redact_before_egress_counting(
        target.body, settings
    )

    if provider is None:
        provider = build_provider(settings)

    result = _call_haiku(redacted_body, settings, provider)

    # Format via jobs.iso (the one definition of the schema's ISO-8601 ms-Z
    # shape), but stamp from the RAW wall clock, not jobs.now(): this is an
    # enrichment record's timestamp, not a jobs-queue eligibility timestamp.
    # jobs.now() deliberately runs *ahead* of true time after absorbing a
    # backward step — correct for "is this job due yet", wrong for "when was
    # this enrichment written".
    ts = jobs.iso(datetime.now(UTC))
    # Provenance (lode-568v.4): the vendor identity alongside the model string
    # -- named `provider_name` to avoid shadowing the LLMProvider instance
    # bound to `provider` above.
    provider_name = provider_identity(settings)
    _write_enrichment(
        conn,
        target.owner_id,
        version_id,
        result,
        settings.enrichment_llm.model,
        ts,
        provider_name,
    )

    # Audit the egress -- one row per enrichment call.
    redactions = {version_id: redaction_count} if redaction_count else None
    log_egress(
        conn,
        "enrich",
        settings.enrichment_llm.model,
        [version_id],
        redactions,
        provider=provider_name,
    )

    log.info(
        "enrich_version: version=%s tags=%d entities=%d edges=%d summary=%s",
        short_version_id(version_id),
        len(result.tags),
        len(result.entities),
        len(result.inferred_edges),
        bool(result.summary),
    )

    return result


# ---------------------------------------------------------------------------
# Batch API helpers (lode-npx.2)
# ---------------------------------------------------------------------------


def _build_batch_request(
    version_id: str,
    body: str,
    settings: Settings,
) -> BatchRequest:
    """Build one :class:`~lode.llm_provider.BatchRequest` for ``version_id`` using ``body``.

    The ``custom_id`` is set to ``version_id`` so results can be mapped back to
    the originating job row without a secondary lookup. Mirrors what
    :func:`_call_haiku` passes to ``provider.structured_call`` — same tool,
    same prompt template, same extraction schema — so the provider's batch
    submission is byte-for-byte identical to the immediate call's wire shape.
    """
    prompt = _PROMPT_TMPL.format(body=body)
    tier = settings.enrichment_llm
    return BatchRequest(
        custom_id=version_id,
        model=tier.model,
        reasoning_effort=tier.reasoning_effort,
        system=_SYSTEM,
        user_prompt=prompt,
        output_schema=EnrichmentResult,
        max_tokens=tier.resolve_max_tokens(MAX_TOKENS),
        tool_name=_TOOL_NAME,
        tool_description=_TOOL_DESCRIPTION,
    )


def submit_enrich_batch(
    conn: sqlite3.Connection,
    job_rows: list[tuple[int, str]],
    settings: Settings,
    *,
    provider: LLMProvider | None = None,
) -> str | None:
    """Submit a batch of enrich jobs via the :class:`~lode.llm_provider.LLMProvider` seam.

    ``job_rows`` is a list of ``(job_id, target_version)`` drawn from the
    ``jobs`` table (status ``pending`` or ``running``).

    Behaviour:

    - Each target is resolved polymorphically (:func:`_resolve_enrich_target`,
      lode-7qi — a note ``version_id`` or an external ``snapshot_id``) and
      gated: not-found, ``no_egress``, tombstone (``op='delete'`` or a
      snapshot's ``status='tombstone'``), and purged (``purged_at IS NOT
      NULL``) targets are marked ``done`` immediately without an API call —
      exactly the same skip logic as :func:`enrich_version`.
    - Valid targets are redacted (:func:`lode.redact.redact_before_egress_counting`)
      then included as :class:`~lode.llm_provider.BatchRequest` objects
      (``custom_id = version_id``).
    - The batch is submitted via ``provider.submit_batch`` (the Anthropic
      Batches API, 50% off, for :class:`~lode.llm_provider.AnthropicProvider`).
    - Each submitted job row is updated to ``status='running'`` with
      ``batch_handle`` set to the provider's returned handle so it survives a
      restart (lode-i05.5).
    - A single ``egress_log`` row is written for all submitted version IDs.

    Returns the batch handle on success, or ``None`` when ``job_rows`` is empty
    or every version was gated out (all skipped, nothing submitted).

    Raises on provider errors — the caller is responsible for handling
    failures and reverting job rows to ``failed`` / ``pending`` as appropriate.

    :param conn: Open SQLite connection.
    :param job_rows: ``(job_id, target_version)`` pairs to submit.
    :param settings: Resolved settings (model, redaction patterns, …).
    :param provider: Optional :class:`~lode.llm_provider.LLMProvider`;
        credential-resolved via :func:`~lode.llm_provider.build_provider` if
        omitted.
    """
    if not job_rows:
        return None

    if provider is None:
        provider = build_provider(settings)

    # Gate each version; build batch requests only for valid ones.
    requests: list[BatchRequest] = []
    skip_ids: list[int] = []  # job_ids whose versions are skipped (gate out)
    submitted_job_ids: list[int] = []  # job_ids included in the batch
    submitted_version_ids: list[str] = []
    redactions: dict[str, int] = {}

    for job_id, version_id in job_rows:
        target = _resolve_enrich_target(conn, version_id)

        if target is None:
            log.warning(
                "submit_enrich_batch: version %s not found — marking done",
                short_version_id(version_id),
            )
            skip_ids.append(job_id)
            continue

        if not target.live or target.no_egress:
            log.debug(
                "submit_enrich_batch: skip version=%s (live=%s no_egress=%s)",
                short_version_id(version_id),
                target.live,
                target.no_egress,
            )
            skip_ids.append(job_id)
            continue

        redacted_body, redaction_count = redact_before_egress_counting(
            target.body, settings
        )
        if redaction_count:
            redactions[version_id] = redaction_count

        requests.append(_build_batch_request(version_id, redacted_body, settings))
        submitted_job_ids.append(job_id)
        submitted_version_ids.append(version_id)

    # Mark gated-out jobs done immediately (same outcome as enrich_version skip).
    if skip_ids:
        with conn:
            conn.executemany(
                "UPDATE jobs SET status = 'done' WHERE id = ?",
                [(jid,) for jid in skip_ids],
            )

    if not requests:
        return None

    # Submit the batch — this is the network call that commits the spend.
    # Bounded client-side (lode-olmi.15): with no timeout this can otherwise
    # hang indefinitely with no signal to the caller.
    batch_id = provider.submit_batch(requests, timeout_s=settings.enrich_call_timeout_s)

    # Persist the handle + flip to running so the collect step (and a restart)
    # can find these jobs (lode-i05.5).
    with conn:
        conn.executemany(
            "UPDATE jobs SET status = 'running', batch_handle = ? WHERE id = ?",
            [(batch_id, jid) for jid in submitted_job_ids],
        )

    # Audit: one egress_log row per batch submission. `provider_name` (not
    # `provider`) to avoid shadowing the LLMProvider instance bound above.
    log_egress(
        conn,
        "enrich",
        settings.enrichment_llm.model,
        submitted_version_ids,
        redactions or None,
        provider=provider_identity(settings),
    )

    log.info(
        "submit_enrich_batch: batch=%s submitted %d version(s)",
        batch_id,
        len(submitted_version_ids),
    )
    return batch_id


def collect_enrich_batch(
    conn: sqlite3.Connection,
    batch_id: str,
    settings: Settings,
    *,
    provider: LLMProvider | None = None,
    outcomes: list[str] | None = None,
) -> bool:
    """Poll a submitted batch and process results if it has ended.

    Polls via ``provider.collect_batch`` (``client.beta.messages.batches.retrieve``
    for :class:`~lode.llm_provider.AnthropicProvider`). Once ended, iterates
    results:

    - **succeeded**: validates the tool-use block, writes enrichment to DB
      via :func:`_write_enrichment`, marks the job ``done`` and stamps its
      ``prompt_ver`` to the current :data:`ENRICH_PROMPT_VER` (lode-q47) — the
      Batches API is the primary production route for enrich jobs, so this
      mirrors the same stamp :func:`lode.worker.run_one` applies on the
      immediate path; :mod:`lode.reconcile`'s enrich-gap step reads a
      ``done`` job's own ``prompt_ver`` to decide whether it is current. When
      ``outcomes`` is given, a formatted :func:`format_enrich_outcome` line is
      appended for each succeeded result (lode-1gr.4) — this is what lets
      ``lode work`` print a per-note outcome for a drain pass that *collects*
      a completed batch (the batch pre-step runs outside :func:`lode.worker.drain`'s
      main claim/run loop, so its outcomes aren't otherwise observable).
    - **errored / expired / canceled**: marks the job ``failed`` with backoff
      (using :data:`~lode.config.Settings.retry_backoff_base_s`); at
      :data:`~lode.config.Settings.retry_max_attempts` the job is
      dead-lettered. No outcome line is appended for these — the existing
      ``log.warning`` covers them.

    Only jobs with ``type='enrich'``, ``status='running'``,
    ``batch_handle=batch_id`` are touched — in-flight jobs from other batches
    are left alone.

    Returns ``True`` when the batch has ended (results processed), ``False``
    when the batch is still in progress (caller should retry later).

    :param conn: Open SQLite connection.
    :param batch_id: The batch handle recorded by :func:`submit_enrich_batch`.
    :param settings: Resolved settings (model, retry knobs, …).
    :param provider: Optional :class:`~lode.llm_provider.LLMProvider`;
        credential-resolved via :func:`~lode.llm_provider.build_provider` if
        omitted.
    """
    if provider is None:
        provider = build_provider(settings)

    # Bounded client-side (lode-olmi.15): with no timeout either call below
    # can otherwise hang indefinitely with no signal to the caller.
    status, batch_results = provider.collect_batch(
        batch_id, timeout_s=settings.enrich_call_timeout_s
    )
    if status == "pending":
        log.debug("collect_enrich_batch: batch=%s still pending", batch_id)
        return False

    # Map custom_id (version_id) → job_id for the in-flight set.
    rows = conn.execute(
        "SELECT id, target_version FROM jobs "
        "WHERE type = 'enrich' AND status = 'running' AND batch_handle = ?",
        (batch_id,),
    ).fetchall()
    job_map: dict[str, int] = {version_id: job_id for job_id, version_id in rows}

    if not job_map:
        log.debug(
            "collect_enrich_batch: batch=%s ended but no running jobs found",
            batch_id,
        )
        return True

    # Raw wall clock, formatted by jobs.iso — an enrichment record timestamp,
    # not a queue predicate (see enrich_version above).
    ts = jobs.iso(datetime.now(UTC))
    # Provenance (lode-568v.4): `provider_name`, not `provider`, to avoid
    # shadowing the LLMProvider instance bound above.
    provider_name = provider_identity(settings)

    for result in batch_results:
        version_id = result.custom_id
        job_id = job_map.get(version_id)
        if job_id is None:
            log.warning(
                "collect_enrich_batch: batch=%s result custom_id=%s has no running job",
                batch_id,
                short_version_id(version_id),
            )
            continue

        if result.outcome == "succeeded":
            try:
                # result.parsed is the provider's raw decoded payload (a
                # RootModel[dict], never a domain-specific validated model --
                # see lode.llm_provider's module docstring); the schema
                # validation stays this module's own job.
                enrichment = EnrichmentResult.model_validate(result.parsed.root)
            except Exception as exc:
                _mark_job_failed(conn, job_id, f"parse error: {exc}", settings)
                log.warning(
                    "collect_enrich_batch: batch=%s version=%s parse error: %s",
                    batch_id,
                    short_version_id(version_id),
                    exc,
                    exc_info=True,
                )
                continue

            # Polymorphic owner lookup (lode-7qi) — the same resolver
            # submit_enrich_batch gated on, but only the owner_id matters
            # here; liveness isn't re-checked (mirrors the pre-lode-7qi
            # existence-only check — a target purged/tombstoned between
            # submit and collect is rare enough that writing its last
            # enrichment result is harmless, and re-gating here would need
            # its own dead-code path with no test coverage to justify it).
            target = _resolve_enrich_target(conn, version_id)
            if target is None:
                log.warning(
                    "collect_enrich_batch: version %s disappeared after batch ended",
                    short_version_id(version_id),
                )
                with conn:
                    conn.execute(
                        "UPDATE jobs SET status = 'done' WHERE id = ?", (job_id,)
                    )
                continue

            _write_enrichment(
                conn,
                target.owner_id,
                version_id,
                enrichment,
                settings.enrichment_llm.model,
                ts,
                provider_name,
            )
            with conn:
                conn.execute(
                    "UPDATE jobs SET status = 'done', prompt_ver = ? WHERE id = ?",
                    (ENRICH_PROMPT_VER, job_id),
                )
            log.info(
                "collect_enrich_batch: batch=%s version=%s done "
                "(tags=%d entities=%d edges=%d)",
                batch_id,
                short_version_id(version_id),
                len(enrichment.tags),
                len(enrichment.entities),
                len(enrichment.inferred_edges),
            )
            if outcomes is not None:
                outcomes.append(format_enrich_outcome(version_id, enrichment))

        else:
            # errored, expired, canceled — treat as a transient failure.
            error_msg = (
                str(result.error)
                if result.error is not None
                else (f"batch result={result.outcome}")
            )
            _mark_job_failed(conn, job_id, error_msg, settings)
            log.warning(
                "collect_enrich_batch: batch=%s version=%s %s",
                batch_id,
                short_version_id(version_id),
                error_msg,
            )

    log.info(
        "collect_enrich_batch: batch=%s ended, processed %d job(s)",
        batch_id,
        len(job_map),
    )
    return True


def _mark_job_failed(
    conn: sqlite3.Connection,
    job_id: int,
    error_msg: str,
    settings: Settings,
) -> None:
    """Apply the retry/dead-letter state transition for a failed batch result.

    Delegates to :func:`lode.jobs.record_job_failure` — the same shared
    transition :func:`lode.worker.run_one` uses for a transient handler
    failure (increments ``attempts``, applies exponential backoff on
    ``next_attempt_at`` via the same wall-clock-drift-safe clock the worker's
    claim predicate reads, and dead-letters at ``retry_max_attempts``). No
    dead-letter hook is invoked here — ``embed``/``enrich`` register none
    (``lode.worker`` module docstring).

    ``record_job_failure``'s ``claim_lost`` (lode-3jte, and the ``claimed_at``
    identity it now also guards on — lode-nggm) is ignored here rather than
    acted on: this caller only ever runs against batch-submitted enrich jobs,
    and :func:`lode.worker._reclaim_stale_running`'s SELECT excludes any row
    with ``batch_handle`` set (module docstring) — the codebase's single
    writer of ``batch_handle`` only ever sets it, never clears it, so such a
    row is excluded from that race for the rest of its life. ``claimed_at`` is
    still read and passed through below (the function's signature requires
    it), it just can never be the thing that decides ``claim_lost`` here.
    """
    row = conn.execute(
        "SELECT attempts, claimed_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    current_attempts, claimed_at = row if row else (0, None)

    new_attempts, dead, _claim_lost = jobs.record_job_failure(
        conn, job_id, current_attempts, claimed_at, error_msg, settings
    )
    if dead:
        log.error(
            "_mark_job_failed: job %d dead-lettered after %d attempt(s)",
            job_id,
            new_attempts,
        )
