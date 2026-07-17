# lode — TUI layout conventions

One page for layout rules that keep a screen's content from running past the docked Footer.
Expected to grow as more screens land (`lode-l38d`'s UX-polish round and beyond) — kept short on
purpose so later conventions have room. See [keybindings.md](keybindings.md) for the companion
keymap doc.

## Every middle panel needs an explicit height (`lode-efn2`)

lode's screens follow one shape: `Header()` / one or more middle panels / `Footer()`, composed
inside a `Screen` whose containing `Vertical` is **not** a scroll container. That matters because
of what happens when a middle panel is left `height: auto` (Textual's default): it sizes to its
full content, and once that exceeds the terminal height the overflow runs **past the docked
Footer and becomes unreachable** — not scrolled-away. `Screen.max_scroll_y` stays `0` throughout;
there is no scrollbar to reach for. This bit three separate DataTables before this convention was
written down (`#browse-table`/`lode-juz8.2`, `#config-knobs`/`lode-l38d.2`, and
`version-history-table`/`external-picker-table`/`lode-efn2`), each surfacing as its own bug report
because nothing forced the next screen's author to remember the rule.

**The fix is always the same shape:** cap the panel to the remaining space (`height: 1fr`) so it
scrolls its *own* content internally instead of running off-screen. `DataTable` is a `ScrollView`,
so `1fr` is enough on its own for a table.

**`DataTable` gets this structurally, for free.** [`lode.tcss`](../src/lode/tui/lode.tcss) carries
a blanket `DataTable { height: 1fr; }` rule — every table in the app is bounded without its
author adding a per-id rule. A screen whose table genuinely needs different sizing (e.g.
`#tags-tag-list`, capped to `30%` so the notes list below it keeps most of the space) still adds
its own per-id override; CSS specificity (`#id` beats element) means the override always wins
regardless of rule order.

**Anything else composed into a middle panel does not get this for free** — a `Vertical`,
`ListView`, `TextArea`-in-a-container, or any future non-`DataTable` widget still needs its own
explicit height entry in `lode.tcss` (or an `overflow-y: auto` scroll container, if scrolling
rather than a fixed pane is the right shape for that screen). When adding a new screen or a new
middle panel to an existing one, ask: **if this panel's content outgrows the terminal, where does
the overflow go?** If the honest answer is "off the bottom, past the Footer," it needs a rule.

Guard tests for this shape assert on layout geometry, not selector text — see
`tests/test_tui_config.py::test_knob_table_scrolls_within_its_own_pane_not_the_whole_screen` and
its `tests/test_tui_browse_screen.py` siblings for the pattern: drive the screen with `run_test`,
assert `screen.max_scroll_y == 0` and the panel's own region ends at or above the Footer's row.
