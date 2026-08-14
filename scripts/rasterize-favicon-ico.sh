#!/bin/bash -e
#
# Build docs/assets/favicon.ico -- the classic ICO fallback for browsers
# that still request /favicon.ico directly rather than honoring an SVG or
# PNG <link rel="icon">. docs/assets/mark.svg is the favicon SVG source
# (already referenced as `theme.logo` in mkdocs.yml, and reused verbatim,
# not redrawn, per lode-fhql.6); docs/assets/mark-16.png (lode-fhql.4) is
# the PNG fallback, already wired as `theme.favicon` in mkdocs.yml. This
# script produces the third, ICO, fallback.
#
# Uses only cairosvg (via a throwaway venv, never the project's own
# ./venv) plus the Python standard library -- the same rasterizer as
# scripts/rasterize-mark.sh, kept out of pyproject.toml/requirements.lock
# since this is a one-off asset-generation tool, not a runtime or dev
# dependency of lode itself. Deliberately does NOT depend on Pillow: this
# host has no jpeg/zlib headers for Pillow's C extension to build against,
# and the modern ICO format needs none of Pillow's machinery anyway -- an
# ICO file is just a small directory header (ICONDIR/ICONDIRENTRY, per the
# MS-ICO / Win32 icon-resource spec) pointing at one or more embedded PNGs
# verbatim. Every current browser and OS reads PNG-in-ICO entries.
#
# A multi-resolution ICO (16/32/48px) is standard practice: the OS/browser
# picks the size it needs at render time rather than upscaling a single
# raster.
#
# Deliberately does NOT source scripts/gate-lib.sh (lode-pcee expects the
# reason stated): this is a human-run, one-off asset generator, not a
# pipeline gate -- nothing classifies its exit code into the 0/1/2 gate
# contract.
#
# Usage: scripts/rasterize-favicon-ico.sh

REPO=$(git rev-parse --show-toplevel)
SVG="$REPO/docs/assets/mark.svg"
OUT="$REPO/docs/assets/favicon.ico"

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

python3 -m venv "$WORKDIR/venv"
"$WORKDIR/venv/bin/pip" install --quiet cairosvg==2.9.0

# Paths go in as argv, not interpolated into the Python source: a repo path
# containing a quote would otherwise break the string literal. Strip XML
# comments before handing the source to cairosvg's strict parser: mark.svg's
# header comment currently contains a literal '--' (CSS var(--paper),
# lode-fhql.17's fix for the same class of bug is unlanded as of this
# writing) which trips defusedxml's expat-based ElementTree parser, even
# though the comment carries no rendering meaning at all. Comments are
# irrelevant to the rasterized pixels either way, so stripping them here
# does not touch the committed file and makes this script robust to the
# tracked source having (or not having) a stray '--' in a comment.
"$WORKDIR/venv/bin/python3" -c "
import re
import struct
import sys

import cairosvg

svg_path, out_path = sys.argv[1], sys.argv[2]
with open(svg_path, 'rb') as f:
    svg_bytes = re.sub(rb'<!--.*?-->', b'', f.read(), flags=re.DOTALL)

sizes = (16, 32, 48)
pngs = [
    cairosvg.svg2png(bytestring=svg_bytes, output_width=s, output_height=s)
    for s in sizes
]

# ICONDIR header: reserved(2)=0, type(2)=1 (icon), count(2)
header = struct.pack('<HHH', 0, 1, len(sizes))

# ICONDIRENTRY per image: width, height (0 means 256), colour count/reserved
# byte pairs, planes, bpp, data size, data offset. 32-bit-depth PNG data
# embedded verbatim is a documented, universally-supported ICO variant.
offset = len(header) + len(sizes) * 16
entries = b''
for size, png in zip(sizes, pngs):
    dim = size if size < 256 else 0
    entries += struct.pack(
        '<BBBBHHII', dim, dim, 0, 0, 1, 32, len(png), offset
    )
    offset += len(png)

with open(out_path, 'wb') as f:
    f.write(header)
    f.write(entries)
    for png in pngs:
        f.write(png)
" "$SVG" "$OUT"

echo "Wrote $OUT"
