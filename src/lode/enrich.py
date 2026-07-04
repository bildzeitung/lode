"""Haiku structured-output enrichment + provenance (lode-npx.1 / lode-npx.2).

Extracts tags, entities, and inferred note-to-concept edges from a note version
via Claude Haiku structured outputs (tool-use) + Pydantic validation. Records
full provenance on every result. Inferred edges are AI suggestions with confidence
scores, stored as ``source='ai'`` -- never asserted facts.

Architecture (docs/storage.md):

- Tags + entities + a one-line summary → ``annotations`` table, ``source='ai'``,
  ``kind='tag'`` / ``'entity'`` / ``'summary'``, whole-note scope
  (``target = note_id``). ``summary`` (lode-0wj.9) is a single Haiku-derived
  sentence describing the head version -- same row shape, same provenance,
  same idempotent replace as tag/entity.
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

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import anthropic
from pydantic import BaseModel, Field

from lode.config import Settings
from lode.curation import is_annotation_suppressed, is_edge_suppressed
from lode.egress import log_egress
from lode.redact import redact_before_egress_counting

log = logging.getLogger(__name__)

#: Prompt version -- baked into every enrichment provenance row so a model or
#: prompt change can trigger corpus-wide re-enrichment via the reconcile scan.
#: Bumped to v2 for the ``summary`` field (lode-0wj.9).
ENRICH_PROMPT_VER = "npx1-v2"

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
    "These are suggestions only -- not asserted facts.\n"
    "- summary: One concise plain-English sentence summarizing the note's main point. "
    "Empty string if the note has no meaningful content.\n\n"
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
            "point. Empty string if the note has no meaningful content."
        ),
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
    """Write tags, entities, the whole-note summary, and inferred edges to the DB.

    Idempotent: deletes existing ``source='ai'`` annotations and edges keyed by
    ``source_version = version_id`` before inserting new rows. All writes commit
    in a single transaction.

    Tags + entities + summary go to ``annotations`` (``kind='tag'`` / ``'entity'``
    / ``'summary'``, ``target = note_id``). Inferred edges go to ``edges``
    (``from_id = note_id``, ``source='ai'``, ``status='fresh'``).

    The summary (lode-0wj.9) is a single whole-note row, written only when
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

        # Tag + entity + summary annotations -- whole-note items carry no
        # per-item confidence and share one row shape; only `kind` and the
        # source list differ. The summary is a single whole-note value, so it
        # joins the loop as a 0-or-1-element list -- an empty summary yields no
        # row, exactly like an empty tag list (lode-0wj.9).
        for kind, values in (
            ("tag", result.tags),
            ("entity", result.entities),
            ("summary", [result.summary] if result.summary else []),
        ):
            for value in values:
                payload = json.dumps(value)
                if is_annotation_suppressed(conn, note_id, kind, payload):
                    continue
                conn.execute(
                    "INSERT INTO annotations "
                    "(target, source_version, kind, payload, source, status, "
                    "model, prompt_ver, created) "
                    "VALUES (?, ?, ?, ?, 'ai', 'fresh', ?, ?, ?)",
                    (
                        note_id,
                        version_id,
                        kind,
                        payload,
                        model,
                        ENRICH_PROMPT_VER,
                        ts,
                    ),
                )

        # Inferred edges -- AI suggestions with confidence; stored source='ai',
        # never as asserted facts.
        for edge in result.inferred_edges:
            if is_edge_suppressed(conn, note_id, edge.to_id):
                continue
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
        "enrich_version: version=%s tags=%d entities=%d edges=%d summary=%s",
        version_id[:12],
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
) -> dict:
    """Build one Batches API request dict for ``version_id`` using ``body``.

    The ``custom_id`` is set to ``version_id`` so results can be mapped back to
    the originating job row without a secondary lookup. ``params`` mirrors what
    :func:`_call_haiku` passes to ``messages.create`` — same tool, same prompt
    template, same extraction schema.
    """
    prompt = _PROMPT_TMPL.format(body=body)
    return {
        "custom_id": version_id,
        "params": {
            "model": settings.enrichment_llm,
            "max_tokens": 1024,
            "system": _SYSTEM,
            "tools": [
                {
                    "name": _TOOL_NAME,
                    "description": "Extract structured enrichment from a note body.",
                    "input_schema": EnrichmentResult.model_json_schema(),
                }
            ],
            "tool_choice": {"type": "tool", "name": _TOOL_NAME},
            "messages": [{"role": "user", "content": prompt}],
        },
    }


