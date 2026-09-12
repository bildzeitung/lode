#!/usr/bin/env bash
#
# The docs-nits collector's two identifying literals, sourced by every script
# in the family (scripts/bd-docs-nit.sh, scripts/bd-docs-nit-create.sh).
#
# Single-sourced rather than spelled once per script because the two are
# compared for EQUALITY at runtime: bd-docs-nit.sh's convergence path only
# fires when every open collector's title equals STANDARD_TITLE, which is the
# title bd-docs-nit-create.sh writes. Two copies plus a rename that touches
# one turns every convergence into a permanent exit-1 "a human opened one on
# purpose" refusal -- nits silently stop landing, and the failure reads as a
# human-ambiguity report rather than a typo.
#
# Sourced only; it defines variables and runs nothing -- which is why both
# assignments carry an SC2034 disable: shellcheck sees no local reader.

# shellcheck disable=SC2034
readonly DOCS_NITS_LABEL="docs-nits"
# shellcheck disable=SC2034
readonly STANDARD_TITLE="Docs wording nits: batch fix in the next docs pass"
