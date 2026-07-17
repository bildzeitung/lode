# lode — TUI layout conventions

One page for layout rules that keep a screen's content inside the space it has — vertically (not
running past the docked Footer) and horizontally (fitting the minimum terminal width). Expected to
grow as more screens land (`lode-l38d`'s UX-polish round and beyond) — kept short on purpose so
later conventions have room. See [keybindings.md](keybindings.md) for the companion keymap doc.

## Every middle panel needs an explicit height (`lode-efn2`)

lode's screens follow one shape: `Header()` / one or more middle panels / `Footer()`. Header and
Footer are docked, so they always hold their rows; the question is only ever what the middle does
when its content outgrows the terminal. If a middle panel ends up unbounded, the overflow runs
**past the docked Footer and becomes unreachable** — not scrolled-away. `Screen.max_scroll_y`
stays `0` throughout and none of the containers in play are scroll containers (`Vertical` defaults
to `overflow: hidden`), so there is no scrollbar to reach for. This bit two DataTables
(`#browse-table`/`lode-juz8.2`, `#config-knobs`/`lode-l38d.2`), each surfacing as its own bug
report because nothing forced the next screen's author to remember the rule.

**A widget's own `max-height: 100%` is not enough, and this is the subtle part.** `DataTable`'s
`DEFAULT_CSS` is `height: auto; max-height: 100%`, but that `100%` resolves against the **parent's
height** — *not* against the space left over after the parent's other children. So it self-bounds
only when the table is its parent's sole space-consuming child. `#config-knobs` shares a `Vertical`
with a 7-row `Static` and still claims the Vertical's full height, overshooting the Footer by
exactly those 7 rows; `#browse-table` is a direct child of its Screen but has a non-docked `Input`
sibling, which the full-height table pushes below the fold.

**The fix is always the same shape:** cap the panel to the space *remaining* after its siblings
(`height: 1fr`) — which is precisely what `max-height: 100%` fails to do — so it scrolls its *own*
content internally instead of running off-screen. `DataTable` is a `ScrollView`, so `1fr` is enough
on its own for a table.

**`DataTable` gets this structurally, for free.** [`lode.tcss`](../src/lode/tui/lode.tcss) carries
a blanket `DataTable { height: 1fr; }` rule — every table in the app is bounded without its
author adding a per-id rule. A screen whose table genuinely needs different sizing (e.g.
`#tags-tag-list`, capped to `30%` so the notes list below it keeps most of the space) still adds
its own per-id override; CSS specificity (`#id` beats element) means the override always wins
regardless of rule order (verified: `#tags-tag-list` still resolves to `30%` even though the
blanket rule appears *later* in the file).

Two tables — `version-history-table` and `external-picker-table` — were covered by that blanket
rule without ever having been broken: each is its Screen's sole non-docked child, so
`max-height: 100%` already bounded it exactly. `lode-efn2` was filed believing otherwise; measuring
it at six terminal sizes showed no overflow with or without a rule. They are bounded explicitly now
for uniformity and to survive a future restructure, not because they were buggy.

**For anything else, check the widget's own `DEFAULT_CSS` height before assuming it needs a rule.**
Most Textual containers already default to `1fr` and self-bound with no help from us — measured
against textual 8.2.8:

| Widget | `DEFAULT_CSS` height | Self-bounds? |
|---|---|---|
| `Vertical`, `VerticalScroll`, `TextArea` | `1fr` | Yes — needs no rule |
| `DataTable` | `auto` + `max-height: 100%` | Only as its parent's sole space-consuming child |
| `ListView` | `auto` (no `max-height` at all) | **No** — more exposed than `DataTable` |
| `Static` | `auto` | No — fine while its content is short, overflows once it isn't |

So the rule is *not* "every middle panel needs an entry in `lode.tcss`" — that would add dead rules
to screens Textual already handles (`AskScreen` and `ReconcileScreen` carry no entries today and
are not bugs). The rule is: **a widget whose `DEFAULT_CSS` height is `auto` is the one to look at**,
and it is exposed once its content can outgrow the space *its siblings leave it*. When adding a new
screen or middle panel, ask: **if this panel's content outgrows the terminal, where does the
overflow go?** If the honest answer is "off the bottom, past the Footer," give it `height: 1fr` (or
an `overflow-y: auto` scroll container, if scrolling rather than a fixed pane is the right shape).

Guard tests for this shape assert on layout geometry, not selector text — see
`tests/test_tui_config.py::test_knob_table_scrolls_within_its_own_pane_not_the_whole_screen` and
its `tests/test_tui_browse_screen.py` siblings for the pattern: drive the screen with `run_test`,
assert `screen.max_scroll_y == 0` and the panel's own region ends at or above the Footer's row.

## The footer: a 100-column minimum, one shared widget, no bracketed keys (`lode-uczx`)

lode's minimum supported terminal width is **100 columns**. This had never been written down
anywhere before `lode-uczx` — three prior footer tickets (`lode-l38d.3`, `lode-3rvw`, `lode-3aen`)
each independently measured and coded against an **80**-column assumption instead, re-derived from
a test literal because nothing in `docs/` said otherwise. Design facts belong in `docs/`
(`CLAUDE.md`), so it is recorded here: **when sizing a screen's footer (or any other width-
sensitive layout), 100 columns is the bound to design against, not 80.**

