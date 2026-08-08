"""Read side for the async work queue and the egress log (lode-35nu.9).

Relocated out of ``lode.cli`` (the ticket's "no bare SQL in the cli
package") -- every query ``lode status``/``lode jobs``/``lode egress``/
``lode work --wait`` used to run inline lives here instead, unchanged; the
CLI commands themselves are dispatch-only callers of these functions.
"""

import sqlite3
from typing import NamedTuple


class EgressRow(NamedTuple):
    """One ``egress_log`` row -- ``list_egress``'s self-describing return type.

    ``destination``/``arguments`` are NULL for ``purpose`` in ('enrich', 'qa')
    and populated for ``purpose='tool'`` (lode-l87l; schema/writer since
    lode-35nu.11.7/.11.1).
    """

    id: int
    ts: str
    purpose: str
    model: str | None
    sent_targets: str
    redactions: str | None
    destination: str | None
    arguments: str | None


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


def list_egress(
    conn: sqlite3.Connection, purpose: str | None = None
) -> list[EgressRow]:
    """Every egress send (or every send of ``purpose``), each as an
    :class:`EgressRow` -- ``lode egress``'s read."""
    where, params = ("", ()) if purpose is None else ("WHERE purpose = ? ", (purpose,))
    rows = conn.execute(
        "SELECT id, ts, purpose, model, sent_targets, redactions, "
        f"destination, arguments FROM egress_log {where}ORDER BY id",
        params,
    ).fetchall()
    return [EgressRow(*row) for row in rows]


def outstanding_jobs(conn: sqlite3.Connection) -> list[tuple[int, str, str, str]]:
    """List jobs still ``pending``/``running`` -- for ``lode work --wait``'s timeout report.

    Read fresh each poll tick so it reflects the latest drain pass, including
    batch-backed enrich jobs still ``running`` on an in-flight Batches API
    request (they are not a bug -- see ``lode.cli.work.work``'s ``--wait``
    docstring).
    """
    return conn.execute(
        "SELECT id, type, status, target_version FROM jobs "
        "WHERE status IN ('pending', 'running') ORDER BY id"
    ).fetchall()
