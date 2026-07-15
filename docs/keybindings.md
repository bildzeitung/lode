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
check every screen's own `BINDINGS` for the same key too — not just `LodeApp`'s.**

## The hard rule: editable-TextArea screens use non-printable keys

A screen that composes an **editable** `TextArea` (`read_only` not set, or explicitly `False`) —
today `EditScreen` (`src/lode/tui/screens/browse.py`) and `CaptureScreen`
(`src/lode/tui/screens/capture.py`) — must bind new actions to a **non-printable** key: a
`ctrl+<letter>` combo, a function key, or a named key like `escape`/`tab`. **Never a bare printable
character** (a letter, digit, or punctuation key without a modifier).

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
- A non-printable key (`ctrl+<letter>`, a function key, `escape`) survives `TextArea`'s
  `is_printable` filter untouched and reaches the Screen binding normally. **This is the rule.**

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
   control-code encoding, where Ctrl+I *is* the Tab character) — and this `TextArea` already
   consumes `tab` for its own indent handling, so a `Binding("ctrl+i", ...)` here would be silently
   unreachable, the same failure mode as a bare printable key, just one layer further down.
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
panel itself holds focus (moved there via the Screen-level `f4` → `focus_related`), not while the
note body `TextArea` still has focus.

Screens whose `TextArea` is `read_only=True` — `NoteViewScreen`, `VersionViewScreen`,
`ReconcileScreen`'s diff view — are unaffected; `read_only` still lets bare-letter Screen bindings
through normally (see `NoteViewScreen`'s bare `h` for history — a read-only-body screen — or
`BrowseScreen`'s `e`/`i`/`d`, none of which touch an editable body).

## Current keymap

### App-level (`LodeApp`, `src/lode/tui/app.py`)

| Key | Action | Notes |
|---|---|---|
| `ctrl+q` | Quit | `priority=True` — always wins, even over a Screen binding |
| `f2` | Show Config | |
| `f3` | Show Browse | |

Free App-level function keys today: `f1`, `f5`–`f12`. **`f4` is not safely reusable at App-level**
without first resolving the shadow described above — it is already Screen-level `"focus_related"`
on `CaptureScreen` (the default screen) and `EditScreen`, so an App-level `f4` binding would be
unreachable from the screen a user sees on startup. `lode-olmi.6`'s tags screen was rekeyed off
`f4` for exactly this reason; pick a free key from the list above instead (or another explicitly
freed one), and re-grep this file before landing.

### Screen-level

| Screen | File | Key | Action | Body TextArea |
|---|---|---|---|---|
| `NoteViewScreen` | `screens/browse.py` | `escape` | Back | read-only |
| | | `h` | Show history | |
| `VersionHistoryScreen` | `screens/browse.py` | `escape` | Back | — |
| `VersionViewScreen` | `screens/browse.py` | `escape` | Back | read-only |
| `EnrichmentModalScreen` | `screens/browse.py` | `escape` | Back | — |
| `DeleteConfirmScreen` | `screens/browse.py` | `y` | Yes, delete | — |
| | | `n` | No, cancel | |
| | | `escape` | Cancel (`show=False`) | |
| `BrowseScreen` | `screens/browse.py` | `escape` | Back | — (DataTable) |
| | | `e` | Edit | |
| | | `i` | Inspect | |
| | | `d` | Delete | |
| | | `slash` | Search forward | |
| | | `question_mark` | Search backward | |
| `EditScreen` | `screens/browse.py` | `ctrl+s` | Save | **editable** |
| | | `escape` | Back (cancel) | |
| | | `f4` | Focus related-notes panel | |
| | | `ctrl+h` | Show version history | |
| | | `ctrl+g` | Inspect (enrichment modal) | |
| `DiscardConfirmScreen` | `screens/capture.py` | `s` | Save & quit | — |
| | | `d` | Discard & quit | |
| | | `c` | Cancel | |
| | | `escape` | Cancel (`show=False`) | |
| `CaptureScreen` (default screen) | `screens/capture.py` | `ctrl+s` | Save & quit | **editable** |
| | | `ctrl+n` | Save & new | |
| | | `escape` | Discard & quit | |
| | | `f4` | Focus related-notes panel | |
| `AskScreen` | `screens/ask.py` | `escape` | Quit screen | — |
| `ConfigScreen` | `screens/config.py` | `escape` | Back | — |
| `ReconcileScreen` | `screens/reconcile.py` | `r` | Re-apply | read-only diff |
| | | `d` | Discard | |
| `RelatedNotesPanel` (focusable widget, not a `Screen`) | `related_notes_panel.py` | `up` | Select previous related | — (only active once the panel holds focus, per the trap above) |
| | | `down` | Select next related | |
| | | `enter` | Open selected (modal) | |
| `RelatedNoteModalScreen` | `related_notes_panel.py` | `escape` | Back | — |

`ctrl+s`/`ctrl+n` on `EditScreen`/`CaptureScreen` already predate this doc and are the precedent
`lode-olmi.2`'s `Ctrl+H` and `lode-g5es`'s `Ctrl+G` both follow: every existing binding on a screen
with an editable body uses a non-printable key — a `ctrl+` combo like these, or a named/function key
like `escape`/`f4` — never a bare letter.

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
  aliases it to `tab`, already consumed by this `TextArea` for indent) and `Ctrl+P` (Textual's `App`
  reserves it, `priority=True`, for the command palette, so it always wins over any Screen binding).
- **`lode-olmi.6`**: the Tags screen's App-level binding was originally `f4`; shadowed by `.9`'s
  Screen-level `f4` on the default screen (`CaptureScreen`), so it is being rekeyed to a different,
  free App-level key (see the table above) — the exact key is chosen at that ticket's build time,
  consulting this doc.
