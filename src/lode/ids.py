"""Shared id-abbreviation helpers (lode-0bs).

:mod:`lode.notes_read` already has a shared short-id helper for NOTE ids
(``short_note_id`` / ``SHORT_NOTE_ID_LENGTH``, lode-1gr.2). VERSION ids never
got the same treatment: the 12-char abbreviation used for log lines and
``lode work``'s per-job outcome echo (lode-1gr.4) was a bare ``[:12]`` slice
literal duplicated across :mod:`lode.worker`, :mod:`lode.enrich`,
:mod:`lode.staleness`, and :mod:`lode.cli` (``cli._short``).

This module lives outside both :mod:`lode.notes_read` (note-centric) and
:mod:`lode.cli` (``worker.py``/``enrich.py`` must be importable without a cli
dependency — cli depends on them, not vice-versa) so every version-id call
site can share one helper without a cyclic or backwards import.

12 chars is correct here — it matches the pre-existing ``cli._short``
abbreviation for version-id digests and is deliberately longer than the
8-char ``SHORT_NOTE_ID_LENGTH`` (note ids and version ids are abbreviated to
different lengths on purpose; this is not a mismatch to reconcile).
"""

from __future__ import annotations

#: THE short version-id length across the codebase -- ``cli.status._short``
#: (log lines, ``lode work``'s per-job outcome echo) delegates to this
#: constant rather than each keeping its own value. Distinct from
#: ``lode.notes_read.SHORT_NOTE_ID_LENGTH`` (8), which abbreviates NOTE ids,
#: not version ids.
SHORT_VERSION_ID_LENGTH = 12


def short_version_id(version_id: str) -> str:
    """Abbreviate a version-id digest to its shared 12-char prefix.

    The one reusable version-id short helper (lode-0bs): log lines in
    :mod:`lode.worker` / :mod:`lode.enrich` / :mod:`lode.staleness` and
    ``lode work``'s per-job outcome echo (``worker._embed_handler``,
    ``enrich.format_enrich_outcome``) all call this rather than each growing
    its own ``[:12]`` slice. ``cli._short`` delegates to this too, adding its
    own ``…`` suffix when the full id is longer than the abbreviation.
    """
    return version_id[:SHORT_VERSION_ID_LENGTH]
