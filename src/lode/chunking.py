"""Structure-aware passage chunker: a version body to passages (lode-x6r.1).

Passages are **the retrieval unit** (``docs/retrieval.md``, "Chunking: passages
are the retrieval unit"): the head version's body is chunked here, and the
passage -- not the whole note -- is what later gets embedded, lexically indexed,
fused, and reranked. This module is *only* the chunker (body to passages); the
embedding leg (``lode-x6r.2``) and the FTS5 lexical index (``lode-x6r.4``) sit
downstream and persist what :func:`chunk` returns.

The split is **structure-aware with a token fallback** (``docs/retrieval.md``):

1. Split on the note's own structure -- markdown headings, paragraphs, and list
   items -- so a section boundary is a passage boundary (meeting notes have
   sections; runbooks have numbered steps).
2. **Sub-split any block over N tokens** into overlapping windows so passage size
   stays bounded for the embedder; consecutive windows share ``overlap`` tokens
   so a sentence straddling a window boundary is not orphaned.
3. Record ``parent_block`` -- the enclosing section (or the whole note when it
   has no headings) -- for **small-to-big retrieval**: match the precise passage,
   expand to its section for the Q&A LLM's context, cite the precise span.

The output rows match the ``passages`` schema (``schema.sql`` / ``docs/storage.md``
data shape): ``passage_id, target_version, ord, char_range, text, parent_block``.

**Deterministic and local -- no LLM, no network.** :func:`chunk` is a pure
function of ``(body, target_version, settings)``: identical input yields
identical passages. This is mandated by the capture-path and privacy stances
(``docs/retrieval.md``) -- chunking rides the async embedding leg, so capture
stays instant and nothing leaves the box.

The token unit is a **whitespace-word approximation**, counted in-module
(:func:`_count_tokens`) rather than via a model tokenizer -- that keeps the
chunker network-free and dependency-light. ``N`` (:attr:`Settings.chunk_threshold_tokens`)
and ``overlap`` (:attr:`Settings.chunk_overlap_tokens`) are **tune knobs**
(``docs/configuration.md``), deferred to the eval harness; the word-count proxy
is an honest stand-in until those are tuned against real corpus data.

``char_range`` is ``"start:end"`` -- half-open character offsets into ``body`` --
so that ``body[start:end] == passage.text`` holds for every passage (the precise
span a citation points at). ``passage_id`` is derived from ``target_version``
(itself a content-address hash, ``lode.hashing``) plus the passage's ordinal,
which is deterministic and unique without re-deriving a content-address here.
"""

import re
from dataclasses import dataclass

from lode.config import Settings

#: A markdown ATX heading: one to six leading ``#`` then a space. Opens a section.
_HEADING_RE = re.compile(r"^#{1,6}\s")

#: A list item: an optional indent, then a bullet (``-``/``*``/``+``) or an
#: ordered marker (``1.`` / ``1)``), then a space. Each item is its own block.
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")

#: A "token" for the size fallback: one maximal run of non-whitespace. A
#: deliberate whitespace-word approximation (see module docstring) -- no model
#: tokenizer, so the chunker stays deterministic and network-free.
_TOKEN_RE = re.compile(r"\S+")


@dataclass(frozen=True)
class Passage:
    """One structure-aware chunk -- a ``passages`` row (``schema.sql``).

    ``char_range`` is ``"start:end"`` half-open char offsets into the source
    body, so ``body[start:end] == text``. ``parent_block`` is the enclosing
    section (or the whole note when unstructured), carried for small-to-big
    context expansion (``docs/retrieval.md``).
    """

    passage_id: str
    target_version: str
    ord: int
    char_range: str
    text: str
    parent_block: str


@dataclass(frozen=True)
class _Block:
    """A structural unit of the body: ``body[start:end]`` plus whether it is a heading.

    Offsets are half-open into the source body; ``end`` excludes any trailing
    newline so a block's span is tight to its visible text. ``is_heading`` marks
    a block that opens a section (used to compute section boundaries).
    """

    start: int
    end: int
    is_heading: bool


def _count_tokens(text: str) -> int:
    """Approximate token count: the number of whitespace-delimited runs in ``text``."""
    return len(_TOKEN_RE.findall(text))


