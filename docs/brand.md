# lode — Brand brief

The durable spec every other identity artifact conforms to (`lode-fhql.3`) — written before any
mark is drawn, so the marks have something to be right or wrong against. Everything below is a
maintainer decision recorded here as the source of truth; if a future artifact (the SVG mark, the
wordmark, the docs site) disagrees with this file, the artifact is wrong, not this file.

## 1. Positioning

> **lode** is an AI-first, TUI-first personal knowledge base for the things you learn during your
> day at work.

One sentence, from [README.md](../README.md). It names the three things that matter, in order:
who it's for (you, working), what it captures (things learned, not tasks or scheduling), and how
you touch it (a terminal, first — not a web app you have to open a browser tab for). Any
positioning copy written elsewhere (the docs-site landing page, a README badge, an OG card) is a
restatement of this sentence, not a replacement for it.

## 2. The name story

> The mother lode: a rich vein you accumulate and mine. An append-only log of notes + a derived
> knowledge graph — the ore and the assay.

This line already lives in README.md and is the strongest asset the project has. It is not a
pull-quote sitting beside the product — it is the metaphor the product's own architecture
literally implements, and every other identity artifact should be traceable back to it:

- **The ore** — the append-only log of captured notes. Raw, unrefined, valuable *because* nothing
  is thrown away. This is the write path: fast, dumb, synchronous.
- **The assay** — the derived knowledge graph, embeddings, and citations. What turns raw ore into
  something you can act on. This is the read path: async, intelligent, on demand.
- **The mine** — the TUI itself. The tool you show up to, day after day, to add to the vein and to
  draw out of it.

A vein of ore is found underground, in the dark, worked with simple tools by hand — not staged
under gallery lighting. That texture (terminal-native, unglamorous, cumulative) should drive the
palette and the mark (§3, §5), not a generic "mining" visual vocabulary (pickaxes, hard hats,
cave-mouths) that has nothing to do with note-taking. The metaphor is structural, not decorative.

