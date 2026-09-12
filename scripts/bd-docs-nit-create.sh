#!/usr/bin/env bash
#
# Create a new, empty docs-nits collector ticket (the standard shape) --
# reserved label `docs-nits`, the STANDARD title below, and a how-to-add /
# how-to-fix description -- and confirm it landed before returning its id.
#
# Reverses "a human opens it" (docs/agents-workflow.md, scripts/bd-docs-nit.sh's
# old N==0 refusal): per lode-z1n5 part 1, scripts/bd-docs-nit.sh append calls
# this the moment it finds ZERO open docs-nits collectors, so a nit is never
# lost just because the standing collector was just drained/closed. /code
# (claim-time successor, part 3) and /land (reopen-on-close backstop, part 4)
# call it directly for the same reason, via this one script -- never an inline
# `bd create` of their own.
#
# bd's on-create validation is warn-only (.beads/config.yaml
# validation.on-create: warn): a create missing required sections STILL
# CREATES and only warns on stderr. A caller that pipes `bd create` into `jq`
# and reads the pipe's own non-zero exit as "not created" will retry and
# duplicate -- this is exactly how lode-wm4l happened (jq exit 5 on a clean
# create). So this script (a) passes full description + acceptance text so the
# warning never fires, (b) writes --json output to a FILE, never a pipe, and
# (c) confirms creation with a follow-up `bd list`, never by trusting
# `bd create`'s own exit status alone.
#
# Usage: scripts/bd-docs-nit-create.sh
#
# Prints the new collector's id to stdout on success.
# Exit 0 -> created, confirmed open via a follow-up `bd list`, and pushed.
# Exit 2 -> MACHINE FAULT: `bd create` failed, its --json could not be parsed,
#           the follow-up `bd list` could not confirm the id, or the push
#           failed. There is no legitimate "not exactly one" state here (this
#           script only ever adds a collector), so every failure is exit 2.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=scripts/docs-nits-constants.sh
. "$SCRIPT_DIR/docs-nits-constants.sh"
readonly DESCRIPTION="Collector for wording-only doc/comment nits (docs/agents-workflow.md \"Wording-only doc nits go to the docs-nits collector\", lode-t551 / lode-z1n5).

How a nit gets here: an agent finds a wording-only nit and runs \`scripts/bd-docs-nit.sh append\`, which appends a NIT-prefixed note here in the mandated patch shape (file, line, verbatim anchor, exact replacement).

How this gets drained: a human runs /code on this ticket once the batch is worth it. The batch builder applies each accepted nit, re-appends or files as its own ticket any nit it declines, drops any nit whose anchor is no longer present (already fixed), and lists every nit's disposition in its hand-off note."
readonly ACCEPTANCE="Every NIT note on this ticket is, before hand-off, either applied, carried over to the successor collector, filed as its own ticket, or dropped as anchor-gone -- and the hand-off note says which for each one."

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

if ! bd create --title "$STANDARD_TITLE" --description "$DESCRIPTION" \
  --acceptance "$ACCEPTANCE" --type=task --priority=4 --label "$DOCS_NITS_LABEL" \
  --json >"$tmp" 2>/dev/null; then
  echo "bd-docs-nit-create.sh: \`bd create\` failed" >&2
  exit 2
fi

id="$(jq -r '.id // (.[0].id) // empty' "$tmp" 2>/dev/null)" || id=""
if [ -z "$id" ]; then
  echo "bd-docs-nit-create.sh: could not parse the new collector's id from \`bd create --json\` output" >&2
  exit 2
fi

# Confirm by a follow-up list -- never by trusting `bd create`'s own exit
# status, which is 0 even on a warn-only validation failure.
if ! confirm_rows="$(bd list --label "$DOCS_NITS_LABEL" --status open --id "$id" --limit 0 --json 2>/dev/null)"; then
  echo "bd-docs-nit-create.sh: the follow-up \`bd list --id $id\` failed" >&2
  exit 2
fi
if ! printf '%s' "$confirm_rows" | jq -e '(. // []) | length == 1' >/dev/null 2>&1; then
  echo "bd-docs-nit-create.sh: follow-up \`bd list\` could not confirm $id was created open" >&2
  exit 2
fi

# stdout is this script's RETURN CHANNEL (the new collector id), and
# scripts/bd-docs-nit.sh append captures it in a command substitution -- so the
# push's own chatter must not land there. Its diagnostics stay visible on stderr.
if ! "$SCRIPT_DIR/bd-dolt-push.sh" >&2; then
  echo "bd-docs-nit-create.sh: scripts/bd-dolt-push.sh failed" >&2
  exit 2
fi

printf '%s\n' "$id"
