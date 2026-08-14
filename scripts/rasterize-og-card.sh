#!/bin/bash -e
#
# Rasterize docs/assets/og-card.svg to the 1200x630 PNG the docs site's
# `og:image` meta tag (docs/overrides/main.html) points at
# (docs/assets/og-card.png), the reproducible evidence lode-fhql.6's
# acceptance criteria require for the social/OG card.
#
# Uses cairosvg (via a throwaway venv, never the project's own ./venv),
# the same tool and pattern as scripts/rasterize-mark.sh -- kept out of
# pyproject.toml/requirements.lock since this is a one-off asset-generation
# tool, not a runtime or dev dependency of lode itself.
#
# The og-card.svg source specifies its own text font stacks (Inter for the
# positioning line, JetBrains Mono for the wordmark, both falling back to
# system defaults per each file's own header); cairosvg renders with
# whatever fonts are actually installed on the machine running this
# script, so the exact glyph shapes are not guaranteed reproducible across
# machines -- only the layout, geometry, and text content are. Human
# legibility sign-off is a separate, human-labeled step (lode-fhql.13),
# same split as the favicon's 16px render.
#
# Deliberately does NOT source scripts/gate-lib.sh (lode-pcee expects the
# reason stated): this is a human-run, one-off asset generator, not a
# pipeline gate -- nothing classifies its exit code into the 0/1/2 gate
# contract.
#
# Usage: scripts/rasterize-og-card.sh

REPO=$(git rev-parse --show-toplevel)
SVG="$REPO/docs/assets/og-card.svg"
OUT="$REPO/docs/assets/og-card.png"

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
    output_width=1200,
    output_height=630,
)
" "$SVG" "$OUT"

echo "Wrote $OUT"