**Naming note.** The `lode-fhql.1` name audit is closed. Its outcome is recorded in
`docs/decisions.md` ("2026-08-14 — DECIDED (`lode-fhql.1`/`lode-fhql.16`/`lode-w3wt`, maintainer
decision)"): the brand, repo name, CLI command and import package stay `lode`, and only the
distribution name became `lode-kb`, because an unrelated project already holds `lode` on PyPI. The
project does not publish to PyPI (see `docs/release.md`'s Non-goals), so that collision is inert for
publishing. Nothing in the brand needs to change for it.

## 3. Palette

Two constraints, in tension, both real:

1. lode is **TUI-first** — its primary UI runs inside whatever terminal and colour scheme the user
   already has, which the project does not control (§4 makes the same point about type). A palette
   stated only as hex values is unusable there.
2. The docs site, README badges, and any SVG mark render on the web, where hex is the only honest
   unit.

So the palette below is stated **both ways** for every colour: a hex anchor for the web, and a
designated terminal-safe counterpart for the TUI. The counterpart is a judgment call, not a
mechanical nearest-neighbour computation — `slate_blue3` happens to be the closest 256-colour entry
to `#5A4FCF`, but `dark_goldenrod` is deliberately *not* the closest to `#C08A3E` (that would be
`light_salmon3`, which reads pink rather than metallic). Where the two disagree, the named
256-colour value is what a terminal surface uses; do not "correct" it toward the hex.
"Terminal-safe" here means the **256-colour xterm
palette** (`\e[38;5;Nm`), not the 16-colour ANSI set — the lesson already learned and recorded in
`src/lode/tui/screens/_markdown_area.py` (`lode-lab1`, retuned `lode-dmbc`): ANSI indices 0–15 are
*remapped by the user's own terminal theme*, so a colour picked there can land anywhere (the
original note-body magenta, ANSI index 5, read as harsh in some themes with no way to soften it
without touching the user's config). The 256-colour range sits outside that remapping and renders
consistently across terminals without requiring truecolor.

| Role | Hex (web) | 256-colour name (TUI) | xterm index |
|---|---|---|---|
| **Primary — vein** | `#5A4FCF` | `slate_blue3` | 62 |
| **Accent — ore** | `#C08A3E` | `dark_goldenrod` | 136 |
| **Ink — text** | `#1E1B2E` | `grey11` | 234 |
| **Paper — background** | `#F7F4EE` | `grey93` | 255 |

- **Primary (`#5A4FCF` / `slate_blue3`)** is the existing Textual accent from the README's
  "Built with Textual" badge — already load-bearing before this brief existed, so it is the
  anchor, not a fresh pick. It reads as the vein: a deliberate, slightly unusual violet-blue that
  doesn't look like anyone else's dev-tool blue.
- **Accent (`#C08A3E` / `dark_goldenrod`)** is the ore itself — a muted, warm metallic gold, never
  the shiny "success" green or gamified yellow other tools reach for. Used sparingly, for the one
  or two things per screen that are literally the payoff of using lode (a citation, a confirmed
  save) — not for routine status.
- **Ink and paper** are the two ends of the light/dark axis, named so a light-mode and a dark-mode
  rendering of the same web asset (the docs site, an OG card) can be produced from one palette
  instead of guessing. **The TUI does not use ink/paper directly** — see the terminal-safe subset
  below.

### The terminal-safe subset — what the TUI is actually allowed to use

Only **primary** and **accent** are brand colours the TUI palette may use, and only as 256-colour
names, never hex, never ANSI 0–15. **Ink and paper are web-only** — the TUI never sets an explicit
foreground/background colour derived from this palette at all; its foreground and background come
from the Textual theme (dark by default; `[tui.theme]` selects another). The rule above binds new
brand-coloured surfaces only — neither existing styling surface is a precedent for it, and this
brief does not retroactively bind them. `lode.cli.CLI_STYLES` maps its semantic names (`note_id`,
`warn`, `danger`) onto *ANSI* colours — `cyan`, `yellow`, `bold red` — deliberately, because CLI
status colour *should* follow whatever the user's terminal theme makes of those names; only
`NOTE_BODY_THEME` moved into the 256-colour range (the lesson above), a syntax palette needing a
predictable intensity. The default note-body values were chosen for a **dark** background, which the
default theme supplies; a user who selects a light theme through `[tui.theme]` can adjust them
through `[tui.theme.syntax]` (both in `docs/configuration.md`). This brief does not introduce a
third palette mechanism; a future TUI surface that wants the brand's primary or accent reaches for
`slate_blue3` / `dark_goldenrod` by name, through whichever of the two existing mechanisms
(`CLI_STYLES` for CLI/rich, a `TextAreaTheme`-style dict for Textual widgets) fits the surface —
never a new one.

### What this palette is not

- Not a "brand blue" gradient, not a multi-stop scheme — two colours plus the two web-only
  neutrals is the whole palette. Nothing here needs a design-system's worth of tints and shades.
- Not a single user setting. The styling surfaces are configurable separately, through the
  `[tui.theme]` and `[cli.theme]` sections documented in `docs/configuration.md`. Neither is a
  "brand colour" setting. The `primary` and `accent` keys of `[tui.theme.colors]` are Textual's
  theme variables, which `lode.tcss` styles the chrome with, not this brief's primary and accent. No
  TUI or CLI surface uses `slate_blue3` or `dark_goldenrod` today. This brief states the *intended*
  brand colours; it does not claim they are configurable.

## 4. Typography

- **The TUI's type is the user's terminal font.** lode does not control it, does not assume
  monospace metrics beyond what a terminal already guarantees, and must never ship an assumption
  (a specific glyph, a specific line-height) that only holds for one font. This is not a gap in
  the brief — it is the same constraint README already documents about a TUI-first tool, made
  explicit here so no future artifact (an ASCII wordmark, a box-drawing mark) is drawn against a
  font it can't rely on. `docs/how-to/` and `docs/tui.md` are where terminal-rendering conventions
  actually live; this brief adds nothing to them beyond naming the constraint.
- **The docs site** (the one surface lode fully controls the rendering of) uses a two-typeface
  pairing:
  - **Body / UI text:** [Inter](https://rsms.me/inter/) — a widely-used, open, highly legible
    grotesque with a genuinely large weight range; free of licensing friction for a self-hosted
    GitHub Pages site (SIL Open Font License).
  - **Code / inline snippets / the wordmark, where one is rendered on the web:** a monospace stack
    anchored on [JetBrains Mono](https://www.jetbrains.com/lp/mono/) (also OFL), falling back to
    the system default monospace stack (`ui-monospace, SFMono-Regular, Menlo, Consolas,
    monospace`) so a visitor without the webfont still gets *a* monospace, not a serif substitute
    — because the site is documenting a monospace-native tool, code and prose should visibly
    differ.
  - No third display/heading face — Inter's weight range (headings can go semibold/bold) is
    enough; a dedicated display font would be one more asset to license, load, and keep legible in
    both themes for no material gain here.

## 5. Voice and tone

The docs already have a distinct register — this section names it explicitly so new writing (the
docs-site landing page, README copy, an eventual OG card's tagline) matches it on purpose rather
than by accident.

- **Direct.** Say the thing, then the reason, in that order. "lode does not control your terminal
  font" reads better than three sentences building up to the same point.
- **Evidence-first.** A claim about behaviour cites where that behaviour is decided or enforced
  (a doc section, a test, a specific mechanism) rather than asserting it needs to be believed.
  This brief itself follows the rule — see how often it points at a file instead of describing one
  in the abstract.
- **No marketing.** No superlatives ("blazing fast", "seamless"), no growth-hacking imperatives
  ("supercharge your notes"), no invented pain-point drama. lode's pitch is the mechanism, not the
  adjectives around it — the retrieval pipeline, the citation gate, the append-only log are
  themselves the argument for why this is worth using.
- **Comfortable with open questions.** `docs/decisions.md` exists and is linked, not buried,
  because the project treats "we haven't decided this yet" as a normal, statable fact rather than
  something to paper over. Brand copy should do the same.
- **Terse over exhaustive, but never at the cost of a citation.** Match the density of the rest of
  `docs/` — a reader who lands on any lode page should not be able to tell, from voice alone,
  whether they're reading the brand brief or the retrieval design doc.

## 6. Usage rules

These bind every future identity artifact (the SVG mark, the ASCII/Unicode wordmark, a favicon or
OG card) once drawn — this brief predates them, so the rules here are stated as constraints on
what gets built, not as a description of something that already exists.

- **Clear space.** The assembled lockup (mark + wordmark), or the mark on its own, needs clear
  space around it at least equal to the height of the mark's tallest element on every side, before
  any surrounding text or UI chrome. It does **not** govern the mark-to-wordmark gap inside the
  lockup — that spacing is fixed in the lockup asset itself (`lode-fhql.18`).
- **Minimum size.** The mark must stay legible (no illegible detail, no colour-fill collapsing to
  a smear) down to a 16×16 favicon rendering — this is a hard constraint on how much detail the
  mark is allowed to carry, decided here so the artist doesn't discover it after drawing something
  too intricate. `lode-fhql`'s own acceptance criteria already require a human sign-off ticket for
  every legibility check an agent cannot itself verify (16px rendering, GitHub light/dark) — this
  rule is what that sign-off checks against.
- **Colour.** Use the primary/accent pair from §3 (hex on the web, the 256-colour subset in a
  terminal context) — no ad hoc recolouring per surface. A monochrome (single-colour) rendering of
  the mark is allowed where the surface demands it (e.g. a favicon that must work in both GitHub's
  light and dark chrome) and should use **ink** in light contexts, and an unfilled/outline
  treatment or **paper** in dark contexts — never primary-on-primary or accent-on-accent.
- **Light/dark handling.** Any web asset (SVG mark, OG card) is produced in both a light-background
  and a dark-background variant from the start, per `lode-fhql`'s own acceptance criteria — there
  is no single "the mark" asset; there are always two, selected by the surface's theme.
- **What not to do.**
  - Don't stretch, skew, or rotate the mark.
  - Don't add a drop shadow, bevel, gradient fill, or any effect the mark wasn't drawn with.
  - Don't recolour the mark to match an unrelated surface's existing palette (e.g. tinting it to a
    third-party badge colour) — it stays primary/accent/ink/paper as specified in §3, or the
    monochrome treatment above.
  - Don't pair the mark with imagery from a generic "mining" visual vocabulary (pickaxes, hard
    hats, cave scenes, gold nuggets) — the metaphor lode draws on is structural (§2: the ore/assay
    split in the architecture), not an illustration of mining as an activity.
