"""Read side for the async work queue and the egress log (lode-35nu.9).

Relocated out of ``lode.cli`` (the ticket's "no bare SQL in the cli
package") -- every query ``lode status``/``lode jobs``/``lode egress``/
``lode work --wait`` used to run inline lives here instead, unchanged; the
CLI commands themselves are dispatch-only callers of these functions.

Readers here return bare positional tuples; :func:`list_egress` alone returns a
NamedTuple (:class:`EgressRow`). That asymmetry is deliberate, not an unfinished
sweep (lode-kl1i): it is the only reader wide enough (8 columns, 3 of them
nullable) that its caller needed a multi-line destructure, and the only one
whose caller branches on one field to decide whether two others are meaningful.
Do not convert the 2-to-6-column readers for symmetry -- and do not copy their
bare-tuple shape into a new wide one.
"""

import sqlite3
from typing import NamedTuple

from lode.jobs import STANDING_REFUSAL_SQL, in_force_enrich_job_id_sql


def job_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """``{status: count}`` over every row in ``jobs`` -- ``lode status``'s table."""
    return dict(
        conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
    )


def dead_letter_jobs(
    conn: sqlite3.Connection,
) -> list[tuple[int, str, str, str | None]]:
    """Every ``dead``-status job's ``(id, type, target_version, last_error)``, oldest first."""
    return conn.execute(
        "SELECT id, type, target_version, last_error FROM jobs "
        "WHERE status = 'dead' ORDER BY id"
    ).fetchall()


def refusal_dead_jobs(
    conn: sqlite3.Connection, refusal_params: tuple[str, str | None, str | None] | None
) -> dict[int, str | None]:
    """``{job_id: refusal_category}`` for every refusal-dead enrich job that still stands.

    A dead row is permanent, so a refusal that reconcile has re-opened (or will
    on its next scan) stays in ``jobs`` for good. A refusal stands (lode-54c7)
    only while it is what keeps reconcile's enrich-gap step from re-opening the
    item:

    - :data:`lode.jobs.STANDING_REFUSAL_SQL` matches ``refusal_params`` (prompt
      version, enrichment model, provider -- the key reconcile binds); and
    - for a note, the row targets the note's current head and no later enrich
      job for that head is live or ``done`` (a refusal re-opened under another
      model matches the key again once the model is set back, but the later job
      has retried it);
    - for an external, the row is the external's in-force enrich job
      (:func:`lode.jobs.in_force_enrich_job_id_sql`, the one reconcile judges).

    ``refusal_params=None`` (no resolvable settings) skips only the key
    comparison; the head tests need no settings and still apply.

    The category is None when the provider supplied none.
    """
    standing = "j.refusal = 1" if refusal_params is None else STANDING_REFUSAL_SQL
    # The s.external_id guard keeps a note-targeted row out of the in-force
    # subquery, which would otherwise scan all of jobs to find nothing.
    return dict(
        conn.execute(
            f"""
            SELECT j.id, j.refusal_category FROM jobs j
            LEFT JOIN snapshots s ON s.snapshot_id = j.target_version
            WHERE j.type = 'enrich' AND j.status = 'dead'
              AND {standing}
              AND (
                (
                  EXISTS (
                      SELECT 1 FROM notes n WHERE n.head_version_id = j.target_version
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs j2
                      WHERE j2.type = 'enrich'
                        AND j2.target_version = j.target_version
                        AND j2.id > j.id
                        AND j2.status != 'dead'
                  )
                )
                OR (
                  s.external_id IS NOT NULL
                  AND j.id = {in_force_enrich_job_id_sql("s.external_id")}
                )
              )
            """,
            refusal_params or (),
        ).fetchall()
    )


def egress_purpose_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """``(purpose, count)`` rows over ``egress_log``, ordered by purpose."""
    return conn.execute(
        "SELECT purpose, COUNT(*) FROM egress_log GROUP BY purpose ORDER BY purpose"
    ).fetchall()


def list_jobs(
    conn: sqlite3.Connection, status: str | None = None
) -> list[tuple[int, str, str, int, str, str | None]]:
    """Every job (or every job in ``status``), each as ``(id, type, status,
    attempts, target_version, last_error)`` -- ``lode jobs``'s read."""
    if status is None:
        return conn.execute(
            "SELECT id, type, status, attempts, target_version, last_error "
            "FROM jobs ORDER BY id"
        ).fetchall()
    return conn.execute(
        "SELECT id, type, status, attempts, target_version, last_error "
        "FROM jobs WHERE status = ? ORDER BY id",
        (status,),
    ).fetchall()


class EgressRow(NamedTuple):
    """One ``egress_log`` row -- ``list_egress``'s self-describing return type.

    ``destination``/``arguments`` are NULL for ``purpose`` in ('enrich', 'qa')
    and populated for ``purpose='tool'`` (lode-l87l; schema/writer since
    lode-35nu.11.7/.11.1).

    Field order is load-bearing: :func:`list_egress` builds these positionally
    from the row, so this declaration must stay in lockstep with that
    function's ``SELECT`` column order (which is why it is declared here,
    immediately above it, rather than at module top).
    """

    id: int
    ts: str
    purpose: str
    model: str | None
    sent_targets: str
    redactions: str | None
    destination: str | None
    arguments: str | None


def list_egress(
    conn: sqlite3.Connection, purpose: str | None = None
) -> list[EgressRow]:
    """Every egress send (or every send of ``purpose``), each as an
    :class:`EgressRow` -- ``lode egress``'s read."""
    where, params = ("", ()) if purpose is None else ("WHERE purpose = ? ", (purpose,))
    return [
        EgressRow(*row)
        for row in conn.execute(
            "SELECT id, ts, purpose, model, sent_targets, redactions, "
            f"destination, arguments FROM egress_log {where}ORDER BY id",
            params,
        )
    ]


def outstanding_jobs(conn: sqlite3.Connection) -> list[tuple[int, str, str, str]]:
    """List jobs still ``pending``/``running`` -- for ``lode work --wait``'s timeout report.

    Read fresh each poll tick so it reflects the latest drain pass, including
    batch-backed enrich jobs still ``running`` on an in-flight Batches API
    request (they are not a bug -- see ``lode.cli.work.work``'s ``--wait``
    docstring).

    Deliberately narrower than ``lode.jobs.LIVE_JOB_STATUSES`` (DECIDED
    lode-uri7): "outstanding" here means being worked right now or about to
    be, not merely retryable -- a job sitting ``'failed'`` in backoff is not
    what ``--wait``'s "still outstanding" report means by that word.
    """
    return conn.execute(
        "SELECT id, type, status, target_version FROM jobs "
        "WHERE status IN ('pending', 'running') ORDER BY id"
    ).fetchall()
