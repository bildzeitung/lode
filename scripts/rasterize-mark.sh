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
# Usage: scripts/rasterize-mark.sh

REPO=$(git rev-parse --show-toplevel)
SVG="$REPO/docs/assets/mark.svg"
OUT="$REPO/docs/assets/mark-16.png"

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

python3 -m venv "$WORKDIR/venv"
"$WORKDIR/venv/bin/pip" install --quiet cairosvg==2.9.0

"$WORKDIR/venv/bin/python3" -c "
import cairosvg
cairosvg.svg2png(
    url='$SVG',
    write_to='$OUT',
    output_width=16,
    output_height=16,
)
"

echo "Wrote $OUT"
