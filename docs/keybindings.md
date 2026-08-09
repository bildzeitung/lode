# lode — TUI keybindings

One page: which keys are taken, at which altitude, and the hard rule that keeps the next TUI
ticket from colliding with this one. **Consult this doc before adding or rebinding a key.**
Settled here; not duplicated in `docs/decisions.md` (that's for what's still *open*).

## Two altitudes

- **App-level** (`LodeApp.BINDINGS`, `src/lode/tui/app.py`) — reachable from *every* screen, unless
  a Screen-level binding on the *currently active* screen claims the same key (see below).
- **Screen-level** (a `Screen`'s or `ModalScreen`'s own `BINDINGS`) — active only while that
  screen (or a widget it composes, e.g. a focused panel) is on top.

**Screen-level shadows App-level on the same key.** Textual resolves a keypress by walking
outward from the currently focused widget to the active screen and only then to the app, taking
the first binding it finds. So if an App-level and a Screen-level binding share a key, the
App-level one is simply unreachable *whenever that screen is active* — it still fires from every
other screen. This is exactly what bit `lode-olmi.6`: `CaptureScreen` is the app's default screen,
and once `lode-olmi.9` landed a Screen-level `f4` on `CaptureScreen`/`EditScreen` ("focus related"),
an App-level `f4` for the Tags screen became unreachable from the screen a user is on by default,
even though the two features never look at the same code. **When claiming a new App-level key,
check every screen's own `BINDINGS` for the same key too — not just `LodeApp`'s.** (This example is
historical — as of `lode-juz8.1` the two live bindings involved read `ctrl+f`/`ctrl+t`, not
`f4`/`f5` — but the shadow mechanics it illustrates are unchanged and apply identically to any
`ctrl+` combo.)

## No function keys (`lode-juz8.1`)

**lode's TUI binds no function keys anywhere — every action key is a `ctrl+<letter>` combo (or a
named key like `escape`/`tab`/`up`/`down`/`enter`).** Before this ticket, three App-level actions
(`f2`/`f3`/`f5`) and one Screen-level action (`f4`, shared by `CaptureScreen`/`EditScreen`) used
function keys — a real ergonomic cost worth closing (many laptops chord Fn+number to reach one at
all). The policy going forward: **a new binding is never a bare function key**, full stop — not
just on an editable-`TextArea` screen (the hard rule below covers that narrower case already),
everywhere in the TUI. Reach for an unclaimed `ctrl+<letter>` and run it through the trap
checklist below before landing it.

**The letter space is nearly exhausted — check it, don't guess.** 26 letters, minus 11 already
claimed by `TextArea`'s own builtin bindings (`ctrl+a/e/w/d/x/c/v/u/k/z/y` — cursor/line-start/end,
cut/copy/paste, delete-word, undo/redo, …), minus the five letters already in live App/Screen use
(`ctrl+g/h/q/r/s` — `ctrl+n` freed by `lode-bsmc`'s Ctrl+S consolidation, back in the candidate
pool below), minus two `KEY_ALIASES` traps (`ctrl+i` → `tab`, `ctrl+m` → `enter`) and one
`App`-level `priority=True` reservation (`ctrl+p`, the command palette) — leaves exactly **`b`,
`f`, `j`, `l`, `n`, `o`, `t`** as formally-checked-safe candidates. Of those, `l` and `o` are
conventional terminal-driver control characters (`ctrl+l` clear/redraw, `ctrl+o` — termios
`VDISCARD` — flush) and `ctrl+j` is the raw LF byte some terminals treat as Enter-adjacent; but none
of the three is *aliased away* to a different key the way the two `KEY_ALIASES` traps above are —
each still reaches a binding under its own `ctrl+<letter>` name (verified against the installed
sequence tables: the LF byte parses to `ctrl+j`, `\x0f` to `ctrl+o`, and `ctrl+l` is not remapped at
all). `ctrl+j` *does* appear in `textual.keys.KEY_ALIASES` (which holds five entries, not just
`escape`), but on the *canonical* side — `ctrl+j` → alias `newline` — so it adds a name rather than
diverting the binding the way `ctrl+i` → `tab` / `ctrl+m` → `enter` do. What Textual's source
*cannot* rule out is a given terminal emulator's own handling of a control byte, so treat them as a
*last resort*, not a first pick. This ticket claimed the two
cleanest mnemonic fits first — `ctrl+b` (Browse), `ctrl+t` (Tags) — plus `ctrl+f` (Focus, for the
related-notes panel) before reaching for one risk-flagged letter, `ctrl+o` ("Options", for Config).
`l` and `j` are left unclaimed for the next ticket that needs one, checked against this same
checklist rather than assumed safe.

## The hard rule: editable-TextArea screens use non-printable keys

A screen that composes an **editable** `TextArea` (`read_only` not set, or explicitly `False`) —
today `EditScreen` (`src/lode/tui/screens/edit.py`) and `CaptureScreen`
(`src/lode/tui/screens/capture.py`) — must bind new actions to a **non-printable** key: a
`ctrl+<letter>` combo or a named key like `escape`/`tab`. **Never a bare printable
character** (a letter, digit, or punctuation key without a modifier), and — per the "No function
keys" policy above — never a function key either; `ctrl+<letter>` is effectively the only option
left.

**Why:** Textual's `TextArea._on_key` intercepts every `is_printable` keypress and calls
`event.stop()` + `prevent_default()` to insert it as a literal character, *before* the event can
ever bubble up to a Screen-level (non-priority) `Binding`. A bare `Binding("h", ...)` on
`EditScreen` does not open anything — it turns "hello" into "hhello" while typing. This was proven
empirically during `lode-olmi.2`'s build (5 tests failed reproducing exactly this) and is why that
ticket's `h`-opens-history binding was amended to `Ctrl+H`.