def _segment(body: str) -> list[_Block]:
    """Split ``body`` into structural blocks in document order.

    Headings and list items each become their own block; consecutive ordinary
    lines accumulate into a paragraph block; a blank line ends the current
    paragraph. Block ``end`` offsets exclude the trailing newline.
    """
    blocks: list[_Block] = []
    para_start: int | None = None
    para_end: int | None = None

    def _flush() -> None:
        nonlocal para_start, para_end
        if para_start is not None and para_end is not None:
            blocks.append(_Block(para_start, para_end, is_heading=False))
        para_start = para_end = None

    offset = 0
    for line in body.splitlines(keepends=True):
        start = offset
        offset += len(line)
        # content_end drops the trailing CR/LF so a block span is tight.
        content_end = start + len(line.rstrip("\r\n"))

        if line.strip() == "":
            _flush()
            continue
        if _HEADING_RE.match(line):
            _flush()
            blocks.append(_Block(start, content_end, is_heading=True))
            continue
        if _LIST_ITEM_RE.match(line):
            _flush()
            blocks.append(_Block(start, content_end, is_heading=False))
            continue
        # Ordinary line: extend (or open) the current paragraph block.
        if para_start is None:
            para_start = start
        para_end = content_end

    _flush()
    return blocks


def _section_ranges(body: str, blocks: list[_Block]) -> list[tuple[int, int]]:
    """Half-open ``(start, end)`` char ranges of the body's sections.

    A heading opens a section that runs until the next heading; content before
    the first heading is an implicit leading section. A body with no headings is
    one section spanning the whole body -- so every passage's ``parent_block``
    is then the whole note (``docs/storage.md``: the enclosing section/note).
    """
    heading_starts = [block.start for block in blocks if block.is_heading]
    points: list[int] = []
    if not heading_starts or heading_starts[0] != 0:
        points.append(0)
    points.extend(heading_starts)
    points.append(len(body))
    return [(points[i], points[i + 1]) for i in range(len(points) - 1)]


def _parent_block(body: str, sections: list[tuple[int, int]], pos: int) -> str:
    """The enclosing section text for a block starting at ``pos`` (whitespace-trimmed)."""
    for start, end in sections:
        if start <= pos < end:
            return body[start:end].strip()
    return body.strip()


def _split_block(body: str, block: _Block, n: int, step: int):
    """Yield ``(start, end)`` char spans for one block, sub-splitting if over ``n`` tokens.

    A block within the budget yields its whole span. A larger block is yielded as
    overlapping token windows of width ``n`` advancing by ``step`` (``= n - overlap``),
    each span trimmed to token boundaries so ``body[start:end]`` is exact.
    """
    tokens = list(_TOKEN_RE.finditer(body[block.start : block.end]))
    if len(tokens) <= n:
        yield (block.start, block.end)
        return
    i = 0
    while i < len(tokens):
        window = tokens[i : i + n]
        yield (block.start + window[0].start(), block.start + window[-1].end())
        if i + n >= len(tokens):
            return
        i += step


def chunk(
    body: str,
    target_version: str,
    *,
    settings: Settings | None = None,
) -> list[Passage]:
    """Chunk ``body`` into structure-aware passages for ``target_version``.

    Splits on markdown headings / paragraphs / list items, sub-splits any block
    over ``settings.chunk_threshold_tokens`` into windows overlapping by
    ``settings.chunk_overlap_tokens``, and records each passage's enclosing
    section as ``parent_block``. Pure and deterministic -- no LLM, no network.

    Returns passages in document order with ``ord`` running ``0..k-1``. A body
    with no content yields an empty list.
    """
    settings = settings or Settings()
    n = settings.chunk_threshold_tokens
    # step must stay positive even if overlap >= N (config bounds them
    # independently); clamping to 1 keeps the window advancing.
    step = max(1, n - settings.chunk_overlap_tokens)

    blocks = _segment(body)
    if not blocks:
        return []
    sections = _section_ranges(body, blocks)

    passages: list[Passage] = []
    for block in blocks:
        parent = _parent_block(body, sections, block.start)
        for start, end in _split_block(body, block, n, step):
            ord_ = len(passages)
            passages.append(
                Passage(
                    passage_id=f"{target_version}:{ord_}",
                    target_version=target_version,
                    ord=ord_,
                    char_range=f"{start}:{end}",
                    text=body[start:end],
                    parent_block=parent,
                )
            )
    return passages
