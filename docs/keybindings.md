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
through normally (see `BrowseScreen`'s `e`/`i`/`d`/`h` today, none of which touch an editable body).

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
`lode-olmi.2`'s `Ctrl+H` follows: every existing binding on a screen with an editable body uses a
`ctrl+` combo, never a bare letter.

## Resolved collisions (history, for context)

Three siblings (`lode-olmi.9`, `.6`, `.2`) independently claimed keys with no shared reference and
collided at land time — this doc exists to stop the next one:

- **`lode-olmi.9`** landed first: Screen-level `f4` on `EditScreen`/`CaptureScreen` → focus the
  related-notes panel (its own `up`/`down`/`enter` inside the panel, all non-conflicting with the
  body `TextArea` because they only fire once the panel — not the body — holds focus).
- **`lode-olmi.2`**: `EditScreen`'s "open version history from the editor" binding was specified as
  bare `h`; proven infeasible against an editable `TextArea` (root cause above), amended to
  **`Ctrl+H`**.
- **`lode-olmi.6`**: the Tags screen's App-level binding was originally `f4`; shadowed by `.9`'s
  Screen-level `f4` on the default screen (`CaptureScreen`), so it is being rekeyed to a different,
  free App-level key (see the table above) — the exact key is chosen at that ticket's build time,
  consulting this doc.
