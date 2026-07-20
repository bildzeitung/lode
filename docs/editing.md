# lode — markdown editing surface

How the note body became a keyboard-first markdown authoring surface (`lode-ev5j`): the decisions
behind it, the browser-safety guard on the open-link binding, and what was deliberately left
unbuilt — including the inline lint squiggles still carrying open questions of their own. See
[keybindings.md](keybindings.md) for the exact key and screen list of the open-link binding this
doc describes, and [tui.md](tui.md) for layout/footer conventions. Companion doc, reachable from
[design.md](design.md#map-of-the-docs).

## No preview pane

lode is a keyboard-first editing surface: markdown is coloured **in place**, inside the same
`TextArea` you type into, and never rendered to a side panel. There is no split view, no
render-to-HTML step, no "preview mode" toggle. This was decided up front and never
reconsidered — a preview pane is a second surface to keep in sync and a second thing to look at,
which cuts against capture staying instant (see [design.md](design.md#1-the-core-problem)).

## Live syntax colouring — block-level only, on four screens

The note body gets live markdown token colouring as you type or view it, via Textual's
`TextArea(language="markdown")` (`textual[syntax]`, tree-sitter-backed). This ships on **four**
`TextArea`s, routed through the one shared helper `src/lode/tui/screens/_markdown_area.py`:

| Screen | Widget id | Editable |
|---|---|---|
| `CaptureScreen` | `BODY_ID` | yes |
| `EditScreen` | `EDIT_BODY_ID` | yes |
| `VersionViewScreen` | `VERSION_BODY_ID` | no |
| `SnapshotViewerScreen` | `SNAPSHOT_VIEWER_BODY_ID` | no |

`ReconcileScreen` is deliberately excluded — it renders a diff, not markdown, and colouring would
fight the diff structure. `CaptureScreen` was not in `lode-ev5j.2`'s original three-screen scope;
it was added by the technical review as `lode-ngk2` once the gap was spotted (colouring a note on
edit but not on capture, the primary authoring surface, was an inconsistency a user would hit
immediately).

**Colour depth is block-level only, not inline** — a scope decision, not a bug: **emphasis,
strong, inline code spans, and inline links (`[text](url)`) are NOT coloured.** What *is*
coloured: headings and heading markers, fenced/indented code and fence
delimiters, list markers, block-quote markers, thematic breaks, backslash escapes, and
*reference-style* link definitions (`[label]: url` — not inline `[text](url)`).

**Why block-only.** `lode-ev5j.1`'s spike (verified against textual 8.2.8 + tree-sitter-markdown
0.5.1) found that `TextArea(language="markdown")` loads only Textual's bundled **block** grammar.
An inline construct like `[my link](https://example.com/path)` collapses to one opaque `inline`
node with no `link_text`/`link_destination`/`link` children — Textual's own bundled
`markdown.scm` produces zero `@link*` captures over it, because its `link_destination`/
`link_label` captures are block-grammar link-*reference-definition* nodes, not inline links.

The installed `tree_sitter_markdown` package *does* expose the separate inline grammar
(`inline_language()`) and even ships a ready-made `injections.scm`/`highlights.scm` pair — the
same convention Neovim/Helix/Zed use to splice block and inline grammars together. But Textual's
architecture has **zero support for tree-sitter injection queries**: one `Language`, one `Parser`,
one `Tree` per `TextArea`, no injection-processing code anywhere in the installed package.
Reaching inline colouring would mean hand-building an injection subsystem — parsing injection
queries, maintaining a child `Parser`/`Tree` per `(inline)` node, byte-offset-adjusting captures
into the parent's coordinate space, incrementally re-parsing on edits — and splicing the merged
captures into `TextArea`'s **private** `_build_highlight_map` path, since Textual's own
highlighter only ever queries the single outer tree. That's a multi-day build on unsupported
private API, the same fragility class flagged for lint squiggles below. Block-only colouring was
accepted instead; a custom `.scm` ships only if a future ticket explicitly justifies that cost.

**`textual[syntax]` is a hard dependency** (`pyproject.toml` `[project].dependencies`, not an
optional extra) — it pulls ~15 tree-sitter grammar packages (there is no markdown-only extra).
Dependencies remain **unpinned**, per the existing policy in `pyproject.toml`; no private Textual
API is used by the colouring feature's *source* (only its tests reach into `TextArea._highlights`,
deliberately, to avoid adding `pytest-textual-snapshot` as a dev dependency).

**Graceful fallback.** A broken or incomplete environment must not kill the screen. The shared
helper (`_markdown_text_area` in `_markdown_area.py`) catches **two** distinct exceptions from
`TextArea.__init__`, and both are required:

- `textual.widgets.text_area.LanguageDoesNotExist` — Textual's own signal that it could not
  resolve the grammar (package missing, or failing to load).
- `ValueError` — raised by `tree_sitter.Language()` itself when the grammar's compiled ABI and
  the installed `tree-sitter` core disagree. `get_language` does not catch this, so it propagates
  straight through `TextArea.__init__`. Because dependencies are deliberately left unpinned, an
  independently-resolved `tree-sitter`/`tree-sitter-markdown` pair is exactly how a real
  environment breaks — making this the *more* likely of the two failure modes, not an exotic one.

Either exception falls back to a plain, uncoloured `TextArea` with everything else (text,
read-only, id, placeholder) unchanged, so editing and scrolling keep working.

## Mouse-clickable links: conceded, in favour of a keyboard binding

Mouse-clickable links are **conceded** — lode has no clickable links in the note body — replaced
by a keyboard "open link under cursor" binding (below). Two independent reasons were considered;
record both, since this is exactly the kind of decision that gets re-litigated:

**(a) `@click` markup is never processed inside a `TextArea` buffer.** `@click` actions are a Rich
*console-markup* mechanism (`Text.from_markup`); `TextArea` composites `Segment`s manually and
never calls `Text.from_markup` anywhere in its render path. This is only processed in
`Static`/`Label` content. **This reason stands, unmodified, empirically confirmed by the
`lode-ev5j.1` spike.**

**(b) Style-based OSC-8 links — originally claimed inert, later found NOT to be, at the byte
level.** The epic's original reasoning also claimed that Textual's compositor never emits the
OSC-8 hyperlink escape sequence a raw Rich `Console` would, so injecting a per-link
`Style(link=url)` into `TextArea` content would render inert. **The spike found this claim wrong
as stated.** Injecting a highlight mapped to `Style(link=url)` (via a custom `TextAreaTheme`) into
a live `TextArea`'s private `_highlights`, then inspecting the actual bytes
`Compositor.render_segments` writes to the terminal, showed a correct OSC-8 span
(`\x1b]8;id=...;<url>\x1b\\`) — Textual's `Strip`/compositor pipeline *does* check `style._link`
and wraps it in OSC-8 on write.

This does **not** reopen the concession: (b) is what was falsified, and (a) alone still rules out
clickable links in a `TextArea`. The keyboard binding also works in every terminal, with no
assumption about OSC-8 support. **If mouse-clickable links are ever reconsidered, start from a
real-terminal OSC-8/click-handling test** — the spike ran with no PTY, so actual click-through
behaviour, and any interference from the app's own SGR mouse-tracking mode, remain untested — not
from the "provably inert" framing above.

## Keyboard "open link under cursor" (`Ctrl+N`)

`Ctrl+N` opens the URL under the cursor in the system browser — the replacement for
mouse-clickable links — on `EditScreen`, `VersionViewScreen`, and `SnapshotViewerScreen` (the
same key on all three; see [keybindings.md](keybindings.md) for the full binding table and the
letter-space accounting behind picking `Ctrl+N`). **`CaptureScreen` is not bound**, even though it
now colours markdown too (`lode-ngk2`). This is an **unclosed gap, not a decision**: `lode-ev5j.3`
inherited the same three screens `lode-ev5j.2` originally targeted, and that scope was never
revisited when `lode-ngk2` added a fourth colouring screen. `ctrl+n` is free on `CaptureScreen`
(retired by `lode-bsmc`), so there is no key conflict standing in the way — tracked as
`lode-vx60`, which will either bind it or record an explicit reason capture is excluded.

Extraction (`src/lode/tui/screens/_link_open.py:extract_link_at_cursor`) cannot read a link node
from the parse tree — the `lode-ev5j.1` spike confirmed inline links aren't reachable there (see
above) — so it regex-scans the cursor's own line instead, trying three shapes in order: an inline
link `[text](url)`, a reference-style link definition `[label]: url`, then a bare URL (matched via
`lode.drawdown.iter_url_spans`, the *same* matcher that decides which URLs a note body opens
external edges for, so `Ctrl+N` can never open a different URL than the one lode recorded as the
external for that character position).

