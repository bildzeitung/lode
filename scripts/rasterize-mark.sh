#!/bin/bash -e
#
# Rasterize docs/assets/mark.svg to a 16x16 PNG (docs/assets/mark-16.png),
# the reproducible evidence lode-fhql.4's acceptance criteria require for
# the mark's 16px favicon-size claim (legibility itself is a human
# sign-off, per lode-fhql.12 -- this script only asserts the artifact and
# the command exist).
#
# Uses cairosvg (via a throwaway venv, never the project's own ./venv) so
# this stays outside pyproject.toml/requirements.lock -- it is a one-off
# asset-generation tool, not a runtime or dev dependency of lode itself.
#
# Deliberately does NOT source scripts/gate-lib.sh (lode-pcee expects the
# reason stated): this is a human-run, one-off asset generator, not a pipeline
# gate -- nothing classifies its exit code into the 0/1/2 gate contract.
#
# Usage: scripts/rasterize-mark.sh [SIZE] [SOURCE_STEM] [OUT_STEM]
#
# SIZE defaults to 16 (the committed docs/assets/mark-16.png). Any other size
# writes docs/assets/mark-<SIZE>.png instead and is not committed -- that path
# exists so the 80x80 render docs/assets/mark-blocks.txt cites as the source of
# its 8x8 grid (10px per cell) is reproducible with this same script.
#
# SOURCE_STEM/OUT_STEM name docs/assets/<SOURCE_STEM>.svg and
# docs/assets/<OUT_STEM>-<SIZE>.png, both defaulting to "mark". They exist so a
# second square-SVG-to-PNG asset does not need a clone of this file: the
# rasterizing step is identical no matter which mark variant is being rendered
# (scripts/rasterize-favicon-mark.sh, lode-fhql.22, is a thin wrapper that only
# supplies these two stems). Unlike scripts/rasterize-og-card.sh (fixed
# 1200x630) and scripts/rasterize-favicon-ico.sh (multi-resolution ICO
# packing), which do more than this and keep their own files.

SIZE="${1:-16}"
SOURCE_STEM="${2:-mark}"
OUT_STEM="${3:-mark}"

REPO=$(git rev-parse --show-toplevel)
SVG="$REPO/docs/assets/$SOURCE_STEM.svg"
OUT="$REPO/docs/assets/$OUT_STEM-$SIZE.png"

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

python3 -m venv "$WORKDIR/venv"
"$WORKDIR/venv/bin/pip" install --quiet cairosvg==2.9.0

# Paths go in as argv, not interpolated into the Python source: a repo path
# containing a quote would otherwise break the string literal.
"$WORKDIR/venv/bin/python3" -c "
import sys
import cairosvg
cairosvg.svg2png(
    url=sys.argv[1],
    write_to=sys.argv[2],
    output_width=int(sys.argv[3]),
    output_height=int(sys.argv[3]),
)
" "$SVG" "$OUT" "$SIZE"

echo "Wrote $OUT"
