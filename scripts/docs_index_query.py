"""Typer CLI over the docs/ lookup index -- ranked pointers, never answers
(``lode-t6o1.3``).

Builds the index (``scripts/docs_index_build.py``, ``lode-t6o1.2``) fresh on
every invocation and returns RANKED POINTERS + SNIPPETS: ``path:line_lo-line_hi``,
the unit's first line, and a short snippet. It NEVER prints a whole unit and
NEVER synthesizes a prose answer -- this is a lookup tool, not a second Q&A
system (``docs/decisions.md``, entry ``lode-t6o1``). The caller reads the
exact range itself.

FTS5 INPUT ESCAPING is the load-bearing half of this ticket. Raw user input
must never reach ``MATCH`` unescaped -- measured against a real index during
the epic's ``/challenge``: a bd issue id (``lode-nt98``) fails with ``no such
column: nt98`` (the hyphen parses as a column filter), a slash-prefixed
question (``what did we decide about /land?``) is an FTS5 syntax error, and a
hyphenated term (``push-vs-pull``) fails the same way. Every bd issue id --
the most natural query key in this repo -- would error on query one.
:func:`_escape_query` fixes all three: split on whitespace, wrap EACH
resulting token in double quotes (escaping any embedded ``"`` by doubling
it, FTS5's own quoting rule), and join with a space -- FTS5's implicit AND
between phrase terms. A double-quoted phrase is matched as a literal string
by the FTS5 query grammar, so no character inside it (hyphen, slash, digit)
is ever parsed as query syntax.

Implicit AND alone is over-strict for a natural multi-term phrase, which
rarely lands every term in one unit, so :func:`query` retries a zero-hit
search with the same tokens joined by ``OR`` (:func:`_escape_query_or`) and
flags those rows as fallback hits (``lode-qcp0``).
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Annotated

import typer

app = typer.Typer(add_completion=False)

#: Chunker doc-class tags, in the order they're documented (docs/decisions.md,
#: lode-t6o1's "Left open, deliberately" -- decision-record vs reference/process).
_DOC_CLASSES = ("decision-record", "reference/process")

#: How much of a unit's body to show as a snippet -- enough to orient the
#: reader without reprinting the whole unit (which the acceptance criteria
#: forbids outright; median unit is 2.5 KB, so even a generous snippet stays
#: far short of that).
_SNIPPET_CHARS = 240


#: Bootstraps docs_index_loader.py itself: scripts/ is not an installed package,
#: so it has to be resolved by path rather than imported. Deliberately UNCACHED
#: -- see the lode-7l68 entry in docs/decisions.md (that module must stay
#: stateless).
_loader_path = Path(__file__).resolve().parent / "docs_index_loader.py"
_loader_spec = importlib.util.spec_from_file_location("docs_index_loader", _loader_path)
assert _loader_spec is not None and _loader_spec.loader is not None
_loader_module = importlib.util.module_from_spec(_loader_spec)
_loader_spec.loader.exec_module(_loader_module)


def _load_sibling(name: str, filename: str) -> ModuleType:
    """Load a ``scripts/`` sibling module under a PRIVATE ``sys.modules`` name.

    ``scripts/`` is not an installed package, so a plain ``import`` fails from
    a caller that does not have it on ``sys.path`` -- which includes this
    module's own tests, loaded by path via ``tests/conftest.py``'s
    ``load_module_from_path``. The private name is what keeps this load from
    colliding with any other loader of the same file; the cache-on-``name``
    check is what keeps one name mapped to exactly ONE module object, which
    the callers below depend on.

    Delegates to ``scripts/docs_index_loader.py`` (``lode-wtk2``), which
    every ``scripts/`` sibling loader in this repo now shares -- this
    function's body used to be that implementation; it is now a thin
    pass-through kept so every call site below stays unchanged.

    """
    return _loader_module.load_sibling(name, filename)


def _load_build() -> ModuleType:
    """Load scripts/docs_index_build.py under a PRIVATE sys.modules name.

    Same rationale as that module's own ``_load_chunker``: ``scripts/`` is
    not an installed package, and this module's own test loads THIS module
    by path via ``tests/conftest.py``'s ``load_module_from_path``, which
    registers under ``sys.modules`` -- a name private to this module never
    collides with that or with any other loader of ``docs_index_build``.

    IMPORTANT (per lode-t6o1.2's technical review, recorded on this ticket's
    Design field): this is the ONE place ``docs_index_build`` is loaded from
    here, and ``Unit``/``chunk_corpus``/``build_index``/``cache_db_path`` are
    all taken from THIS loaded copy, never re-imported independently -- a
    second independent load of ``docs_index_chunker.py`` would create a
    distinct ``Unit`` class object and break ``isinstance`` silently.
    """
    return _load_sibling("_docs_index_query_build_impl", "docs_index_build.py")


_build = _load_build()


def _load_log() -> ModuleType:
    """Load scripts/docs_index_log.py under a PRIVATE sys.modules name.

    Same rationale as :func:`_load_build`: ``scripts/`` is not an installed
    package, and this module's own test loads THIS module by path, without
    ``scripts/`` on ``sys.path`` -- a plain ``import docs_index_log`` would
    fail there.

    Called at the one logging site rather than at import, so a query that
    never reaches it -- a `--class` validation error, or a library caller of
    :func:`query` -- does not pay to load a module it will not use.
    The loaded module is cached on ``sys.modules`` under that private name,
    so repeat calls return the same object.
    """
    return _load_sibling("_docs_index_query_log_impl", "docs_index_log.py")


def _escape_query(raw: str, joiner: str = " ") -> str:
    """Tokenize ``raw`` on whitespace and quote each term as an FTS5 phrase.

    See the module docstring for the measured failures this fixes and why.
    Returns the empty string for a raw query with no non-whitespace content
    (an all-whitespace or empty input has no terms to quote).

    NUL bytes are dropped first. Quoting cannot save them: sqlite3 binds a
    ``str`` as a C string, so a NUL anywhere in the MATCH argument truncates
    it mid-token and FTS5 raises ``unterminated string`` -- verified at
    technical review. Unreachable from argv (execve forbids NUL) but not from
    a library caller, so it is handled here rather than assumed away.

    ``joiner`` selects the combining semantics: the default single space is
    FTS5's implicit AND (the primary mode), ``" OR "`` the fallback mode's
    (``lode-qcp0`` -- a natural multi-term phrase almost never lands every
    term in one unit, so AND-only is over-strict as the ONLY mode). Both
    modes share this one copy of the quoting rule, so an escaping fix can
    never reach one mode and miss the other.
    """
    terms = raw.replace("\x00", "").split()
    return joiner.join('"' + term.replace('"', '""') + '"' for term in terms)


def _search(
    conn: sqlite3.Connection,
    match: str,
    doc_class: str | None,
    limit: int,
) -> list[tuple[str, int, int, str, str]]:
    """Run one MATCH query and return raw ``(path, line_lo, line_hi,
    first_line, body)`` rows -- shared by the AND pass and the OR fallback
    pass in :func:`query`, so the SQL shape lives in exactly one place."""
    sql = (
        "SELECT path, line_lo, line_hi, first_line, body FROM units WHERE units MATCH ?"
    )
    params: list[str | int] = [match]
    if doc_class is not None:
        sql += " AND doc_class = ?"
        params.append(doc_class)
    sql += " ORDER BY bm25(units) LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def _snippet(body: str) -> str:
    """A short, single-line preview of a unit's body -- never the whole unit."""
    flat = " ".join(body.split())
    if len(flat) <= _SNIPPET_CHARS:
        return flat
    return flat[:_SNIPPET_CHARS].rstrip() + "..."


