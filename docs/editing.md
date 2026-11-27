# lode — markdown editing surface

How the note body became a keyboard-first markdown authoring surface (`lode-ev5j`): the decisions
behind it, the browser-safety guard on the open-link binding, and what was deliberately left
unbuilt, plus the inline lint squiggles (designed in `lode-o7pf`, built as its children land). See
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

**The palette** (`NOTE_BODY_SYNTAX_STYLES` in `_markdown_area.py`, `lode-lab1`, retuned by
`lode-dmbc`). The bundled grammar's captures are the complete menu of what *can* be coloured; lode
maps four of them:

| Capture | Covers | Style |
|---|---|---|
| `text.literal` | every line of a fenced block (delimiters, info string, body), indented code blocks, reference-link titles | `plum3` |
| `punctuation.delimiter` | the ` ``` ` fence characters | `grey42` |
| `heading.marker` | `#`…`######` and setext underlines | `steel_blue3` |
| `list.marker` | bullet/ordered markers — **and thematic breaks**, which share the capture | `dark_sea_green4` |

Unmapped, and left that way on purpose: `heading` (the heading text), `link.uri`, `link.label`,
`punctuation.special` (block-quote markers, which the grammar emits as zero-width spans here),
`string.escape`, and `none`. `none` is the load-bearing one — `lode-76go` found it is emitted
*after* `text.literal` in each line's highlight iteration order, so mapping it would win the colour
attribute at render time and silently undo the code-block colour. The same ordering is used
deliberately in the other direction for `punctuation.delimiter`, which lands after `text.literal`
and so dims the fence characters on top of the whole-line code span.

**Colour only — no bold, no background tint** (maintainer decision), and **every value sits in the
256-colour range, never the standard 16.** Indices 0–15 are remapped by the user's terminal theme:
the original `text.literal` colour was `magenta` (index 5), which is why it rendered harsh and why
there was no way to soften it in place. The 256-colour names render consistently without requiring
truecolor, so hex — which would have hardcoded a dark-background assumption — was not needed.
Whether any of this should become user-configurable is open; see
[decisions.md](decisions.md).

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
private API. Block-only colouring was accepted instead; a custom `.scm` ships only if a future
ticket explicitly justifies that cost.

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
mouse-clickable links — on all **four** colouring screens: `EditScreen`, `VersionViewScreen`,
`SnapshotViewerScreen` (bound together by `lode-ev5j.3`), and `CaptureScreen` (bound separately by
`lode-5ill`, once `lode-ngk2` had made it the fourth colouring screen). The same key on all four;
see [keybindings.md](keybindings.md) for the full binding table and the letter-space accounting
behind picking `Ctrl+N`.

**This was a closed gap, not a permanent decision to exclude capture.** `lode-ev5j.3` inherited the
same three screens `lode-ev5j.2` originally targeted, and that scope was not revisited when
`lode-ngk2` added a fourth colouring screen — leaving `CaptureScreen` able to *show* a coloured
link with no way to *open* one. `ctrl+n` was free on `CaptureScreen` (retired by `lode-bsmc`), so
there was no key conflict standing in the way, and link extraction never depended on the colouring
(see below) — `lode-5ill` closed the gap by binding it identically to the other three screens.

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

<!-- The section's old heading anchor. An append-only decisions.md entry ("Markdown editing — open
items parked in editing.md") links to it and cannot be repointed. -->
<a id="inline-lint-squiggles--deferred-not-built-see-lode-o7pf"></a>
## Inline lint squiggles — get_line underlines (see `lode-o7pf`)

Inline lint squiggles are plain underlines on markdown-lint-flagged character ranges in the
**editable** note bodies, updated as you type. **This section is the settled design, not shipped
code:** the rule engine (`lode-o7pf.2`), the widget (`lode-o7pf.3`) and the wiring (`lode-o7pf.4`)
are not built, so nothing under `src/lode/` implements it yet. The Textual behaviour it relies on
is verified against `textual` 8.2.8 / `rich` 15.0.0.

The feature was first deferred out of `lode-ev5j` into its own epic, `lode-o7pf`. This design
replaces two earlier ones: injecting ranges into `TextArea`'s private `_highlights` map (unsupported
API, UTF-8 byte offsets, and re-injection after every edit, since the syntax highlighter clears the
map), and post-processing the `Strip` returned by a `render_line` override (which would redo the
cell-column, scroll, gutter and soft-wrap arithmetic Textual already does after `get_line`).

