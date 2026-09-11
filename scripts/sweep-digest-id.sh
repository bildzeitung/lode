#!/usr/bin/env bash
#
# Resolve /sweep's single digest issue -- the one bd issue carrying the reserved
# `sweep-digest` label -- and REFUSE unless there is exactly one
# (.claude/skills/sweep/SKILL.md sections 5 and 6).
#
# Extracted per lode-x495's technical review. /sweep runs each fenced bash block
# as its own Bash tool invocation, so no shell state crosses between them: the
# `$DIGEST_ID` that section 4 resolves cannot reach section 5 (which READS the
# digest body) or section 6 (which REWRITES it wholesale). Both therefore have to
# re-derive it. Written inline that was two byte-for-byte copies of the same bd
# query + jq count + refusal, already drifting in their diagnostics -- exactly the
# "logic shared by two call sites belongs in scripts/, never duplicated in
# markdown" rule docs/agents-workflow.md states, and the same "ungated inline
# shell in a SKILL.md rots silently" lesson as scripts/epic-children-closed.sh
# (lode-v4rk, whose three inline copies were all silently broken),
# scripts/release-latest-tag.sh (lode-b2bf) and scripts/release-bump.sh
# (lode-ns3r).
#
# The resolve-by-label query and the 0/1/2 refusal contract itself now live in
# scripts/bd-label-single-id.sh (lode-ayfm), shared with scripts/bd-docs-nit.sh
# -- this script is a thin wrapper supplying the `sweep-digest` label, `--all`
# (a closed digest is still a duplicate a human must resolve), and its own
# per-label advisory wording for the N==0 and N>1 refusal paths.
#
# Usage: scripts/sweep-digest-id.sh
#
# Exit 0  -> prints the digest issue's id to stdout. Exactly one exists.
# Exit 1  -> NOT exactly one digest. Diagnostic to stderr, nothing to stdout.
#            This is a legitimate state, not a fault, and the caller must not
#            proceed past it:
#              N == 0  section 4's bootstrap/no-op path owns this. Either the
#                      queue is empty (a clean no-op pass) or section 4 has not
#                      created the digest yet.
#              N >  1  section 4's anomaly path owns this: "do not guess which
#                      is authoritative and do not write anything." A human
#                      consolidates by hand. Picking `.[0].id` here would
#                      silently overwrite whichever duplicate happened to sort
#                      first -- the precise failure section 4 forbids, and the
#                      reason this refusal is mechanical rather than prose.
# Exit 2  -> MACHINE FAULT (bd or jq failed, malformed JSON, or a bad
#            invocation of the shared helper). Same "exit 2 is the machine,
#            never the content" convention as scripts/release-bump.sh /
#            scripts/release-latest-tag.sh / scripts/merge-precheck.sh.
#
# Read-only: only ever calls `bd list`, never a bd write.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [ "$#" -ne 0 ]; then
  echo "sweep-digest-id.sh: takes no arguments (got: $*)" >&2
  exit 2
fi

exec "$SCRIPT_DIR/bd-label-single-id.sh" sweep-digest --all \
  --zero-advisory "  No digest exists yet -- /sweep section 4's N==0 path owns this (bootstrap, or
  a clean no-op pass on an empty queue). Do not read or write a digest here." \
  --dup-advisory "  Duplicate digests -- /sweep section 4's N>1 anomaly path owns this. Do NOT guess
  which is authoritative and do NOT write. Report the ids and let a human
  consolidate (keep one, strip the sweep-digest label off the rest):"