def query(
    raw_query: str,
    doc_class: str | None = None,
    limit: int = 5,
) -> list[tuple[str, int, int, str, str, bool]]:
    """Run ``raw_query`` against a freshly built index and return the top
    ``limit`` hits, ranked by FTS5's ``bm25()``, as ``(path, line_lo,
    line_hi, first_line, snippet, used_fallback)`` tuples. Never returns a
    whole unit body.

    ``used_fallback`` is ``True`` on every row when the AND-only search (the
    primary mode) returned zero hits and an OR search over the same terms
    was retried instead (``lode-qcp0`` -- measured ~50% zero-hit rate on
    real multi-term queries). The OR retry only fires on a genuine zero-hit
    AND result, and only when it actually differs from the AND query (a
    single-term query has no OR/AND distinction, so no second query is run).
    """
    match = _escape_query(raw_query)
    if not match:
        return []

    conn = _build.build_index()
    try:
        rows = _search(conn, match, doc_class, limit)
        used_fallback = False
        if not rows:
            or_match = _escape_query(raw_query, " OR ")
            if or_match != match:
                rows = _search(conn, or_match, doc_class, limit)
                used_fallback = bool(rows)
    finally:
        conn.close()

    return [
        (path, line_lo, line_hi, first_line, _snippet(body), used_fallback)
        for path, line_lo, line_hi, first_line, body in rows
    ]


