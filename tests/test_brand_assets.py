"""Consistency gates for the hand-authored brand assets under ``docs/assets/``.

Two claims in those files are asserted in prose but were, until this module,
enforced by nothing (lode-fhql.4, added in technical review):

1. ``lockup.svg``'s header says it "reuses mark.svg's exact geometry ... so the
   two files never drift". Cross-file ``<use href>`` does not work for
   standalone SVG assets, so the geometry is genuinely copy-pasted and
   duplication is the right mechanism — but the non-drift property needs a
   gate, or a future edit to ``mark.svg`` silently leaves the lockup carrying
   the old mark, discoverable only by eyeballing both.
2. ``mark-blocks.txt`` carries the same 64 cells twice — once as the ASCII
   ``v``/``s``/``.`` proof grid, once as the Unicode block redraw the terminal
   wordmark (lode-fhql.5) consumes. The second is a pure glyph substitution of
   the first, so an edit to one must be an edit to both.

Deliberately NOT gated: re-deriving the grid from ``mark.svg``'s own geometry.
That needs either ``cairosvg`` (which ``scripts/rasterize-mark.sh`` keeps out of
``pyproject.toml``/``requirements.lock`` on purpose) or a hand-transcribed
reimplementation of stroke-coverage maths — itself unverified — to gate a
64-cell artifact that changes approximately never.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parent.parent / "docs" / "assets"

# ASCII proof-grid glyph -> Unicode block-redraw glyph, per mark-blocks.txt's
# own legend.
GLYPHS = {"v": "█", "s": "░", ".": "·"}


def _shape_lines(svg: str) -> list[str]:
    """The mark's drawable elements, whitespace-normalised, in source order."""
    body = svg[svg.index("<svg") :]
    shapes = re.findall(r"<(?:rect|path)\b.*?/>", body, re.DOTALL)
    return [" ".join(s.split()) for s in shapes]


@pytest.mark.parametrize("svg_path", sorted(ASSETS.glob("*.svg")), ids=lambda p: p.name)
def test_svg_is_strict_xml(svg_path: Path) -> None:
    """Every docs/assets/*.svg must parse under a strict XML parser.

    A standalone-opened SVG (MS Edge, etc.) and scripts/rasterize-mark.sh's
    cairosvg/defusedxml both parse strictly -- unlike an SVG embedded via HTML,
    which browsers parse laxly. A literal ``--`` inside an XML comment (XML 1.0
    section 2.5) is the failure mode this gate exists to catch (lode-fhql.17);
    it slipped through review because the lax embedded-HTML path never noticed.
    """
    ET.parse(svg_path)


def test_lockup_carries_the_marks_geometry_verbatim() -> None:
    mark = _shape_lines((ASSETS / "mark.svg").read_text())
    lockup = _shape_lines((ASSETS / "lockup.svg").read_text())

    assert mark, "mark.svg has no <rect>/<path> elements -- parser or asset broke"
    assert lockup[: len(mark)] == mark, (
        "lockup.svg's mark geometry has drifted from mark.svg. The two are "
        "copy-pasted on purpose; edit both, or update this gate deliberately."
    )


def _grids(text: str) -> tuple[list[str], list[str]]:
    ascii_rows = re.findall(r"^[vs.]{8}$", text, re.MULTILINE)
    block_rows = re.findall(r"^[█░·]{8}$", text, re.MULTILINE)
    return ascii_rows, block_rows


def test_block_redraw_matches_the_ascii_proof_grid() -> None:
    ascii_rows, block_rows = _grids((ASSETS / "mark-blocks.txt").read_text())

    assert len(ascii_rows) == 8, f"expected an 8-row ASCII grid, got {len(ascii_rows)}"
    assert len(block_rows) == 8, f"expected an 8-row block grid, got {len(block_rows)}"

    translated = ["".join(GLYPHS[ch] for ch in row) for row in ascii_rows]
    assert translated == block_rows, (
        "mark-blocks.txt's Unicode redraw no longer matches its own ASCII proof "
        "grid cell for cell -- one of the two was edited alone."
    )
