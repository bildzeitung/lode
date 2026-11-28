#!/usr/bin/env bash
#
# Run the lode CLI from this checkout: ./lode.sh <arguments>
#
# --project pins uv to this script's checkout while leaving the caller's
# working directory alone, so relative paths in <arguments> resolve where the
# caller typed them and the script works when invoked from any directory.
set -euo pipefail

exec uv run --frozen --project "$(dirname "$(realpath "$0")")" lode "$@"
