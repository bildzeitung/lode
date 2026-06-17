#!/bin/bash -ex
#
# Pull the third-party Docker images lode uses for tooling.
#
# minlag/mermaid-cli: validates the Mermaid diagrams in docs/ (see
# scripts/validate-mermaid.sh). Bundles the mermaid.js parser GitHub renders
# with, so it catches diagram syntax errors before they ship.

docker pull minlag/mermaid-cli:latest
