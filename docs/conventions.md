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

Every registered command that carries more than a one-line docstring passes its user-facing text as
`help=` on its own `.command(...)` decorator (sub-apps included). The docstring stays as the
maintainer's design record; `help=` is what `--help` prints. **The docstring is left VERBATIM** —
never edited, not even to strip markup or a bd id from its first line. Every conforming diff is
purely additive: one `help=` argument, zero docstring bytes touched.

The `help=` text:

- Answers **"when do I run this?"** — name the signal that should send someone here (e.g. `lode
  status`'s "Action needed" line) where one exists.
- Is **hard-bounded, test-enforced length** — rendered at `COLUMNS=80`: at most 12 lines for a
  command `help=`, at most 3 for an option/argument `help=`. The gate is a pytest corpus scan
  (`lode-ii25.9`) with a human-vetted allowlist escape hatch — each exemption entry in the gate test
  carries a reason.
- Has a **first line that stands alone** as a plain one-line sentence — Typer derives the
  `lode --help` list summary (`short_help`) from it.
- Uses **`\n\n` for paragraph breaks only, never a single `\n`** (Rich does not rewrap inside a
  paragraph, so source-line wrapping renders ragged at other widths).
- Contains **no Sphinx roles, RST backticks, bd issue ids, or test names**. It may cite a `docs/`
  page as a footnote, never as something load-bearing for the decision to run the command.

## Multi-exception `except`: parenthesized only

Always write a multi-exception handler as `except (A, B):` — never the unparenthesized `except A,
B:` form PEP 758 re-legalized in Python 3.14.

A multi-exception `except` **without** an `as` binding carries a trailing `# fmt: skip`, because
`ruff format` would otherwise strip the parentheses back to the bare form on every `nox -t fix`.
The `as` form (`except (A, B) as exc:`) needs no marker — PEP 758 keeps the parentheses mandatory
there, so the formatter leaves it alone.

This **reverses** the earlier tree-wide adoption of the bare form; the why, and the rejected
alternatives, live in
[`configuration.md`](configuration.md#python-style-multi-exception-except-must-be-parenthesized-lode-buay).

A pytest corpus scan (`tests/test_except_parens_gate.py`) gates this fiat mechanically — it fails on
any tracked-source bare or unmarked multi-exception `except`, so a dropped `# fmt: skip` marker turns
`nox -s tests` red instead of degrading silently.

## Derive identifiers, never retype them

A long opaque identifier — a full git SHA, a bd issue id, a `.claude/worktrees/` hash — is never
hand-typed from a shorter prefix or from memory. Always derive it mechanically: `$(git rev-parse
<ref>)` for a commit SHA, `bd show <id>` for a bd id, the actual path on disk for a worktree hash.

A `PreToolUse(Bash)` hook backstops this for 40-hex git SHAs only; the fiat covers every opaque
identifier. Why this needs a mechanism rather than an instruction, and what the hook does and does
not catch:
[`docs/agents-workflow.md`](agents-workflow.md#guard-against-fabricated-shas-lode-fpmi) (lode-fpmi).
