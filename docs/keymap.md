# lode — keyboard shortcuts

A reference for every key lode's TUI responds to, for someone **using** lode day to day -- not for someone adding or rebinding a key (that's [keybindings.md](https://github.com/bildzeitung/lode/blob/trunk/docs/keybindings.md), the maintainer doc this page is generated from).

**The live, always-current list is one keypress away.** Press `Ctrl+_` (or `?` on most screens) inside lode any time to open the in-app help overlay -- it lists every binding on the screen you're on, including a few kept off the footer to save space. This page is a convenience for browsing outside the app; the overlay is the definitive source at runtime.

## Keys that work everywhere

| Key | Action |
|---|---|
| `ctrl+q` | Quit |
| `ctrl+o` | Show Config |
| `ctrl+b` | Show Browse |
| `ctrl+t` | Show Tags |
| `ctrl+l` | Show Ask |
| `ctrl+underscore` | Show keybinding help overlay |
| `ctrl+shift+minus` | Show keybinding help overlay |
| `ctrl+minus` | Show keybinding help overlay |
| `?` | Show keybinding help overlay |

## Keys by screen

Each screen also has its own keys, active only while that screen is showing. A key listed here can differ from what the same key does elsewhere in the app -- lode resolves whichever binding belongs to the screen you're currently on first.

### Version history

| Key | Action |
|---|---|
| `escape` | Back |

### Viewing an old version

| Key | Action |
|---|---|
| `escape` | Back |
| `ctrl+n` | Open link under cursor |
| `ctrl+l` | Ask about this note |

### Enrichment inspector

| Key | Action |
|---|---|
| `escape` | Back |

### Yes/No confirmation dialogs

| Key | Action |
|---|---|
| `y` | Yes -- shared base for `DeleteConfirmScreen`, `NoEgressClearConfirmScreen` and `SaveAsNoteConfirmScreen` |
| `n` | No |
| `escape` | Cancel |

### Browse

| Key | Action |
|---|---|
| `escape` | Back |
| `i` | Inspect |
| `v` | View retrieved content |
| `d` | Delete |
| `x` | Expand/collapse summary |
| `n` | Toggle no_egress on the highlighted note; clearing it confirms first |
| `slash` | Search |

### External-source picker

| Key | Action |
|---|---|
| `escape` | Back |

### Tags

| Key | Action |
|---|---|
| `escape` | Back |
| `space` | Toggle tag |

### Viewing a saved web snapshot

| Key | Action |
|---|---|
| `escape` | Back |
| `t` | Toggle raw HTML |
| `ctrl+n` | Open link under cursor |

### Editing a note

| Key | Action |
|---|---|
| `ctrl+s` | Save |
| `escape` | Back (cancel) |
| `ctrl+f` | Focus related-notes panel |
| `ctrl+h` | Show version history |
| `ctrl+g` | Inspect (enrichment modal) |
| `ctrl+r` | View retrieved content |
| `ctrl+n` | Open link under cursor |
| `ctrl+l` | Ask about this note |

### Discard-and-quit confirmation

| Key | Action |
|---|---|
| `s` | Save & quit |
| `d` | Discard & quit |
| `c` | Cancel |
| `escape` | Cancel |

### Capture (the screen you land on)

| Key | Action |
|---|---|
| `ctrl+s` | Save & new |
| `escape` | Discard & quit |
| `ctrl+f` | Focus related-notes panel |
| `ctrl+n` | Open link under cursor |

### Ask

| Key | Action |
|---|---|
| `escape` | Back |
| `up` | Move the focused-citation cursor to the previous citation |
| `down` | Move the focused-citation cursor to the next citation |
| `ctrl+j` | Open the focused citation's cited version/snapshot |
| `ctrl+s` | Save the current answer as a new note |

### Config

| Key | Action |
|---|---|
| `escape` | Back |

### Reconcile

| Key | Action |
|---|---|
| `r` | Re-apply |
| `d` | Discard |

### The related-notes panel

| Key | Action |
|---|---|
| `up` | Select previous related |
| `down` | Select next related |
| `enter` | Open selected (modal) |

### Viewing a related note

| Key | Action |
|---|---|
| `escape` | Back |

### The keybinding help overlay

| Key | Action |
|---|---|
| `escape` | Close |
| `?` | Close |