**Mechanism: override `TextArea.get_line`, a documented public hook.** Its docstring invites
stylizing the returned `Text` ("You can stylize the Text object returned here to apply additional
styling to TextArea content"). `_render_line` calls it first, then applies wrap sections, tab
expansion, horizontal-scroll crop and the gutter itself, so lint ranges are plain **character
indices** into the document line. There is no UTF-8 byte-offset, soft-wrap, scroll or gutter
arithmetic, and no private Textual API (`_highlights`, `_line_cache`, … are neither read nor
written). Selection, syntax highlights and the cursor are stylized after `get_line`, and a plain
underline survives all of them.

**Visual:** `Style(underline=True)` only — no colour, no background — so each range keeps its own
text and syntax colour. Rich has no underline colour (no SGR 58), and syntax styles applied after
`get_line` would override a lint foreground on headings and code anyway. No `TextAreaTheme` entry
and no palette key. See
[Squiggle form](#squiggle-form-decided-in-advance-straight-underline-not-wavy-undercurl) for why
it is straight, not undercurl.

**Scope: visible lines, lazily.** Lint runs inside `get_line`, which Textual calls only for lines
it is about to draw, so only visible lines are linted and the result is always current — no
`Changed` handler, timer, debounce or explicit cache invalidation. Every edit, undo and redo
already clears Textual's line cache (and Textual must keep that clear, since its cache key does not
include the text). Rules may read neighbouring lines. Two costs follow from where the hook sits:
Textual calls `get_line` once per **wrapped row**, not once per document line, and with soft wrap
on (the default, which `_markdown_text_area` keeps) every cursor move re-fetches every visible row,
not only an edit.

**Fence context.** Fence state comes from `lode.fence_parsing` (`fence_flags` /
`match_fence_marker`), the repo's one sanctioned CommonMark fence scanner. `fence_flags` is
whole-buffer O(n), so its result is memoized for **one render pass**: built on the first `get_line`
call of the pass and dropped when the pass ends (a `render_lines` override brackets a pass), so a
repaint served entirely from Textual's line cache, such as a cursor blink, does no scan. Lines
inside a fence and fence delimiter lines are exempt from every rule (`fence_flags` reports the
opener as outside, so the opener is checked with `match_fence_marker`).

**Rules (v1).** Hand-rolled pure functions over `(lines, index, fence context)` returning
character ranges `[start, end)` on that line; no new dependency (no `pymarkdownlnt`).

| ID | Flags | Underlined range |
|---|---|---|
| MD009 | trailing whitespace; exactly two trailing spaces is a markdown hard line break and is **not** flagged | the trailing whitespace run |
| MD010 | hard tab | each tab character |
| MD018 | no space after the ATX heading marker (`#Heading`) | the `#` run |
| MD019 | multiple spaces after the ATX heading marker | the extra spaces after the first |
| MD022 | ATX heading not preceded or followed by a blank line (first line of the buffer needs no blank before, the last none after) | the heading text |
| MD042 | empty link target (`[text]()`) | the whole link |

Excluded on purpose: MD012 and MD047 (nothing to underline — blank lines and a missing final
newline have no characters) and MD034 bare URL (would flag most work notes).

**Open, not yet ruled on (`lode-kgg0h`).** Four questions about the table were raised when this
section was reviewed. The table above is the design until they are decided:

- **MD009 underlines the space just typed.** One trailing space is flagged, two are not, three
  are, so the underline comes and goes at every word boundary typed at the end of a line. Checked
  against `textual` 8.2.8 with a throwaway copy of the table row; no rule knows where the cursor is.
- **MD018 flags lines that are not headings**, such as `#1234 fixed the retry` or `#eng-oncall` —
  the reason MD034 is excluded. From the table's wording; not checked against code.
- **Only fenced code is exempt.** An indented code block, an inline code span and a fence inside a
  block quote are linted as prose: `fence_flags` reports none of them as inside a fence.
- **MD022 on a heading with no text** (a bare `##`) has nothing to underline. From the table's
  wording; not checked against code.

**Screen set.** `EditScreen` and `CaptureScreen` bodies only; the read-only `VersionViewScreen`
and `SnapshotViewerScreen` bodies stay clean. Wiring: a `TextArea` subclass in its **own module**
([one-widget-per-module fiat](conventions.md#textual-one-screen-or-custom-widget-per-module)),
opted in through `_markdown_text_area`. It works on the grammar-less fallback `TextArea` too:
neither `get_line` nor the line-cache clear depends on a grammar.

**Testing.** Pure per-rule unit tests (including non-ASCII lines, a code fence and the two-space
exemption); assertions on the spans of the `Text` that `get_line` returns, with no
`pytest-textual-snapshot`; and a perf cap.

## Squiggle form, decided in advance: straight underline, not wavy undercurl

Inline lint squiggles are a **plain, straight underline in the text's own colour** — never a wavy
"undercurl." A wavy squiggle needs the terminal control sequence SGR `4:3` (undercurl), which is
terminal-dependent (kitty/WezTerm/VTE-based terminals support it,
many others don't) and — decisively — **stock Rich cannot emit it at all**: `lode-ev5j.1`'s spike
confirmed `rich.style.Style(underline=True)` renders through a real `Console` as plain SGR `4`,
never `4:3`, and `Style.__init__`'s parameter list has no undercurl parameter — there is no code
path in stock Rich that could ever produce it. This is settled fact, not a future build-time
check.

## Rejected alternative: gutter marker + status-line message

A third option was **offered and not chosen**: an upgrade-safe gutter-marker plus
status-line-message affordance for lint feedback, using no private Textual API at all — a
column-edge marker character instead of an inline underline, with the actual lint message shown
on the status line rather than requiring hover/inspection of styled text. This avoids private-API
fragility entirely. The user chose true inline underlines instead, and the `get_line` design above
gets them with no private API either. Recorded so the tradeoff isn't re-derived from scratch.
