#!/bin/bash -u
#
# Retry wrapper for `bd dolt push`, hardening it against the two concurrent-writer
# failure modes documented in docs/decisions.md ("bd dolt push retry-on-reject",
# lode-83d) and validated in docs/agents-workflow.md ("Concurrent bd dolt push under
# fan-out", lode-nps.3):
#
#   1. Rejected push (non-fast-forward). `dolt push` to refs/dolt/data is
#      fast-forward-only + atomically CAS-protected on the branch ref, exactly like
#      `git push` — a losing concurrent writer is REJECTED, never silently dropped,
#      and the fix is mechanical: pull (merge the winner's commit in), then retry.
#   2. Embedded-mode lock contention. lode runs bd in embedded (in-process Dolt
#      engine) mode, and every worktree on a machine shares ONE physical Dolt store
#      (single-writer, enforced via file lock per beads' own docs). A concurrent
#      writer mid-operation produces a transient "database is locked" error that
#      clears as soon as that writer's single operation finishes.
#
# Both failure modes are transient and self-clear within a few seconds under
# lode's actual write pattern (disjoint per-ticket rows, short-lived locks) — a
# short backoff-and-retry loop is the correct-weight fix; switching bd to Dolt
# server mode is deliberately NOT done here (see docs/decisions.md).
#
# Usage: scripts/bd-dolt-push.sh [any `bd dolt push` flags, e.g. --remote foo]
# Env overrides (mainly for tests): BD_DOLT_PUSH_MAX_ATTEMPTS, BD_DOLT_PUSH_BASE_DELAY

MAX_ATTEMPTS="${BD_DOLT_PUSH_MAX_ATTEMPTS:-5}"
BASE_DELAY="${BD_DOLT_PUSH_BASE_DELAY:-2}"

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
  bd dolt push "$@"
  status=$?
  if [ "$status" -eq 0 ]; then
    exit 0
  fi

  if [ "$attempt" -eq "$MAX_ATTEMPTS" ]; then
    echo "bd-dolt-push: giving up after ${MAX_ATTEMPTS} attempts (exit ${status})" >&2
    exit "$status"
  fi

  # Exponential backoff + small jitter, so a herd of concurrent producers doesn't
  # retry in lockstep and re-collide.
  delay=$((BASE_DELAY * (1 << (attempt - 1))))
  jitter=$((RANDOM % BASE_DELAY + 1))
  sleep_for=$((delay + jitter))
  echo "bd-dolt-push: attempt ${attempt}/${MAX_ATTEMPTS} failed (exit ${status}) — pulling + retrying in ${sleep_for}s" >&2

  # Best-effort: fold in whatever the winner pushed so this attempt has a shot at
  # fast-forwarding. If the pull itself fails (e.g. the lock is still held), fall
  # through to the retry anyway — the next push attempt will surface the same
  # failure and keep backing off.
  bd dolt pull || true
  sleep "$sleep_for"
  attempt=$((attempt + 1))
done