**One shared footer widget, not ten call sites each managing their own flags.** Every
footer-bearing screen composes `lode.tui.lode_footer.LodeFooter` — a ~4-line `Footer` subclass
that bakes in `compact=True, show_command_palette=False` — instead of calling Textual's stock
`Footer()` directly. Before `lode-uczx`, only two of the ten screens (`BrowseScreen`,
`CaptureScreen`) passed those two flags explicitly, because each had independently hit an overflow
bug and fixed it locally; the other eight stayed bare. That is drift-by-default: a new screen that
forgets the flags regresses silently, which is exactly how `CaptureScreen` — the app's own
default/landing screen — clipped past `BrowseScreen`'s fix undetected (`lode-3rvw`). `LodeFooter`
is the one seam for any future footer-wide style change; a screen never repeats the flags itself.

Both of those flags are load-bearing at the 100-column bound, and neither is the `show=False`
binding-hiding that `lode-l38d.3` ruled out and this epic has held to since: `compact=True` only
trims Textual's per-entry padding, and `show_command_palette=False` drops only the footer's
auto-added "^p palette" icon — `ctrl+p` still opens the palette (verified), and the palette was
never one of lode's declared `BINDINGS`. Measured costs of dropping either, plus the tests that
enforce it, are in [`lode.tui.lode_footer`](../src/lode/tui/lode_footer.py)'s docstring.

Rejected alternatives, so the question isn't reopened:

- **Central CSS in `lode.tcss`** targeting Textual's internal compact/command-palette classes
  (`.-compact`, `.-command-palette`): at the 100-column bound this genuinely fits too, but the
  leading dash marks those classes as Textual-*internal*, not public API — a Textual upgrade could
  silently revert the look. `LodeFooter` uses only `Footer`'s public `__init__` parameters instead.
- **Repeating the two flags at each call site:** works, but is the drift-by-default `LodeFooter`
  exists to close.

**The bracketed-key style (`[d]elete`, `[i]nspect`, `E[x]pand`) is ruled out, permanently.** It
needs the binding's key letter to literally appear inside its own description, which only
single-letter bindings have — counted across the app's real binding set: `BrowseScreen` has 4 of 7
(`i`/`v`/`d`/`x`; not `escape`/`slash`/`question_mark`), `ReconcileScreen` has 2 of 2 (`r`/`d`), and
every other screen (`CaptureScreen`, `EditScreen`, `ConfigScreen`, `AskScreen`, `TagsScreen`,
`VersionHistoryScreen`, `VersionViewScreen`, `ExternalPickerScreen`) has **zero** — all `ctrl+`
combos or `escape`, per the no-function-key / no-bare-printable-key-on-an-editable-widget policy
`docs/keybindings.md` documents. Styling only the ~6 bindings out of ~40 that qualify would leave
two visual idioms in one footer bar — *more* drift than a plain, uniform description list, which is
the exact bug the shared `LodeFooter` widget exists to eliminate. Do not reopen this a fourth time.
