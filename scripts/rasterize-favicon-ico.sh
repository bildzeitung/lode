#!/bin/bash -e
#
# Build docs/assets/favicon.ico -- the classic ICO fallback for browsers
# that still request /favicon.ico directly rather than honoring an SVG or
# PNG <link rel="icon">. docs/assets/favicon-mark.svg is the favicon SVG
# source (lode-fhql.22 -- a theme-neutral, fixed-tile variant of mark.svg,
# NOT mark.svg itself, which mkdocs.yml's `theme.logo` still uses unchanged
# for the site header; see favicon-mark.svg's own header for why the two
# diverge); docs/assets/favicon-16.png (lode-fhql.22) is the PNG fallback,
# wired as `theme.favicon` in mkdocs.yml. This script produces the third,
# ICO, fallback.
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
SVG="$REPO/docs/assets/favicon-mark.svg"
OUT="$REPO/docs/assets/favicon.ico"

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

python3 -m venv "$WORKDIR/venv"
"$WORKDIR/venv/bin/pip" install --quiet cairosvg==2.9.0

# Paths go in as argv, not interpolated into the Python source: a repo path
# containing a quote would otherwise break the string literal. The SVG is
# handed to cairosvg by `url=`, exactly as scripts/rasterize-mark.sh and
# scripts/rasterize-og-card.sh do -- which requires favicon-mark.svg to
# parse under a strict XML parser (cairosvg goes through defusedxml's
# expat-based ElementTree). That is not an assumption: lode-fhql.17 fixed
# the literal '--' that used to sit in mark.svg's header comment and added a
# gate over every docs/assets/*.svg
# (tests/test_brand_assets.py::test_svg_is_strict_xml) so it cannot come
# back unnoticed, in any file under that glob including this one.
"$WORKDIR/venv/bin/python3" -c "
import struct
import sys

import cairosvg

svg_path, out_path = sys.argv[1], sys.argv[2]

sizes = (16, 32, 48)
pngs = [
    cairosvg.svg2png(url=svg_path, output_width=s, output_height=s)
    for s in sizes
]

# ICONDIR header: reserved(2)=0, type(2)=1 (icon), count(2)
header = struct.pack('<HHH', 0, 1, len(sizes))

# ICONDIRENTRY per image: width, height, colour count/reserved byte pairs,
# planes, bpp, data size, data offset. Each dimension is a single byte, so
# 256 would have to be encoded as 0 -- every size above is well under that,
# so no such encoding is needed here. 32-bit-depth PNG data embedded
# verbatim is a documented, universally-supported ICO variant.
offset = len(header) + len(sizes) * 16
entries = b''
for size, png in zip(sizes, pngs):
    entries += struct.pack(
        '<BBBBHHII', size, size, 0, 0, 1, 32, len(png), offset
    )
    offset += len(png)

with open(out_path, 'wb') as f:
    f.write(header)
    f.write(entries)
    for png in pngs:
        f.write(png)
" "$SVG" "$OUT"

echo "Wrote $OUT"
