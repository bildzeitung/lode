"""``lode stats`` -- read-only corpus-inspection CLI (lode-tyhy).

Deliberately NOT part of ``lode status`` (whose contract is "action
needed" -- see that command's own help). This is informational: a
periodic look at how large the corpus, and its tombstone population
specifically, actually are -- without hand-running SQL against
``$LODE_HOME/lode.db``. See ``docs/externals.md``/``lode-w0h.1`` for the
tombstone taxonomy, and ``lode-oni`` (deferred) for the eventual consumer
of the ``empty_extract`` breakdown this renders.

Read-only: every query in ``lode.stats_read`` is a bare ``SELECT``, no
writes, no queue interaction, no new config knobs. Rendering-only here,
same dispatch/rendering split ``lode.cli.status``/``lode.cli.egress``
already follow.
"""

from lode.cli import _DbOption, _open_db, _tabular_table, app, console
from lode.jobs_read import egress_purpose_counts
from lode.stats_read import (
    edges_by_source,
    edges_by_status,
    empty_extract_raw_payload_retained_count,
    externals_by_source_type,
    externals_no_egress_count,
    note_counts,
    passage_index_stats,
    snapshot_status_counts,
    tombstone_reason_counts,
    version_chain_stats,
)

#: The one tombstone reason ``lode-oni`` (deferred: headless-render
#: retrieval) would actually consume -- annotated inline wherever it appears
#: below rather than filtered out, so the count stays visible in context.
_ONI_CANDIDATE_REASON = "empty_extract"


def _counts_table(
    title: str,
    column: str,
    rows: list[tuple[str, int]],
    *,
    flag_label: str | None = None,
    flag_suffix: str = "",
) -> None:
    """Render a ``(label, count)`` list as a titled two-column table.

    ``flag_label``/``flag_suffix`` append a note to that one row's label cell
    (only the lode-oni candidate bucket uses it); every other row renders bare.
    """
    console.print(title, style="table.header")
    table = _tabular_table()
    table.add_column(column)
    table.add_column("Count", justify="right")
    if not rows:
        table.add_row("(none)", "0")
    for label, count in rows:
        suffix = flag_suffix if label == flag_label else ""
        table.add_row(f"{label}{suffix}", str(count))
    console.print(table)


@app.command(
    help=(
        "Show read-only corpus-inspection metrics.\n\n"
        "Point-in-time only -- run it periodically by hand to see how the "
        "corpus (and the empty_extract tombstone population specifically) "
        "is growing. Deliberately separate from 'lode status', whose "
        "contract stays action-needed only."
    )
)
def stats(db: _DbOption = None) -> None:
    """Show read-only corpus-inspection metrics.

    Renders, as Rich tables: snapshots by status (ok/tombstone); tombstones
    by reason, parsed from the stable ``[tombstone: <reason>]`` body tag
    (``lode.externals.tombstone_body``), with the ``empty_extract`` bucket
    flagged as the lode-oni (deferred headless-render retrieval) candidate
    set; how many of those empty_extract tombstones still retain a
    raw_payload (the upper bound on pages lode-oni could actually
    re-render); a corpus overview (notes total/live/deleted, version chain
    depth, externals by source_type, no_egress externals, passage +
    embedding counts with index coverage); edges by status and by source;
    and the egress log's total entry count.

    All read-only SELECTs, no queue interaction, no writes -- see
    docs/externals.md's tombstone taxonomy (lode-w0h.1) for what each
    reason bucket means.
    """
    conn = _open_db(db)
    try:
        snap_counts = snapshot_status_counts(conn)
        reason_counts = tombstone_reason_counts(conn)
        retained = empty_extract_raw_payload_retained_count(conn)
        notes = note_counts(conn)
        versions = version_chain_stats(conn)
        by_source_type = externals_by_source_type(conn)
        no_egress_externals = externals_no_egress_count(conn)
        passages = passage_index_stats(conn)
        edge_status = edges_by_status(conn)
        edge_source = edges_by_source(conn)
        # Same seam `lode status` totals egress through, so the two commands
        # can never report different totals for the same DB.
        egress_total = sum(n for _, n in egress_purpose_counts(conn))
    finally:
        conn.close()

    _counts_table("Snapshots by status", "Status", snap_counts)
    _counts_table(
        "Tombstones by reason",
        "Reason",
        reason_counts,
        flag_label=_ONI_CANDIDATE_REASON,
        flag_suffix=" (lode-oni candidate)",
    )
    console.print(
        f"empty_extract tombstones with raw_payload retained: {retained}",
        markup=False,
        highlight=False,
    )

    console.print("Corpus overview", style="table.header")
    overview = _tabular_table()
    overview.add_column("Metric")
    overview.add_column("Value", justify="right")
    overview.add_row("Notes (total)", str(notes.total))
    overview.add_row("Notes (live)", str(notes.live))
    overview.add_row("Notes (deleted)", str(notes.deleted))
    overview.add_row("Versions (total)", str(versions.total_versions))
    overview.add_row("Version chain depth (max)", str(versions.max_depth))
    overview.add_row("Version chain depth (avg)", f"{versions.avg_depth:.2f}")
    overview.add_row("Externals with no_egress set", str(no_egress_externals))
    overview.add_row("Passages (total)", str(passages.passages))
    overview.add_row("Embeddings (total)", str(passages.embeddings))
    overview.add_row(
        "Index coverage (targets with embeddings)",
        str(passages.targets_with_embeddings),
    )
    overview.add_row(
        "Index coverage (targets without embeddings)",
        str(passages.targets_without_embeddings),
    )
    overview.add_row("Egress log (total entries)", str(egress_total))
    console.print(overview)

    _counts_table("Externals by source_type", "Source type", by_source_type)
    _counts_table("Edges by status", "Status", edge_status)
    _counts_table("Edges by source", "Source", edge_source)
