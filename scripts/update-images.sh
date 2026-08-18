#!/bin/bash -ex
#
# Pull the third-party Docker images lode uses for tooling.
#
# minlag/mermaid-cli: validates the Mermaid diagrams in docs/ (see
# scripts/validate-mermaid.sh) and renders them for the docs site (see
# scripts/build_docs_site.py's MERMAID_IMAGE). Bundles the mermaid.js parser
# GitHub renders with, so it catches diagram syntax errors before they ship.
# Pinned (lode-3ld8) to the SAME tag as both those consumers -- keep in sync;
# tests/test_build_docs_site.py's
# test_validate_mermaid_and_update_images_pin_match_build_docs_site enforces
# it.

docker pull minlag/mermaid-cli:10.9.1