The two structurally possible workarounds and why one is rejected:

- `priority=True` on the Screen binding would reach the handler, but a priority binding intercepts
  *every* press of that key globally while the screen is active — the user could never again type
  that literal character into the note body. **Rejected** for any binding meant to coexist with
  typing.
- A non-printable key (`ctrl+<letter>`, `escape`) survives `TextArea`'s
  `is_printable` filter untouched and reaches the Screen binding normally. **This is the rule** —
  narrowed to exclude function keys by the "No function keys" policy above.

**Project practice: Ctrl-prefixed keys are standard for `EditScreen` actions.** Every action this
screen binds beyond `escape` (`ctrl+s`, `ctrl+h`, `ctrl+g`, …) uses a `ctrl+` combo, precisely
because its default-focused body `TextArea` swallows a bare printable key before a Screen binding
ever fires. When adding a new `EditScreen` (or `CaptureScreen`) action, reach for an unclaimed
`ctrl+<letter>` first — don't reintroduce a bare-letter binding "because it reads better," and check
this doc's table plus the two traps below before picking one.

**Not every non-printable key is actually safe — two more traps beyond bare-printable
(`lode-g5es`).** Picking `EditScreen`'s enrichment-inspector binding ran into both, in order:

1. **Textual's own `KEY_ALIASES` collide with some `ctrl+<letter>` combos.** `ctrl+i` looks like
   the obvious choice for an "Inspect" binding (mirroring `BrowseScreen`'s bare `i`), but Textual's
   `textual.keys.KEY_ALIASES` maps `ctrl+i` to `tab` (`"tab": ["ctrl+i"]` — an artifact of the ASCII
   control-code encoding, where Ctrl+I *is* the Tab character) — so a `ctrl+i` binding is
   indistinguishable from `tab`, a non-printable navigation key the editor never routes to a
   Screen-level inspector binding (this body `TextArea` uses the default `tab_behavior="focus"`, so
   `tab` does *not* insert an indent — confirmed empirically: pressing `ctrl+i` with the body focused
   neither opened the inspector nor changed the body). A `Binding("ctrl+i", ...)` here would be
   silently unreachable, the same failure mode as a bare printable key, just one layer further down.
   `ctrl+m` has the same problem (aliased to `enter`).
2. **Some `ctrl+<letter>` combos are reserved App-level with `priority=True`, unconditionally
   beating every Screen binding.** `ctrl+p` looked like the next-best pick ("peek," matching the
   inspector modal's own glance-and-dismiss contract) — but Textual's `App` registers
   `COMMAND_PALETTE_BINDING = "ctrl+p"` for `action_command_palette` with `priority=True`
   (`textual/app.py`, unconditional whenever `ENABLE_COMMAND_PALETTE` is set, which `LodeApp` never
   overrides). A priority binding is checked *before* Screen-level resolution even walks outward
   (the same mechanism that makes the app's own `ctrl+q` win everywhere), so `Binding("ctrl+p", ...)`
   on `EditScreen` was confirmed empirically to open Textual's `CommandPalette`, never the inspector
   — not a Screen-shadows-App case (that direction is a non-priority App key losing to a Screen
   key, see above), the reverse: an App key that *cannot lose*.

**Before binding a new `ctrl+<letter>` on an editable-`TextArea` screen, check three things: (a)
`TextArea.BINDINGS` for a builtin action already on that combo, (b) `textual.keys.KEY_ALIASES` for
an alias collision, and (c) `App.BINDINGS` (plus any `priority=True` binding `App` itself registers,
like the command palette) for a global reservation that would always win.** `lode-g5es` landed on
`ctrl+g` ("glance") for the enrichment-inspector action after `i` failed the printable-key rule,
`ctrl+i` failed the alias check, and `ctrl+p` failed the priority-reservation check.

**A related trap, not just the printable/non-printable split:** `TextArea` also consumes `up`,
`down`, `enter`, and `tab` for its own cursor movement / newline / indent while it holds focus, even
though those aren't "printable" characters. A Screen-level binding on one of those keys is
unreachable while the `TextArea` itself is focused — it only fires once focus has moved to a
*different* widget on the same screen. `lode-olmi.9`'s `RelatedNotesPanel` uses exactly this: it
binds its own `up`/`down`/`enter` for stepping through related notes, but those only apply once the
panel itself holds focus (moved there via the Screen-level `ctrl+f` → `focus_related`), not while
the note body `TextArea` still has focus.

Screens whose `TextArea` is `read_only=True` — `VersionViewScreen`, `SnapshotViewerScreen`,
`ReconcileScreen`'s diff view — are unaffected; `read_only` still lets bare-letter Screen bindings
through normally (see `SnapshotViewerScreen`'s bare `t` for toggling raw HTML — a read-only-body
screen — or `BrowseScreen`'s `i`/`d`, none of which touch an editable body).

**A trap on the action side, not the key side — pop the screen with `app.pop_screen`, never bare
`pop_screen`.** A Screen-level `Binding("escape", "app.pop_screen", "Back")` must name the pop action
**app-namespaced**. Textual resolves an unqualified action string against the *current* namespace,
which for a Screen binding is the Screen itself; no `Screen` subclass defines `pop_screen` (it lives
on `App`), so the bare `"pop_screen"` form silently does nothing while looking correct. Verified
against Textual 8.2.8. This is why every "Escape → Back" binding in the TUI —
`ConfigScreen`, `TagsScreen`, `AskScreen`, the browse-family view/picker/modal screens
(`VersionHistoryScreen`/`VersionViewScreen`/`ExternalPickerScreen`/`SnapshotViewerScreen`/`EnrichmentModalScreen`),
and `RelatedNoteModalScreen` — uses `"app.pop_screen"`. The sole exception is
`BrowseScreen.action_dismiss_screen`, which keeps a hand-rolled action (it closes an open search box
first, then pops); its binding names the bare `"dismiss_screen"`, which correctly resolves to that
Screen's own method. Discovered on `lode-11io`, then collapsed from ~13 hand-rolled one-line
`action_show_*`/`action_dismiss_screen` wrappers onto Textual's builtin action strings across the
tree by `lode-pijc`.

## Current keymap

### App-level (`LodeApp`, `src/lode/tui/app.py`)

| Key | Action | Notes |
|---|---|---|
| `ctrl+q` | Quit | `priority=True` — always wins, even over a Screen binding. Hidden from the footer (`show=False`, `lode-2bt3.2`) to make room for the new Help entry below — the binding itself is unchanged and fully live; see "The keybinding help overlay" section for why |
| `ctrl+o` | Show Config | "Options" — rekeyed off the function key `f2` by `lode-juz8.1` |
| `ctrl+b` | Show Browse | "Browse" — rekeyed off the function key `f3` by `lode-juz8.1` |
| `ctrl+t` | Show Tags | "Tags" — rekeyed off the function key `f5` by `lode-juz8.1` (itself a land-time rekey off `f4` — see the history below) |
| `ctrl+l` | Show Ask | Claimed by `lode-11io` — the mnemonic `ctrl+a` is NOT available (a `TextArea`/`Input` builtin, cursor-to-line-start); confirmed against all three traps below and against every screen's own `BINDINGS` |
| `ctrl+underscore` | Show keybinding help overlay | Claimed by `lode-2bt3.2` — see "The keybinding help overlay" section below |
| `?` (hidden, `show=False`) | Show keybinding help overlay | Same action as `ctrl+underscore`; convenience-only, reachable wherever no `TextArea`/`Input` holds focus (freed by `lode-2bt3.1`) |

No App-level function keys remain — see the "No function keys" policy above. `ctrl+l` is now
claimed (above); `ctrl+n` is now claimed too, at the **Screen** level — `EditScreen`,
`VersionViewScreen`, `SnapshotViewerScreen` (`lode-ev5j.3`), and `CaptureScreen` (`lode-5ill`,
below) all share the same open-link-under-cursor binding. **`ctrl+j` is now claimed too** — at the
**Screen** level, by `AskScreen` (`lode-35nu.4`, below), for "open the focused citation's cited
version/snapshot". **No formally-safe letter is left** for the next ticket that needs a *new*
App-level (or Screen-level) key — see the "No function keys" section's letter-space accounting
above for how to widen the pool (a fresh `ctrl+<letter>` audit, or reclaiming one already spent on
a feature that no longer needs it) before landing one.

**`lode-35nu.11.3` needed a per-note ask entry point and spent no new letter at all.** Rather than
widen the pool, it reused the existing App-level `ctrl+l` ("Ask") via the documented
screen-shadows-app mechanism (above): `EditScreen` and `VersionViewScreen` each declare their own
Screen-level `Binding("ctrl+l", "ask_about_note", "Ask")` — same key, same footer label, so nothing
about the footer or the letter ledger changes — that simply resolves *first* while one of those
screens is active and opens the note-scoped ask flow (the current note pinned as primary context)
instead of the corpus-wide one. **The accepted cost of that shadow** (the `lode-olmi.6` trap
above, in its mildest form): while one of those two screens is active, the *corpus-wide* Ask is no
longer reachable in one key — Escape back to Browse first. Judged acceptable because both keys are
the same action, differing only in scope, and "Ask" pressed from inside a note most plausibly means
"ask about this note". `AskScreen` itself gained an optional `note_id` constructor
parameter rather than becoming a second screen module — the zero-arg form (`SCREENS["ask"]`'s
App-level push) is the unchanged corpus-wide behaviour. A future ticket needing a genuinely *new*
action on one of these screens still faces the exhausted pool above; this one didn't need to.

### Screen-level

| Screen | File | Key | Action | Body TextArea |
|---|---|---|---|---|
| `VersionHistoryScreen` | `screens/version_history.py` | `escape` | Back | — |
| `VersionViewScreen` | `screens/version_view.py` | `escape` | Back | read-only |
| | | `ctrl+n` | Open link under cursor | |
| | | `ctrl+l` | Ask about this note (`lode-35nu.11.3`, shadows the App-level `ctrl+l`) | |
| `EnrichmentModalScreen` | `screens/enrichment_modal.py` | `escape` | Back | — |
| `DeleteConfirmScreen` | `screens/delete_confirm.py` | `y` | Yes, delete | — |
| | | `n` | No, cancel | |
| | | `escape` | Cancel (`show=False`) | |
| `BrowseScreen` | `screens/browse.py` | `escape` | Back | — (DataTable) |
| | | `i` | Inspect | |
| | | `v` | View retrieved content | |
| | | `d` | Delete | |
| | | `x` | Expand/collapse summary (hidden from the footer, `show=False`, `lode-2bt3.3`) | |
| | | `slash` | Search (restarts from the top each keystroke, `lode-2bt3.1`) | search direction retired (`lode-2bt3.1`) — `?` was unbound on every screen until `lode-2bt3.2` claimed it App-level for the help overlay |
| | | `s` | Quick search (BM25, narrows the list, `lode-35nu.6`) | distinct from `slash`'s scan-and-highlight |
| `ExternalPickerScreen` | `screens/external_picker.py` | `escape` | Back | — (DataTable) |
| `TagsScreen` | `screens/tags.py` | `escape` | Back | — (DataTable grid, `lode-l38d.9`) |
| | | `space` | Toggle tag (`show=False`) | hand-rolled multi-select; `enter` (DataTable's own native binding) does the same. Hidden to match the `SelectionList` it replaced, whose own `space` binding was `show=False` too |
| `SnapshotViewerScreen` | `screens/snapshot_viewer.py` | `escape` | Back | read-only |
| | | `t` | Toggle raw HTML | |
| | | `ctrl+n` | Open link under cursor | |
| `EditScreen` | `screens/edit.py` | `ctrl+s` | Save | **editable** |
| | | `escape` | Back (cancel) | |
| | | `ctrl+f` | Focus related-notes panel | |
| | | `ctrl+h` | Show version history | |
| | | `ctrl+g` | Inspect (enrichment modal) | |
| | | `ctrl+r` | View retrieved content (hidden from the footer, `show=False`, `lode-2bt3.3`) | |
| | | `ctrl+n` | Open link under cursor (hidden from the footer, `show=False`, `lode-2bt3.3`) | |
| | | `ctrl+l` | Ask about this note (`lode-35nu.11.3`, shadows the App-level `ctrl+l`) | |
| `DiscardConfirmScreen` | `screens/discard_confirm.py` | `s` | Save & quit | — |
| | | `d` | Discard & quit | |
| | | `c` | Cancel | |
| | | `escape` | Cancel (`show=False`) | |
| `CaptureScreen` (default screen) | `screens/capture.py` | `ctrl+s` | Save & new | **editable** |
| | | `escape` | Discard & quit | |
| | | `ctrl+f` | Focus related-notes panel | |
| | | `ctrl+n` | Open link under cursor | |
| `AskScreen` | `screens/ask.py` | `escape` | Back | — |
| | | `up` | Move the focused-citation cursor to the previous citation (`show=False` — hidden the same way `BrowseScreen`'s own arrow-key row navigation is; the status line above the footer shows the current one) | |
| | | `down` | Move the focused-citation cursor to the next citation (`show=False`, same reasoning) | |
| | | `ctrl+j` | Open the focused citation's cited version/snapshot (`lode-35nu.4`) | |
| | | `ctrl+s` | Save the current answer as a new note (`lode-35nu.11.4`, per-note ask only) | |
| `SaveAsNoteConfirmScreen` | `screens/save_as_note_confirm.py` | `y` | Yes, save | — |
| | | `n` | No, cancel | |
| | | `escape` | Cancel (`show=False`) | |
| `ConfigScreen` | `screens/config.py` | `escape` | Back | — |
| `ReconcileScreen` | `screens/reconcile.py` | `r` | Re-apply | read-only diff |
| | | `d` | Discard | |
| `RelatedNotesPanel` (focusable widget, not a `Screen`) | `related_notes_panel.py` | `up` | Select previous related | — (only active once the panel holds focus, per the trap above) |
| | | `down` | Select next related | |
| | | `enter` | Open selected (modal) | |
| `RelatedNoteModalScreen` | `screens/related_note_modal.py` | `escape` | Back | — |
| `HelpScreen` | `screens/help.py` | `escape` | Close (`show=False` — footerless, `lode-2bt3.2`) | — |
| | | `?` | Close (`show=False`, same reason) | |

`ctrl+s` on `EditScreen`/`CaptureScreen` already predates this doc and is the precedent
`lode-olmi.2`'s `Ctrl+H`, `lode-g5es`'s `Ctrl+G`, `lode-0sjj`'s `Ctrl+R`, and `lode-ev5j.3`'s
`Ctrl+N` all follow: every existing binding on a screen with an editable body uses a non-printable
key — a `ctrl+` combo like these, or the named key `escape` — never a bare letter, and (per the "No
function keys" policy above) never a function key either. (`CaptureScreen` also bound `ctrl+n`
until `lode-bsmc` consolidated it onto the same stack-aware `ctrl+s` and freed the letter, which
`lode-ev5j.3` then reclaimed for the open-link binding — see the "Current keymap" table above and
the letter-space accounting earlier in this doc.)

**`lode-ev5j.3`'s open-link binding is the same `Ctrl+N` on all three of its screens on purpose,
even though only `EditScreen`'s body is editable.** `VersionViewScreen` and `SnapshotViewerScreen`
both have `read_only=True` bodies, so a bare printable key would have reached a Screen-level binding
there too (see the read-only-body exception noted above) — but a feature reachable by a *different*
key depending on which screen happens to be showing it is worse than one that costs a `ctrl+`
prefix on two screens that didn't strictly need it. One key, three screens, no exceptions.
`SnapshotViewerScreen` also gained a `LodeFooter` for the first time as part of this ticket — it had
no footer at all before, so its two pre-existing bindings (`escape`/`t`) were technically
undiscoverable too; `lode-ev5j.3`'s own "shown in the footer" acceptance criterion closed that gap
rather than leaving the new binding as a third invisible one.

**`lode-5ill` extended the same `Ctrl+N` to a fourth screen, `CaptureScreen`, once `lode-ngk2` made
it a colouring screen too.** The "one key, no exceptions" reasoning above applies unchanged:
`CaptureScreen`'s body is editable (same trap `EditScreen` avoids), `ctrl+n` was already confirmed
free there (`lode-bsmc`), and link extraction never depended on the colouring at all — it's a
regex scan of the cursor's line (`_link_open.py`), independent of whichever screens happen to be
coloured. Bound identically: `Binding("ctrl+n", "open_link", "Link")` delegating to the same
`open_link_under_cursor` helper.

**`lode-35nu.4` claims the last formally-safe letter, `ctrl+j`, on `AskScreen`.** Up/Down were free
(confirmed against `Input.BINDINGS` — the question field's own builtin bindings cover left/right/
home/end/backspace/delete/enter/cut/copy/paste, never up/down, so they bubble to the Screen), but
opening the focused citation needed a genuine action key, and every bare printable letter is
reachable by the question `Input` too (it consumes any `is_printable` key exactly like `TextArea`
does — see the "hard rule" above) — so this is not a `read_only`-body exception; a bare `o` here
would type into the question field instead of opening anything. `ctrl+j` was the one letter this
doc's own ledger had left unclaimed, confirmed against `Input.BINDINGS`, `KEY_ALIASES`, and every
other screen's `BINDINGS` before landing it. Bound as `Binding("ctrl+j", "open_citation", "Open
citation")`; Up/Down are `Binding("up", "focus_prev_citation", ..., show=False)` /
`Binding("down", "focus_next_citation", ..., show=False)` — hidden from the footer, matching how
`BrowseScreen`'s own `DataTable` row navigation (also Up/Down) isn't listed there either.

Two things this claim did *not* clear, recorded so the next reader doesn't re-derive them:

- **Up/Down are not globally free on this screen — they are free from the question `Input` only.**
  The answer pane (`#ask-results-pane`) is a focusable `VerticalScroll` whose own `up`/`down`
  bindings scroll the answer, and a focused widget wins over a Screen binding. Verified empirically
  (tab to the pane, press Down: it scrolls and the citation cursor does *not* move). This is the
  wanted outcome — scrolling a long answer is never stolen — but it means the citation cursor is
  reachable only while the question field has focus. `ctrl+j` reaches the Screen from both.
- **The terminal-level `ctrl+j` caveat from the "No function keys" section still stands** and was
  not retired by this claim. Textual's own tables are clean (`\n` → `ctrl+j`, `\r` → `enter`, so a
  `ctrl+j` binding never sees a normal Return, verified against the installed 8.2.8 tables), but a
  terminal emulator configured to send LF rather than CR for Return would deliver `ctrl+j` here.
  That does **not** regress submit-on-Enter: on such a terminal `Input`'s `enter` binding is
  already unreachable app-wide, before and after this ticket. The residual is narrower — on that
  configuration, Return in the question field would additionally fire "open citation". Accepted as
  the known cost of spending the last risk-flagged letter; revisit if the letter pool is widened.

**`lode-35nu.11.4` claims `ctrl+s` on `AskScreen`, without widening the global letter ledger.**
The ledger above tracks `ctrl+<letter>` collisions against `TextArea`'s builtins (the trap that
motivated the whole doc), but `AskScreen` composes no `TextArea` — only an `Input` (the question
field), whose own builtins (`ctrl+a/e/w/u/k/x/c/v/d`, plus `ctrl+left`/`ctrl+right`/`ctrl+shift+*`)
do not include `ctrl+s`; confirmed against `Input.BINDINGS` directly (Textual 8.2.8), same as every
`ctrl+<letter>` claim above. `ctrl+s` is already spent on `EditScreen`/`CaptureScreen` ("Save") —
reused here on purpose rather than avoided, since it is the identical action (confirm-and-save) on
a screen that is never active at the same time as either of those two, so there is no collision to
resolve, only a mnemonic to reuse. Confirmed against `KEY_ALIASES` (no entry) and `App.BINDINGS`'s
`priority=True` reservations (none) too. Gated at the call site to a per-note ask with a
non-abstained answer on screen (`AskScreen._note_id is not None` — a source note to link the new
note back to is what the feature needs); a no-op notification otherwise, same pattern as `ctrl+j`'s
own "no citation to open" guard.

## The keybinding help overlay (`lode-2bt3.2`)

**The problem: `?` is printable, so it cannot reach the three text-entry
screens.** `CaptureScreen`, `EditScreen`, and `AskScreen` each focus an
editable `TextArea`/`Input` by default, and (per the "hard rule" above) a
focused text-entry widget consumes any `is_printable` keypress as a literal
character before it can ever reach a Screen- or App-level binding. A bare
`?` binding would therefore be silently unreachable from the exact three
screens a user spends the most time on — confirmed empirically
(`tests/test_tui_help_screen.py::test_question_mark_is_swallowed_as_a_literal_character_on_capture`).

**The fix: `Ctrl+Underscore`, not the unavailable `Ctrl+?`.** The obvious
mnemonic, `Ctrl+?`, does not exist in Textual's `Keys` vocabulary — terminals
cannot encode `Ctrl+Shift+/` as a distinct byte, confirmed against the
installed textual 8.2.8 (`textual.keys.Keys` has no `ControlQuestionMark`
entry). The substitute is `Ctrl+Underscore` (`Keys.ControlUnderscore =
"ctrl+underscore"`) — Textual's name for the `0x1f` byte terminals emit for
`Ctrl+/` (the same physical key as `?`, on virtually every keyboard layout,
since `_` sits on the shifted `-`/`/`-adjacent row and terminals encode the
control-modified byte independent of the shift state). **Terminal-arrival
verification (per the ticket's own requirement):** confirmed at the
ANSI-sequence-table level rather than against a live terminal (no
interactive terminal is available in this build environment) —
`textual/_ansi_sequences.py` maps the raw byte `"\x1f"` directly to
`(Keys.ControlUnderscore,)`, which is the standard, universal `xterm`/`vt100`
encoding for `Ctrl+/` (also documented as "Also for Ctrl-hyphen" in that same
table) — not something Textual invented or that varies meaningfully by
terminal emulator. If a specific terminal is later found not to deliver this
byte, escalate rather than silently falling back to a letter (none are left
— see the "No function keys" section's letter-space accounting above).

Neither a ctrl-*letter* (so it doesn't draw on the exhausted letter-space
this doc's own ledger tracks) nor a function key (so `lode-juz8.1`'s ban
stands untouched) — `Ctrl+Underscore` was unbound anywhere in lode before
this ticket.

**Both keys, one action.** `LodeApp.BINDINGS` binds `ctrl+underscore` (shown
in the footer) and `?` (hidden, `show=False`) to the same
`action_show_help`. `?` still works as a convenience wherever no
`TextArea`/`Input` holds focus (`BrowseScreen`, `TagsScreen`, `ConfigScreen`,
…) — freed for this by `lode-2bt3.1` retiring search-direction. Screens with
an editable body reach the overlay only via `Ctrl+Underscore`.

**A real, visible App-level binding — not `show=False`.** The overlay is
a first-class, discoverable affordance by design (the whole point of the
ticket); it is not buried behind its own undiscoverable key.

**Paid for by demoting Quit's footer entry, not by widening the footer
budget.** An App-level binding renders in *every* screen's footer (this
doc's own `ctrl+l`/"Ask" history documents the same +7-column cost pattern),
and `EditScreen`/`BrowseScreen` were already the tightest of the ten
footer-bearing screens with little slack left. Rather than shorten more
labels, this ticket drops `ctrl+q`'s "Quit" footer entry (`show=False` —
the binding itself is unchanged, still `priority=True` and fully live):
`Ctrl+Q` is well known, and Escape is an additional route out on most
screens, so hiding it from the footer costs little. This is a small,
targeted pull-forward of `lode-2bt3.3`'s general per-screen footer-priority
mechanism (not a substitute for it — that ticket must re-verify this call
once the full mechanism exists), using the one mechanism (`show=False`) that
ticket is expected to generalize.

**Footer-width measurements (100 columns, every screen re-measured against
the real pilot after this change):**

| Screen | Before (`Quit` shown, no `Help`) | After (`Quit` hidden, `Help` shown) |
|---|---|---|
| `CaptureScreen` | — | 84/100 |
| `BrowseScreen` | — | 94/100 |
| `EditScreen` | 98/100 (tightest of the ten) | 98/100 (unchanged — one drop pays for one add) |
| `AskScreen` | — | 81/100 |

No screen needed a label shortened; dropping `Quit` almost exactly offsets
adding `Help` on every footer, since both render as a 1-2 character key
display plus a 4-5 character label.

(Historical snapshot as of this ticket. `BrowseScreen`'s 94/100 and
`EditScreen`'s 98/100 above have since moved to 97/100 and 89/100
respectively — see "Per-screen footer priority" below for `lode-2bt3.3`'s
current numbers and what changed to get there.)

**Content and shape.** `HelpScreen` (`src/lode/tui/screens/help.py`)
composes Textual's own shipped `BindingsTable`
(`textual.widgets._key_panel`) — the ticket's own review (bd notes,
criticisms 1/2/7) found that hand-rolling the screen/app binding merge, the
screen-shadows-app resolution, and the namespace grouping would reinvent
already-correct framework machinery, so this module writes only the open
key, a one-property override to defeat `ModalScreen`'s binding-chain
truncation (`HelpScreen.active_bindings`, overriding `Screen`'s own property
to return a snapshot captured *before* the modal was pushed — see that
module's docstring), and styling. Inclusion rule: exclude
`binding.system` only, matching `BindingsTable`'s own filter — no
lode-specific narrowing layered on top, which is also what keeps
`tests/test_tui_help_screen.py`'s anti-drift test bounded and implementable.
A `ModalScreen`, footerless (dismisses on Escape/`?`, no other standing
action) per docs/tui.md's modal rule.

## Per-screen footer priority — the footer becomes a hint surface (`lode-2bt3.3`)

**The payoff of `lode-2bt3.2`'s overlay.** `lode-wk4x` rejected per-screen footer priority because
it "silently hides bindings" — valid only while no fallback discovery surface existed. With the
help overlay in place (`Ctrl+_`/`?`, lists every binding on a screen INCLUDING `show=False` ones,
`tests/test_tui_help_screen.py`'s own anti-drift gate), a footer entry hidden via `Binding(show=False)`
is still fully live and one keypress away — hiding it from the footer is no longer lossy.

**The mechanism is the one `lode-2bt3.2` already used to hide `Quit` — generalized, not
reinvented.** No new machinery: `Binding(show=False)` at the declaration site, per binding, per
screen. `LodeFooter` (`src/lode/tui/widgets/lode_footer.py`) needed zero changes — Textual's stock
`Footer` already honours `show=`; this ticket is entirely a matter of which `BINDINGS` entries carry
the flag and what their labels say.

**Re-measured from post-`lode-2bt3.2` trunk (not an earlier snapshot), per the ticket's own note that
the un-abbreviation budget is tighter than its description assumed** — `lode-2bt3.2` already spent one
App-level footer slot (`Help`) and paid for it by hiding `Quit`.

- **`BrowseScreen`** — `Expand` (`x`, `toggle_summary`) is now hidden: a reversible, non-destructive
  display toggle, the least-needed reminder of the screen's seven bindings once learned, unlike
  Inspect/Delete/Find/Quick which apply to the primary read/search/delete workflow every session. That
  recovers the width to un-abbreviate `"S"` → `"Quick"` (not `"Search"` — this screen already has
  `"Find"` for `/`'s scan, and a second `"Search"` label would read as a duplicate) and `"View"` →
  `"View content"`. MEASURED: 97/100, `hscroll=False` (the App-level `Help` label's ~7 columns included). Un-hiding
  `Expand` at full length reopens the overflow: 106/100 with `hscroll=True` (confirmed, not assumed).
- **`EditScreen`** — `"View content"` (`ctrl+r`, view externally-retrieved content) and `"Link"`
  (`ctrl+n`, open URL under cursor) are now hidden: both apply only to a subset of notes (an
  externally-retrieved note; a note whose cursor sits on a URL) rather than every note this screen
  edits, unlike Save/Back/Related/History/Inspect/Ask. That recovers enough width to restore
  `"Rel"` → `"Related"` and `"Hist"` → `"History"` in full. MEASURED: 89/100, `hscroll=False` (11
  columns' slack, deliberately not spent further — see `"Cfg"` below). Un-hiding `"View content"`
  alone at full length reopens the overflow: 105/100 with `hscroll=True` (confirmed, not assumed).
- **`"Cfg"` → `"Config"`, RE-EXAMINED against `EditScreen` as the ticket instructed, and now
  RE-PINNED against `BrowseScreen` instead.** `Cfg` is an App-level binding, so any label change
  there renders in every footer-bearing screen, not just `EditScreen`. `EditScreen` itself now has
  11 columns' slack and could absorb `"Config"` alone — but `BrowseScreen`, at 97/100 after its own
  un-abbreviation above, cannot: swapping in `"Config"` measures exactly 100/100 with
  `show_horizontal_scrollbar` flipping to `True` (fails the "fits without hscroll" bar). `"Cfg"`
  therefore stays abbreviated — not because `EditScreen` needs it short any more, but because the
  binding-declaration comment in `src/lode/tui/app.py` is the single source of truth for an
  App-level label and it now names `BrowseScreen`, not `EditScreen`, as the constraint. A future
  ticket that wants to restore it must re-measure `BrowseScreen`'s footer, not just `EditScreen`'s.
- **`Quit`'s demotion — RE-VERIFIED, not inherited**, per `lode-2bt3.2`'s own note that this ticket
  must re-check the call once the general mechanism exists. The reasoning is unchanged: `Ctrl+Q` is
  one of the most universally-known TUI conventions, Escape covers the same ground on most screens,
  and the help overlay lists it regardless of footer visibility. It stays hidden.

**The footer is a hint surface, not the binding contract — see `docs/tui.md`'s footer section for
the redefinition** (the abbreviate-an-existing-label pattern this doc's own history above documents
repeatedly is retired there).

**Test-gate semantics changed to match.** `tests/test_tui_browse_screen.py`'s Browse/Edit footer
tests were renamed `..._with_every_shown_binding_visible` (from `..._with_every_binding_visible`)
and now assert only that every SHOWN binding fits in 100 columns without `hscroll`; the guarantee
that every HIDDEN binding stays reachable is `tests/test_tui_help_screen.py`'s anti-drift gate's job
(parametrized over both screens that hide anything — `BrowseScreen` and `EditScreen` — `show=False`
entries included), not duplicated in the footer tests. That parametrization is this ticket's own
review finding: the gate previously ran against `BrowseScreen` alone as "the representative screen",
which was sound only while no Screen-level binding anywhere was hidden; the moment `EditScreen`
started hiding `ctrl+r`/`ctrl+n`, "the gate covers it" stopped being true for that screen until the
gate visited it.

## Resolved collisions (history, for context)

Three siblings (`lode-olmi.9`, `.6`, `.2`) independently claimed keys with no shared reference and
collided at land time — this doc exists to stop the next one:

- **`lode-olmi.9`** landed first: Screen-level `f4` on `EditScreen`/`CaptureScreen` → focus the
  related-notes panel (its own `up`/`down`/`enter` inside the panel, all non-conflicting with the
  body `TextArea` because they only fire once the panel — not the body — holds focus).
- **`lode-olmi.2`**: `EditScreen`'s "open version history from the editor" binding was specified as
  bare `h`; proven infeasible against an editable `TextArea` (root cause above), amended to
  **`Ctrl+H`**.
- **`lode-g5es`**: `EditScreen`'s enrichment-inspector binding was specified as a verbatim copy of
  `BrowseScreen`'s bare `i`; escalated for the same reason as `.2`'s `h` (empirically confirmed:
  pressing `i` typed a literal `i` into the note body instead of opening the modal). Human-resolved
  as a Ctrl-prefixed key — Ctrl-prefixed keys are now project practice for `EditScreen` actions —
  and landed as **`Ctrl+G`** after two more candidates each failed their own check: `Ctrl+I` (Textual
  aliases it to `tab`, a non-printable navigation key, so the binding is unreachable) and `Ctrl+P` (Textual's `App`
  reserves it, `priority=True`, for the command palette, so it always wins over any Screen binding).
- **`lode-olmi.6`**: the Tags screen's App-level binding was originally `f4`; shadowed by `.9`'s
  Screen-level `f4` on the default screen (`CaptureScreen`), so it landed rekeyed to **`f5`** (see
  the table above) — the next free App-level function key, confirmed with a repo-wide grep for
  `f5` against both `App.BINDINGS` and every screen's own `BINDINGS` before landing.
- **`lode-0sjj`**: the content-viewer binding (lode-olmi.8's decision doc) was specified as a single
  `v` key shared verbatim by both `BrowseScreen` and `EditScreen` — escalated for the identical
  reason as `.2`'s `h` and `g5es`'s `i` (empirically confirmed: pressing `v` on `EditScreen` typed a
  literal `v` into the note body instead of opening the viewer). `BrowseScreen`'s half kept bare `v`
  (its focused widget is a `DataTable`, not an editable `TextArea`, so no collision there);
  human-resolved `EditScreen`'s half as a Ctrl-prefixed key, per project practice, landing on
  **`Ctrl+R`** ("retrieved") after checking it against the same three traps `Ctrl+G` was: it is not
  one of `TextArea`'s own builtin bindings (`ctrl+a/e/w/d/x/c/v/u/k/z/y`, see the table in the hard
  rule's "before binding" checklist above), `KEY_ALIASES` doesn't remap it to a non-printable key, and
  `App` doesn't reserve it with `priority=True`.
- **`lode-juz8.1`**: adopted the "no function keys" policy above and remapped every remaining
  function key in the TUI — App-level `f2`/`f3`/`f5` (Config/Browse/Tags) and Screen-level `f4`
  (`focus_related` on `CaptureScreen`/`EditScreen`) — to `ctrl+` combos in one pass, since the
  by-then-nearly-exhausted safe-letter space (six formally-clean candidates — `b`, `f`, `j`, `l`,
  `o`, `t` — for four required distinct bindings) meant picking them independently, ticket by
  ticket, risked exactly the kind of collision this doc's "Resolved collisions" history already
  catalogs three times over. Landed as **`Ctrl+O`** (Config, "Options"), **`Ctrl+B`** (Browse,
  direct mnemonic), **`Ctrl+T`** (Tags, direct mnemonic), and **`Ctrl+F`** (focus-related, "Focus")
  — each confirmed against the same three formal traps as every Ctrl-binding above (`TextArea`
  builtins, `KEY_ALIASES`, `App` `priority=True` reservations), by inspecting the installed
  `textual.widgets.TextArea.BINDINGS`, `textual.keys.KEY_ALIASES`, and `textual.app.App.BINDINGS`
  directly rather than by assumption. `l` and `j` were left unclaimed (see the policy section above
  for why) for the next ticket that needs a fifth.
- **`lode-11io`**: wired up the previously-unreachable `AskScreen` (registered in `SCREENS` but
  nothing ever pushed it). The mnemonic pick `ctrl+a` was tried and rejected — MEASURED: with a
  `ctrl+a` → `show_ask` binding added App-level, pressing `ctrl+a` on `CaptureScreen` never reached
  the app (action did not fire, screen unchanged) and the entry did not even render in the footer,
  because both `TextArea` and `Input` already claim `ctrl+a` as a builtin (`home,ctrl+a`,
  cursor-to-line-start) — it would have been dead on exactly the three text-entry screens (Capture,
  Ask, Edit) and live everywhere else. Landed as **`Ctrl+L`** instead (the letter space was down to
  `l`/`j`; `ctrl+j` is the raw LF byte, worse), confirmed against all three traps and against every
  screen's own `BINDINGS` (none uses `ctrl+l`). Also fixed `AskScreen`'s Escape, which called
  `self.app.exit()` directly (coherent only while Ask was unreachable/standalone) — it now pops like
  every sibling, which closes `lode-s58y` at the root: that ticket's "footer shows Quit twice" was
  true, but its proposed fix (relabel to "Back") was wrong, since escape really did quit either way;
  once Escape genuinely pops, the duplicate *action* disappears and the label is honestly "Back".
- **`lode-ev5j.3`**: the open-link-under-cursor binding (Feature 3 of the `lode-ev5j` markdown
  epic). The letter space was down to `n`/`j` after `lode-11io` claimed `l`; `ctrl+n` won outright
  (no known caveat, freed by `lode-bsmc`'s Ctrl+S consolidation) over `ctrl+j` (the raw LF byte
  caveat flagged in the "No function keys" section) — confirmed against all three traps and against
  every screen's own `BINDINGS` (none used `ctrl+n`). Bound identically on all three of its target
  screens (`EditScreen`/`VersionViewScreen`/`SnapshotViewerScreen`) even though only `EditScreen`'s
  body is editable — see the note above the table for why. Landing it also reopened `EditScreen`'s
  footer-width budget: MEASURED at 105/100 with every other label full-length (the first time this
  screen has clipped since `lode-uczx`), closed by shortening two more labels — "Related" → "Rel"
  and "History" → "Hist" — to 98/100 (measured; see `EditScreen`'s own `BINDINGS` comment in
  `src/lode/tui/screens/edit.py`). `SnapshotViewerScreen` gained a `LodeFooter` for the first time
  (previously footerless, with no way to show its own pre-existing `escape`/`t` bindings either) —
  the first `ModalScreen` in the tree to get one; the small glance-and-dismiss popups
  (`DiscardConfirmScreen`, `DeleteConfirmScreen`, `EnrichmentModalScreen`, `RelatedNoteModalScreen`)
  stay footerless on purpose, unaffected.
