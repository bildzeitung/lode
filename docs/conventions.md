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

Every option and argument uses the `Annotated` form — `x: Annotated[Path | None, typer.Option(...)]
= None` — never a bare `typer.Option(...)`/`typer.Argument(...)` in the default-argument position.
This holds for *every* parameter, including the `bool`/`str` ones ruff's B008 does not flag; the
lint's own blind spot is what made the file inconsistent in the first place
([`stack.md`](stack.md#ruffs-lint-rule-set-settled-lode-cs5u), `lode-up58`).

## Typer: user-facing help via `help=`, rationale in the docstring

Every `@app.command()` that carries more than a one-line docstring passes its user-facing text as
`@app.command(help="...")`. The docstring stays as the maintainer's design record; `help=` is what
`--help` prints. **The docstring is left VERBATIM** — `help=` fully owns the rendered surface, so no
docstring is ever edited, not even to strip markup or a bd id from its first line (nothing renders
it). Every conforming diff is purely additive: one `help=` argument, zero docstring bytes touched.

The `help=` text:

- Is **extremely concise** and answers "when do I run this?" — a reader deciding whether to run the
  command should get there in a few lines, not a few screens. Name the signal that should send
  someone here (e.g. `lode status`'s "Action needed" line) where one exists.
- Is **hard-bounded, test-enforced length**: at most 12 rendered lines at `COLUMNS=80` for a command
  `help=`, at most 3 rendered lines at `COLUMNS=80` for an option/argument `help=`. The gate is a
  pytest corpus scan (`lode-ii25.9`) with a human-vetted allowlist escape hatch — each exemption
  entry in the gate test carries a reason.
- Has a **first line that stands alone** as a plain one-line sentence: Typer derives the
  `lode --help` command-list summary (`short_help`) from it, so every other command's list entry
  depends on this line making sense in isolation.
- Uses **`\n\n` for paragraph breaks only, never a single `\n`** — Rich does not rewrap a single
  newline inside a paragraph, so a `help=` string hard-wrapped at source-line boundaries renders
  ragged at other terminal widths.
- Contains **no Sphinx roles, RST backticks, bd issue ids, or test names**. It may cite a `docs/`
  page as a footnote, but the reader must be able to decide whether to run the command without
  opening it — a pointer that is load-bearing for the decision is a bug.

## Derive identifiers, never retype them

A long opaque identifier — a full git SHA, a bd issue id, a `.claude/worktrees/` hash — is never
hand-typed from a shorter prefix or from memory. Always derive it mechanically: `$(git rev-parse
<ref>)` for a commit SHA, `bd show <id>` for a bd id, the actual path on disk for a worktree hash.

A `PreToolUse(Bash)` hook backstops this for 40-hex git SHAs only; the fiat covers every opaque
identifier. Why this needs a mechanism rather than an instruction, and what the hook does and does
not catch:
[`docs/agents-workflow.md`](agents-workflow.md#guard-against-fabricated-shas-lode-fpmi) (lode-fpmi).