@app.command(
    help=(
        "Find where something is written down in docs/, without reading all "
        "of docs/.\n\nRun this when you need the passage that settles a "
        "question -- a decision id, a term, a phrase. It prints ranked "
        "pointers (path:line_lo-line_hi), each with the unit's first line and "
        "a short snippet, and nothing else: read the cited range yourself. It "
        "never prints a whole unit and never writes prose of its own.\n\nThe "
        "index is rebuilt from docs/ on every run, so results are never "
        "stale.\n\nWhen nothing matches every term at once, it retries "
        "matching ANY term and marks those rows as an OR-match fallback."
    )
)
def main(
    text: Annotated[
        str,
        typer.Argument(help="Search terms -- a bd id, a phrase, anything."),
    ],
    doc_class: Annotated[
        str | None,
        typer.Option(
            "--class",
            help="Restrict to one doc class: decision-record or reference/process.",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            min=1,
            help="Maximum number of ranked pointers to print.",
        ),
    ] = 5,
) -> None:
    """Query the docs/ lookup index and print ranked pointers, never answers.

    Rebuilds the index fresh from docs/*.md on every call (no cache), then
    prints up to --limit results as `path:line_lo-line_hi` + the unit's
    first line + a short snippet. Read the cited range yourself -- this
    tool never prints a whole unit and never synthesizes prose.
    """
    if doc_class is not None and doc_class not in _DOC_CLASSES:
        print(
            f"error: --class must be one of {', '.join(_DOC_CLASSES)}, got {doc_class!r}",
            file=sys.stderr,
        )
        raise typer.Exit(1)

    start = time.perf_counter()
    results = query(text, doc_class=doc_class, limit=limit)
    elapsed_ms = (time.perf_counter() - start) * 1000
    fallback_fired = False
    if results:
        for path, line_lo, line_hi, first_line, snippet, used_fallback in results:
            fallback_fired = fallback_fired or used_fallback
            marker = " [fallback: OR match]" if used_fallback else ""
            print(f"{path}:{line_lo}-{line_hi}{marker}  {first_line}")
            print(f"    {snippet}")
    else:
        print("No results.")

    # Logged AFTER the results are printed, and failing open: instrumentation
    # must never break or delay the thing it measures. An unwritable cache dir
    # (OSError) or an undeterminable home directory (RuntimeError from
    # Path.home()) costs one log line, not the answer the caller asked for --
    # CLAUDE.md routes every agent through this CLI first.
    try:
        _load_log().append_invocation(
            query_text=text,
            hit_count=len(results),
            fallback_fired=fallback_fired,
            elapsed_ms=elapsed_ms,
        )
    except (OSError, RuntimeError):  # fmt: skip
        pass


if __name__ == "__main__":
    app()
