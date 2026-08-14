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
# Only the two ASSETS differ, though -- the rasterizing step itself is
# identical -- so this is a thin wrapper over scripts/rasterize-mark.sh's
# stem arguments rather than a copy of its throwaway-venv/cairosvg-pin
# boilerplate, which would then need bumping in lockstep with nothing
# gating that the two agree.
#
# Usage: scripts/rasterize-favicon-mark.sh [SIZE]
#
# SIZE defaults to 16 (the committed docs/assets/favicon-16.png). Any other
# size writes docs/assets/favicon-<SIZE>.png instead and is not committed.

exec "$(git rev-parse --show-toplevel)/scripts/rasterize-mark.sh" \
    "${1:-16}" favicon-mark favicon