**Browser invocation is guarded**, not a bare `webbrowser.open()` call: `resolve_link_open`
refuses to open when the browser controller that would actually run resolves to exactly
`webbrowser.GenericBrowser` (an exact `type()` check, not `isinstance` — `BackgroundBrowser`
subclasses it and is safe) or when no display is reachable (`$DISPLAY`/`$WAYLAND_DISPLAY` unset,
outside macOS). In every case — opened or refused — the URL is surfaced on the status line, so
it's always manually copyable; no silent no-op. The whole browser-resolution/open path runs on a
worker thread (`@work(thread=True)`), since resolving and invoking a browser controller has three
verified-blocking stdlib paths (an untimed `xdg-settings` subprocess call, a 5s cold-launch wait,
and blocking for the browser's full foreground lifetime) that must not freeze the Textual event
loop. Only the cheap cursor/line read stays on the event loop — reading it from a worker thread
would race the user's own typing.

## Inline lint squiggles — deferred, not built (see `lode-o7pf`)

Inline lint squiggles (coloured underlines on markdown-lint-flagged ranges, updated as you type)
were **deferred out of this epic entirely**, split into their own follow-on epic, `lode-o7pf`
(blocked on this epic's colouring landing). This was not a scope cut for its own sake — it removed
*all* private-Textual-API use from `lode-ev5j`'s critical path:

- The only styling channel for a lint squiggle is Textual's **private, unsupported**
  `_highlights` dict (`_highlights[line].append((start_col, end_col, highlight_name))`) — Textual's
  own docs warn this "is not a supported feature; it may change without notice." A standing
  upgrade-fragility cost this epic chose not to take on yet.
- The tree-sitter highlighter **owns** `_highlights` and clears it on every edit (verified:
  `_build_highlight_map` calls `highlights.clear()`, `textual` 8.2.8 `_text_area.py:827`). Lint
  ranges can't be appended once — they'd need re-injecting after every syntax re-highlight.
- An injected highlight **silently no-ops** unless a custom `TextAreaTheme` registers the
  highlight name in `syntax_styles` (`_text_area.py:1501` does `.get(name)` and skips on `None`).
- No linter was ever chosen (hand-rolled rules vs. `pymarkdownlnt` vs. something else) — that
  decision, and the resulting range-granularity question (most rules report a line + start column
  with no natural end column), is `lode-o7pf`'s to make before it builds anything.

## Squiggle form, decided in advance: straight underline, not wavy undercurl

Whenever inline lint squiggles do get built, they will be a **plain, coloured, straight
underline** — never a wavy "undercurl." A wavy squiggle needs the terminal control sequence
SGR `4:3` (undercurl), which is terminal-dependent (kitty/WezTerm/VTE-based terminals support it,
many others don't) and — decisively — **stock Rich cannot emit it at all**: `lode-ev5j.1`'s spike
confirmed `rich.style.Style(underline=True)` renders through a real `Console` as plain SGR `4`,
never `4:3`, and `Style.__init__`'s parameter list has no undercurl parameter — there is no code
path in stock Rich that could ever produce it. This is settled fact, not a future build-time
check.

## Rejected alternative: gutter marker + status-line message

A third option was **offered and not chosen**: an upgrade-safe gutter-marker plus
status-line-message affordance for lint feedback, using no private Textual API at all — a
column-edge marker character instead of an inline underline, with the actual lint message shown
on the status line rather than requiring hover/inspection of styled text. This avoids every
`_highlights`-fragility concern above entirely. The user chose true inline underlines instead.
Recording the tradeoff here so it isn't re-derived from scratch if the private-API cost of
`lode-o7pf` ever becomes a real problem.
