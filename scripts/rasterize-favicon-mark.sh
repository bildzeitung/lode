#!/bin/bash -e
#
# Rasterize docs/assets/favicon-mark.svg to a 16x16 PNG
# (docs/assets/favicon-16.png), wired as mkdocs.yml's `theme.favicon`
# (lode-fhql.22). Deliberately a separate source and output from
# scripts/rasterize-mark.sh / docs/assets/mark-16.png: favicon-mark.svg
# carries a fixed paper background tile behind the ink mark so the tab icon
# stays legible against both light and dark browser chrome, which
# mark.svg's plain currentColor render does not (see favicon-mark.svg's own
# header for why the two files diverge).
#
# Uses cairosvg (via a throwaway venv, never the project's own ./venv), the
# same tool and pattern as scripts/rasterize-mark.sh -- kept out of
# pyproject.toml/requirements.lock since this is a one-off asset-generation
# tool, not a runtime or dev dependency of lode itself.
#
# Deliberately does NOT source scripts/gate-lib.sh (lode-pcee expects the
# reason stated): this is a human-run, one-off asset generator, not a
# pipeline gate -- nothing classifies its exit code into the 0/1/2 gate
# contract.
#
# Usage: scripts/rasterize-favicon-mark.sh [SIZE]
#
# SIZE defaults to 16 (the committed docs/assets/favicon-16.png). Any other
# size writes docs/assets/favicon-<SIZE>.png instead and is not committed.

SIZE="${1:-16}"

REPO=$(git rev-parse --show-toplevel)
SVG="$REPO/docs/assets/favicon-mark.svg"
OUT="$REPO/docs/assets/favicon-$SIZE.png"

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