def submit_enrich_batch(
    conn: sqlite3.Connection,
    job_rows: list[tuple[int, str]],
    settings: Settings,
    *,
    client: anthropic.Anthropic | None = None,
) -> str | None:
    """Submit a batch of enrich jobs to the Anthropic Batches API (50% off).

    ``job_rows`` is a list of ``(job_id, target_version)`` drawn from the
    ``jobs`` table (status ``pending`` or ``running``).

    Behaviour:

    - Each version is gated: ``no_egress``, tombstone (``op='delete'``), and
      purged (``purged_at IS NOT NULL``) versions are marked ``done`` immediately
      without an API call — exactly the same skip logic as :func:`enrich_version`.
    - Valid versions are redacted (:func:`lode.redact.redact_before_egress_counting`)
      then included as Batches API request objects (``custom_id = version_id``).
    - The batch is submitted to ``client.beta.messages.batches.create``.
    - Each submitted job row is updated to ``status='running'`` with
      ``batch_handle = batch_id`` so the handle survives a restart (lode-i05.5).
    - A single ``egress_log`` row is written for all submitted version IDs.

    Returns the batch ID on success, or ``None`` when ``job_rows`` is empty or
    every version was gated out (all skipped, nothing submitted).

    Raises on Batches API errors — the caller is responsible for handling
    failures and reverting job rows to ``failed`` / ``pending`` as appropriate.

    :param conn: Open SQLite connection.
    :param job_rows: ``(job_id, target_version)`` pairs to submit.
    :param settings: Resolved settings (model, redaction patterns, …).
    :param client: Optional Anthropic client; created fresh if omitted.
    """
    if not job_rows:
        return None

    if client is None:
        client = anthropic.Anthropic()

    # Gate each version; build batch requests only for valid ones.
    requests: list[dict] = []
    skip_ids: list[int] = []  # job_ids whose versions are skipped (gate out)
    submitted_job_ids: list[int] = []  # job_ids included in the batch
    submitted_version_ids: list[str] = []
    redactions: dict[str, int] = {}

    for job_id, version_id in job_rows:
        row = conn.execute(
            """
            SELECT v.body, v.op, v.purged_at, n.no_egress
            FROM versions v
            JOIN notes n ON n.note_id = v.note_id
            WHERE v.version_id = ?
            """,
            (version_id,),
        ).fetchone()

        if row is None:
            log.warning(
                "submit_enrich_batch: version %s not found — marking done",
                version_id[:12],
            )
            skip_ids.append(job_id)
            continue

        body, op, purged_at, no_egress = row

        if op == "delete" or purged_at is not None or no_egress:
            log.debug(
                "submit_enrich_batch: skip version=%s (op=%s purged=%s no_egress=%s)",
                version_id[:12],
                op,
                purged_at is not None,
                bool(no_egress),
            )
            skip_ids.append(job_id)
            continue

        redacted_body, redaction_count = redact_before_egress_counting(body, settings)
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
    batch = client.beta.messages.batches.create(requests=requests)
    batch_id = batch.id

    # Persist the handle + flip to running so the collect step (and a restart)
    # can find these jobs (lode-i05.5).
    with conn:
        conn.executemany(
            "UPDATE jobs SET status = 'running', batch_handle = ? WHERE id = ?",
            [(batch_id, jid) for jid in submitted_job_ids],
        )

    # Audit: one egress_log row per batch submission.
    log_egress(
        conn,
        "enrich",
        settings.enrichment_llm,
        submitted_version_ids,
        redactions or None,
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
    client: anthropic.Anthropic | None = None,
) -> bool:
    """Poll a submitted batch and process results if it has ended.

    Retrieves the batch status via ``client.beta.messages.batches.retrieve``.
    If ``processing_status == 'ended'``, iterates results:

    - **succeeded**: validates the tool-use block, writes enrichment to DB
      via :func:`_write_enrichment`, marks the job ``done`` and stamps its
      ``prompt_ver`` to the current :data:`ENRICH_PROMPT_VER` (lode-q47) — the
      Batches API is the primary production route for enrich jobs, so this
      mirrors the same stamp :func:`lode.worker.run_one` applies on the
      immediate path; :mod:`lode.reconcile`'s enrich-gap step reads a
      ``done`` job's own ``prompt_ver`` to decide whether it is current.
    - **errored / expired / canceled**: marks the job ``failed`` with backoff
      (using :data:`~lode.config.Settings.retry_backoff_base_s`); at
      :data:`~lode.config.Settings.retry_max_attempts` the job is
      dead-lettered.

    Only jobs with ``type='enrich'``, ``status='running'``,
    ``batch_handle=batch_id`` are touched — in-flight jobs from other batches
    are left alone.

    Returns ``True`` when the batch has ended (results processed), ``False``
    when the batch is still in progress (caller should retry later).

    :param conn: Open SQLite connection.
    :param batch_id: The Batches API handle recorded by :func:`submit_enrich_batch`.
    :param settings: Resolved settings (model, retry knobs, …).
    :param client: Optional Anthropic client; created fresh if omitted.
    """
    if client is None:
        client = anthropic.Anthropic()

    batch = client.beta.messages.batches.retrieve(batch_id)
    if batch.processing_status != "ended":
        log.debug(
            "collect_enrich_batch: batch=%s still %s",
            batch_id,
            batch.processing_status,
        )
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

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    for result in client.beta.messages.batches.results(batch_id):
        version_id = result.custom_id
        job_id = job_map.get(version_id)
        if job_id is None:
            log.warning(
                "collect_enrich_batch: batch=%s result custom_id=%s has no running job",
                batch_id,
                version_id[:12] if len(version_id) >= 12 else version_id,
            )
            continue

        if result.result.type == "succeeded":
            try:
                tool_block = next(
                    b for b in result.result.message.content if b.type == "tool_use"
                )
                enrichment = EnrichmentResult.model_validate(tool_block.input)
            except Exception as exc:
                _mark_job_failed(conn, job_id, f"parse error: {exc}", settings)
                log.warning(
                    "collect_enrich_batch: batch=%s version=%s parse error: %s",
                    batch_id,
                    version_id[:12],
                    exc,
                )
                continue

            note_row = conn.execute(
                "SELECT note_id FROM versions WHERE version_id = ?", (version_id,)
            ).fetchone()
            if note_row is None:
                log.warning(
                    "collect_enrich_batch: version %s disappeared after batch ended",
                    version_id[:12],
                )
                with conn:
                    conn.execute(
                        "UPDATE jobs SET status = 'done' WHERE id = ?", (job_id,)
                    )
                continue

            _write_enrichment(
                conn,
                note_row[0],
                version_id,
                enrichment,
                settings.enrichment_llm,
                ts,
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
                version_id[:12],
                len(enrichment.tags),
                len(enrichment.entities),
                len(enrichment.inferred_edges),
            )

        else:
            # errored, expired, canceled — treat as a transient failure.
            error_type = result.result.type
            error_msg = (
                f"batch result={error_type}"
                if not hasattr(result.result, "error")
                else f"batch error: {result.result.error}"
            )
            _mark_job_failed(conn, job_id, error_msg, settings)
            log.warning(
                "collect_enrich_batch: batch=%s version=%s %s",
                batch_id,
                version_id[:12],
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

    Mirrors the logic in :func:`lode.worker.run_one` for transient failures:
    increments ``attempts``, applies exponential backoff on ``next_attempt_at``,
    and dead-letters at ``retry_max_attempts``.
    """
    row = conn.execute("SELECT attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
    current_attempts = row[0] if row else 0
    new_attempts = current_attempts + 1

    if new_attempts >= settings.retry_max_attempts:
        with conn:
            conn.execute(
                "UPDATE jobs SET status = 'dead', attempts = ?, last_error = ? "
                "WHERE id = ?",
                (new_attempts, error_msg, job_id),
            )
        log.error(
            "_mark_job_failed: job %d dead-lettered after %d attempt(s)",
            job_id,
            new_attempts,
        )
    else:
        delay = min(
            settings.retry_backoff_base_s * (2 ** (new_attempts - 1)),
            settings.retry_backoff_cap_s,
        )
        next_at = (datetime.now(UTC) + timedelta(seconds=delay)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        with conn:
            conn.execute(
                "UPDATE jobs SET status = 'failed', attempts = ?, "
                "last_error = ?, next_attempt_at = ? WHERE id = ?",
                (new_attempts, error_msg, next_at, job_id),
            )
