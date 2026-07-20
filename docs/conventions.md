# lode — coding conventions

Prescriptive style **fiats** — the "because the maintainer said so" rules for writing code in this
repo. Unlike the design docs elsewhere under [`docs/`](design.md), the rules here carry **no
independent rationale** and don't need one to bind: they are a maintainer's call, not a reasoned
consequence of the architecture. The litmus that keeps this file honest — if a rule *does* earn a
"why anyone would reach on their own," it has stopped being a fiat and belongs in the relevant
design doc, not here.

This file is `@import`ed by [`CLAUDE.md`](../CLAUDE.md), so its contents are mechanically inlined
into the main session **and** every non-fork subagent — the `coding` producer, the `code-reviewer`,
the `land-review` agent, and the `land` session that dispatches it. One source; the write-side and
the review-side read the same bytes, so a fiat here cannot drift between who writes the code and who
gates it. Keep it tight — one short section per rule.

## Textual: one Screen or custom Widget per module

Every `Screen` subclass and every custom `Widget` subclass lives in **its own module file** — one
top-level UI class per `.py`.

Does **not** apply to inline helper widgets: a small, one-off `Widget`/`Static` subclass defined
next to its sole caller and used nowhere else may stay in that caller's module. The line is reuse
and standing — a screen, or a widget that any other module imports, gets its own file; a private
helper that exists only to serve one parent does not.

## Python: Typer, never argparse

Every Python CLI in this repo is built with **Typer**. Never `argparse`.

## Derive identifiers, never retype them

A long opaque identifier — a full git SHA, a bd issue id, a `.claude/worktrees/` hash — is never
hand-typed from a shorter prefix or from memory. Always derive it mechanically: `$(git rev-parse
<ref>)` for a commit SHA, `bd show <id>` output for a bd id, the actual worktree path on disk for a
hash. Holding a short form (a 7-char abbreviation, a fragment recalled from earlier context) and
typing out the rest by hand is exactly the gap where an LLM confabulates a plausible-looking but
fabricated tail — the invented characters are as fluent as the real ones, so the mistake is not
self-detectable by re-reading what was typed (lode-fpmi). A `git cat-file -e`-backed PreToolUse hook
denies an unrecognized 40-hex string in a `bd`/`git` command as a backstop, but the fiat is the
first line of defense: never retype what can be derived.
