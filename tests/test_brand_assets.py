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

Since lode-fhql.17 this module also gates that every ``docs/assets/*.svg``
parses under a strict XML parser -- a separate concern from the two
consistency claims above, but the same two files and the same owner.

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


def _shapes(svg: str) -> list[str]:
    """The drawable elements' raw source, in document order.

    The single extractor both gates below build on, so a future element type
    (``circle``, ``polygon``, ...) is added in one place and cannot leave the
    two comparisons silently disagreeing about what counts as geometry.
    """
    body = svg[svg.index("<svg") :]
    return re.findall(r"<(?:rect|path)\b.*?/>", body, re.DOTALL)


def _shape_lines(svg: str) -> list[str]:
    """The mark's drawable elements, whitespace-normalised, in source order."""
    return [" ".join(s.split()) for s in _shapes(svg)]


SVGS = sorted(ASSETS.glob("*.svg"))


def test_the_svg_corpus_is_not_empty() -> None:
    """Guard the parametrised gate below against silently collecting nothing.

    An empty parameter set makes pytest SKIP the test rather than fail it, so a
    move or rename of docs/assets/ would retire the strict-XML gate without a
    red run. This assertion is the only thing that makes that loud.
    """
    assert SVGS, f"no *.svg found under {ASSETS} -- the strict-XML gate is inert"


@pytest.mark.parametrize("svg_path", SVGS, ids=lambda p: p.name)
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


def _document(svg: str) -> str:
    """The SVG document itself, with the leading maintainer comment stripped.

    Anchored on ``<svg xmlns``, not on ``<svg``: lockup.svg's header comment
    contains a literal ``<svg>`` and would otherwise be treated as the start
    of the document.
    """
    return svg[svg.index("<svg xmlns") :]


def test_dark_lockup_is_the_lockup_recoloured_and_nothing_else() -> None:
    """lockup-dark.svg differs from lockup.svg only in the root ``color``.

    The dark variant exists solely to give <picture>'s
    ``prefers-color-scheme: dark`` source a paper-coloured lockup
    (lode-fhql.19); its geometry is a verbatim copy. Without this gate an edit
    to lockup.svg silently leaves the dark variant behind, and the drift is
    invisible to anyone on the other theme -- exactly the failure mode that
    produced this ticket.
    """
    light = _document((ASSETS / "lockup.svg").read_text())
    dark = _document((ASSETS / "lockup-dark.svg").read_text())

    assert 'color="#F7F4EE"' in dark, (
        "lockup-dark.svg's root color is no longer paper (#F7F4EE, "
        "docs/brand.md section 3) -- it will not be legible on a dark theme."
    )
    assert dark == light.replace('color="#1E1B2E"', 'color="#F7F4EE"'), (
        "lockup-dark.svg is no longer lockup.svg recoloured. The two are "
        "copy-pasted on purpose (only the root colour differs); edit both, or "
        "update this gate deliberately."
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


# Attributes that carry the actual shape (position/size/path), as opposed to
# presentation (fill/stroke/opacity). og-card.svg (lode-fhql.6) deliberately
# hard-codes ink hex fills instead of mark.svg/lockup.svg's currentColor (an
# OG card is a fixed, non-themable raster -- see og-card.svg's own header),
# so a byte-for-byte comparison like test_lockup_carries_the_marks_geometry_verbatim
# would false-positive on that intentional difference. Compare geometry only.
_GEOMETRY_ATTRS = ("d", "x", "y", "width", "height")


def _geometry(svg: str) -> list[dict[str, str]]:
    """Each drawable element's geometry-only attributes, in source order."""
    out = []
    for shape in _shapes(svg):
        attrs = dict(re.findall(r'(\w[\w-]*)="([^"]*)"', shape))
        out.append({k: v for k, v in attrs.items() if k in _GEOMETRY_ATTRS})
    return out


def _id_mark_geometry(svg_text: str, source_name: str) -> list[dict[str, str]]:
    """Geometry of the ``<g id="mark">`` group's own rect/path children.

    Shared by every derived asset (og-card.svg, favicon-mark.svg, ...) that
    wraps a verbatim copy of mark.svg's geometry in its own transformed/
    recoloured group alongside other elements mark.svg doesn't have (a
    background rect, wordmark text, ...) -- scope the comparison to just
    that group, in the same untransformed 0-32 coordinate space mark.svg
    uses. Selected by `id="mark"`, not by being the first `<g>` in the file:
    adding a second group later must not silently re-point a gate at the
    wrong geometry.

    Searches the document with its leading maintainer comment stripped
    (``_document``), not the raw file text: a comment that itself mentions
    the literal string ``<g id="mark">`` in prose (as favicon-mark.svg's
    does) would otherwise let this regex latch onto that prose and swallow
    everything up to the real element's closing ``</g>``.
    """
    document = _document(svg_text)
    group = re.search(r'<g\b[^>]*id="mark"[^>]*>(.*?)</g>', document, re.DOTALL)
    assert group, (
        f'{source_name} has no <g id="mark"> wrapping the copied mark geometry'
    )
    return _geometry(f"<svg>{group.group(1)}</svg>")


def test_og_card_carries_the_marks_geometry() -> None:
    mark = _geometry((ASSETS / "mark.svg").read_text())
    og_card = _id_mark_geometry((ASSETS / "og-card.svg").read_text(), "og-card.svg")

    assert mark, "mark.svg has no <rect>/<path> elements -- parser or asset broke"
    assert og_card == mark, (
        "og-card.svg's mark geometry has drifted from mark.svg. The two are "
        "copy-pasted on purpose (colour differs deliberately); edit both, or "
        "update this gate deliberately."
    )


def test_favicon_mark_carries_the_marks_geometry() -> None:
    """favicon-mark.svg (lode-fhql.22) wraps a verbatim copy of mark.svg's
    geometry in its own `<g id="mark">`, alongside the paper background tile
    mark.svg doesn't have -- same drift risk and same gate shape as
    test_og_card_carries_the_marks_geometry.
    """
    mark = _geometry((ASSETS / "mark.svg").read_text())
    favicon_mark = _id_mark_geometry(
        (ASSETS / "favicon-mark.svg").read_text(), "favicon-mark.svg"
    )

    assert mark, "mark.svg has no <rect>/<path> elements -- parser or asset broke"
    assert favicon_mark == mark, (
        "favicon-mark.svg's mark geometry has drifted from mark.svg. The two "
        "are copy-pasted on purpose (colour and background tile differ "
        "deliberately); edit both, or update this gate deliberately."
    )


def test_favicon_mark_has_a_theme_neutral_background_tile() -> None:
    """favicon-mark.svg's whole point (lode-fhql.22) is a fixed paper tile
    behind the mark, so the favicon is legible regardless of the browser
    tab's own theme. Assert the tile is present, paper-coloured, and covers
    the full 32x32 viewBox -- not just present-but-wrong-size/colour.
    """
    svg = (ASSETS / "favicon-mark.svg").read_text()
    document = _document(svg)
    tile = re.search(r"<rect\b[^>]*/>", document)
    assert tile, 'favicon-mark.svg has no background <rect> before <g id="mark">'
    assert document.index(tile.group(0)) < document.index('id="mark"'), (
        "favicon-mark.svg's background tile must be drawn before (under) the mark"
    )
    attrs = dict(re.findall(r'(\w[\w-]*)="([^"]*)"', tile.group(0)))
    assert attrs.get("fill") == "#F7F4EE", (
        "favicon-mark.svg's background tile is not paper (#F7F4EE, "
        "docs/brand.md section 3)"
    )
    assert (attrs.get("x"), attrs.get("y")) == ("0", "0"), (
        "favicon-mark.svg's background tile does not start at the viewBox origin"
    )
    assert (attrs.get("width"), attrs.get("height")) == ("32", "32"), (
        "favicon-mark.svg's background tile does not cover the full 32x32 viewBox"
    )
