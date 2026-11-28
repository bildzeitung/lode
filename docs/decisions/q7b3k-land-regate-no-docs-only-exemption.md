---
id: q7b3k
date: 2026-11-28
status: decided
supersedes: []
related: []
---

## /land's post-merge re-gate has no docs-only exemption (decided, lode-brbhf)

Maintainer ruling on `lode-6lw71`, 2026-11-28, option A. `/land` Section 3 used to skip nox for a
merge set that touched only docs, running `scripts/validate-mermaid.sh` alone. That was the
Python-only reasoning `lode-31kju` already ruled out for direct merges to `trunk`
(`CLAUDE.md`: the pre-merge gates run whatever file types the merge touches). The corpus-scan
gates under `tests/` cover shell scripts and markdown, so a docs-only merge can turn them red.

The ruling: every merge set that merged anything gets the full re-gate command (`nox -t fix`,
`nox -s tests`, `nox -t everything-else`, `nox -s lock_currency`), whatever file types it touches.
`scripts/validate-mermaid.sh` still runs when a merged diff touches a `docs/` diagram. The
empty-merge-set skip (`lode-0jan`) stays: with nothing merged there is nothing new to certify.

`code-reviewer.md` step 3 said a docs-only branch has no Python gate, which contradicted its
step 5 (all three buckets); it now says a docs-only branch gets the same gates. Other instruction
files (`.claude/skills`, `.claude/agents`, `docs/agents-workflow.md`) were grepped for a
docs-only gate exemption with the same reasoning, and none was found:
`coding.md` step 7 already runs the builder gates for a docs/shell-only change.
