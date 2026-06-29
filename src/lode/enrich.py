"""Haiku structured-output enrichment + provenance (lode-npx.1).

Extracts tags, entities, and inferred note-to-concept edges from a note version
via Claude Haiku structured outputs (tool-use) + Pydantic validation. Records
full provenance on every result. Inferred edges are AI suggestions with confidence
scores, stored as ``source='ai'`` -- never asserted facts.

Architecture (docs/storage.md):

- Tags + entities → ``annotations`` table, ``source='ai'``,
  ``kind='tag'`` / ``'entity'``, whole-note scope (``target = note_id``).
- Inferred edges → ``edges`` table, ``source='ai'``, with ``confidence``,
  ``reason``, and ``source_version``. These are Haiku suggestions; user curation
  (``source='user'``) is separate and irreplaceable (pinned to ``note_id``).
- Full provenance on every row: ``model``, ``prompt_ver`` (:data:`ENRICH_PROMPT_VER`),
  ``source_version`` (the version_id enriched), ``created`` (ISO-8601 UTC).
- Egress gate: ``no_egress`` notes are never sent to Haiku; ``redact_before_egress``
  strips secrets before the payload leaves the box. One ``egress_log`` row is written
  per enrichment call (``purpose='enrich'``).
- Idempotent: deletes existing ``source='ai'`` rows keyed to the enriched
  ``source_version`` before writing new results, so re-running on the same version
  converges cleanly.

The ``enrich`` job type is claimed by the worker (lode-i05.3 handler registry, wired
in :mod:`lode.worker`); the reconciliation scan (lode-i05.4 ``enrich_gap`` step, wired
in :mod:`lode.reconcile`) re-enqueues any head version missing a live enrich job.
"""

import json
import logging
import sqlite3
from datetime import UTC, datetime

import anthropic
from pydantic import BaseModel, Field

from lode.config import Settings
from lode.egress import log_egress
from lode.redact import redact_before_egress_counting

log = logging.getLogger(__name__)

#: Prompt version -- baked into every enrichment provenance row so a model or
#: prompt change can trigger corpus-wide re-enrichment via the reconcile scan.
ENRICH_PROMPT_VER = "npx1-v1"

#: Tool name used to force Haiku into structured output via tool-use calling.
_TOOL_NAME = "extract_enrichment"

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
    "These are suggestions only -- not asserted facts.\n\n"
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

    All three fields default to empty lists so a note with no extractable items
    produces a valid, storable result. Validated by Pydantic before persistence.
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


# ---------------------------------------------------------------------------
# Haiku call
# ---------------------------------------------------------------------------


def _call_haiku(
    body: str,
    settings: Settings,
    client: anthropic.Anthropic,
) -> EnrichmentResult:
    """Call Haiku with structured output via tool-use; return a validated result.

    Forces a single tool call (``tool_choice={"type": "tool", "name": ...}``) so
    the response is always a structured extraction parseable by Pydantic.
    Raises on any API error -- the worker's retry/backoff layer handles transient
    failures.
    """
    prompt = _PROMPT_TMPL.format(body=body)
    response = client.messages.create(
        model=settings.enrichment_llm,
        max_tokens=1024,
        system=_SYSTEM,
        tools=[
            {
                "name": _TOOL_NAME,
                "description": "Extract structured enrichment from a note body.",
                "input_schema": EnrichmentResult.model_json_schema(),
            }
        ],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
        messages=[{"role": "user", "content": prompt}],
    )
    tool_block = next(b for b in response.content if b.type == "tool_use")
    return EnrichmentResult.model_validate(tool_block.input)


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------


def _write_enrichment(
    conn: sqlite3.Connection,
    note_id: str,
    version_id: str,
    result: EnrichmentResult,
    model: str,
    ts: str,
) -> None:
    """Write tags, entities, and inferred edges to the DB.

    Idempotent: deletes existing ``source='ai'`` annotations and edges keyed by
    ``source_version = version_id`` before inserting new rows. All writes commit
    in a single transaction.

    Tags + entities go to ``annotations`` (``kind='tag'``/``'entity'``,
    ``target = note_id``). Inferred edges go to ``edges`` (``from_id = note_id``,
    ``source='ai'``, ``status='fresh'``).
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

        # Tag + entity annotations -- whole-note items carry no per-item
        # confidence. Both kinds share one row shape; only `kind` and the
        # source list differ.
        for kind, values in (("tag", result.tags), ("entity", result.entities)):
            for value in values:
                conn.execute(
                    "INSERT INTO annotations "
                    "(target, source_version, kind, payload, source, status, "
                    "model, prompt_ver, created) "
                    "VALUES (?, ?, ?, ?, 'ai', 'fresh', ?, ?, ?)",
                    (
                        note_id,
                        version_id,
                        kind,
                        json.dumps(value),
                        model,
                        ENRICH_PROMPT_VER,
                        ts,
                    ),
                )

        # Inferred edges -- AI suggestions with confidence; stored source='ai',
        # never as asserted facts.
        for edge in result.inferred_edges:
            conn.execute(
                "INSERT INTO edges "
                "(from_id, to_id, source, reason, confidence, source_version, status) "
                "VALUES (?, ?, 'ai', ?, ?, ?, 'fresh')",
                (note_id, edge.to_id, edge.reason, edge.confidence, version_id),
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enrich_version(
    conn: sqlite3.Connection,
    version_id: str,
    settings: Settings,
    *,
    client: anthropic.Anthropic | None = None,
) -> EnrichmentResult | None:
    """Enrich a single note version with Haiku structured extraction.

    Loads the version body, gates on ``no_egress`` / tombstone / purged, redacts
    secrets before egress, calls Haiku with structured outputs, writes results to
    the DB, and audits the egress.

    Returns the :class:`EnrichmentResult` on success, or ``None`` when enrichment
    is skipped (``no_egress`` note, soft-delete tombstone, or purged version).

    :param conn: Open SQLite connection.
    :param version_id: The version to enrich.
    :param settings: Resolved settings (enrichment model, redaction patterns, ...).
    :param client: Optional Anthropic client (created fresh if omitted). Inject in
        tests to avoid a live API call.
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

    if row is None:
        log.warning("enrich_version: version %s not found", version_id[:12])
        return None

    body, op, purged_at, note_id, no_egress = row

    if op == "delete":
        log.debug("enrich_version: skip tombstone version=%s", version_id[:12])
        return None

    if purged_at is not None:
        log.debug("enrich_version: skip purged version=%s", version_id[:12])
        return None

    if no_egress:
        log.debug("enrich_version: skip no_egress note_id=%s", note_id[:12])
        return None

    # Redact secrets before sending to Haiku.
    redacted_body, redaction_count = redact_before_egress_counting(body, settings)

    if client is None:
        client = anthropic.Anthropic()

    result = _call_haiku(redacted_body, settings, client)

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    _write_enrichment(conn, note_id, version_id, result, settings.enrichment_llm, ts)

    # Audit the egress -- one row per enrichment call.
    redactions = {version_id: redaction_count} if redaction_count else None
    log_egress(conn, "enrich", settings.enrichment_llm, [version_id], redactions)

    log.info(
        "enrich_version: version=%s tags=%d entities=%d edges=%d",
        version_id[:12],
        len(result.tags),
        len(result.entities),
        len(result.inferred_edges),
    )

    return result
