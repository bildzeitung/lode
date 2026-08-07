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

**One documented exception: `RelatedNotesPanel`** — see [its own section
below](#relatednotespanel-reserves-its-maximum-height-at-mount-lode-35nu10). Do *not* reach for
`height: 1fr` there. It is an `auto`-height `Static` sharing a `Vertical` with a `1fr` `TextArea`,
and its content arrives **asynchronously** while the user is typing, so the requirement is stronger
than "stay inside the terminal": its box must never change size *at all*, or it moves the ground
under the edit cursor. `1fr` would also clip rather than scroll — a `Static` is not a `ScrollView`,
which is exactly the distinction the `DataTable` prose above turns on. It gets a fixed reserved
height instead, pinned imperatively because the row count comes from a runtime `Settings` knob.

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

**One shared footer widget, not a call site per screen each managing its own flags.** Every
footer-bearing screen composes `lode.tui.widgets.lode_footer.LodeFooter` — a ~4-line `Footer` subclass
that bakes in `compact=True, show_command_palette=False` — instead of calling Textual's stock
`Footer()` directly. Before `lode-uczx`, only two of the ten **footer-bearing** screens
(`BrowseScreen`, `CaptureScreen`) passed those two flags explicitly, because each had independently
hit an overflow bug and fixed it locally; the other eight stayed bare. That is drift-by-default: a
new screen that forgets the flags regresses silently, which is exactly how `CaptureScreen` — the
app's own default/landing screen — clipped past `BrowseScreen`'s fix undetected (`lode-3rvw`).
`LodeFooter` is the one seam for any future footer-wide style change; a screen never repeats the
flags itself.

**That "ten" counts footers, not screens** — `lode-uczx` converted ten `yield Footer()` call sites
that lived in only six modules at the time (`browse.py` alone held five). The module tree has grown
a lot since: **15** screen modules live under `src/lode/tui/screens/` today (excluding `__init__`
and underscore-prefixed leaf modules). Almost all of that growth is the one-Screen-per-module split
(`docs/conventions.md`) unpacking `browse.py` and `capture.py`, not new screens — the screen count
itself went 14 → 15. The footer-bearing count is now **eleven** — `lode-ev5j.3` moved
`SnapshotViewerScreen` from bare to footer-bearing (see the rule just below) — and the other **four**
are modals that stay bare by design.

**Modals are footerless unless they carry standing actions (`lode-ev5j.3`).** A modal that is a
transient glance-and-dismiss popup — a confirm dialog or an inline picker/detail view
(`DiscardConfirmScreen`, `DeleteConfirmScreen`, `EnrichmentModalScreen`, `RelatedNoteModalScreen`) —
stays footerless on purpose: it has no standing, discoverable action worth a permanent on-screen
reminder. A modal earns a footer once it carries real, standing actions with nowhere else to show
them. `SnapshotViewerScreen` is the precedent: it already had two such actions (`Back`, `Toggle raw
HTML`) with no footer to list them in; once a third binding (open-link-under-cursor,
`lode-ev5j.3`) needed the same on-screen discoverability its sibling screens (`edit.py`,
`version_view.py`) already gave it, the gap became a blocker rather than a pre-existing quirk to
leave alone, so it was closed rather than deferred. When adding a new modal, ask the same question:
does it have a standing action worth keeping visible, or is it a glance-and-dismiss popup? The
latter stays bare.

Both of those flags are load-bearing at the 100-column bound, and neither is the `show=False`
binding-hiding that `lode-l38d.3` ruled out and this epic has held to since: `compact=True` only
trims Textual's per-entry padding, and `show_command_palette=False` drops only the footer's
auto-added "^p palette" icon — `ctrl+p` still opens the palette (verified), and the palette was
never one of lode's declared `BINDINGS`. Measured costs of dropping either, plus the tests that
enforce it, are in [`lode.tui.widgets.lode_footer`](../src/lode/tui/widgets/lode_footer.py)'s docstring.

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

## Capture screen: Ctrl+S is "Save & New," never "Save & Browse" (`lode-bsmc`)

`CaptureScreen`'s consolidated Ctrl+S (`docs/keybindings.md`) saves and resets the buffer for a
fresh note, staying on the capture screen — it deliberately does **not** navigate to Browse.
Considered and rejected: landing on Browse after a save makes the just-saved note feel like an
*incomplete task* (as if it weren't really saved), turning "remember to look at it later" into an
easily-overlooked step. Staying put and clearing the buffer gives clean closure instead — the task
is done, so do the next thing right here. Don't re-litigate this.

## Settling TUI tests under load: predicate wait vs. keystroke drain, one home in conftest (`lode-lcju`)

Two tickets (`lode-64jn`, `lode-9y68`) independently hit the same root cause — a flaky TUI test
under real machine load (several agents gating at once) — and independently invented two different
settle helpers, in two different test files, neither referencing the other. Neither lived in
`tests/conftest.py`, which had no pilot/settle helper at all. That is the exact failure this
section exists to close: the next TUI test author must find one documented rule and one home, not
grep two contradictory docstrings and invent a third dialect.

**The mechanism (verified against Textual 8.2.8's source, not guessed).** Everything below flows
from one fact: `textual._wait.wait_for_idle` decides "idle" by comparing this process's own CPU
time (`time.process_time()`) against wall-clock time (`time.monotonic()`). A process that is
merely *starved of scheduler timeslices* — which this machine legitimately causes, running several
concurrent `nox -s tests` invocations at once — advances its own CPU time slowly regardless of
whether the screen transition or keystroke cascade it's supposed to be waiting for has actually
finished, and the heuristic misreads that starvation as idleness. It is the **only** load-sensitive
element anywhere in the pilot press/pause path:

- `pilot.press(*keys)` is `App._press_keys(keys)` (paces *between* keystrokes using
  `wait_for_idle()` alone — no message-count check) **then one** `_wait_for_screen()` (a real
  message-count drain) at the very end. So a multi-key `press()` can dispatch a later key while an
  earlier key's reactive cascade is still in flight; only the very last key gets a real drain.
- `pilot.pause()` is `_wait_for_screen()` (a message-count drain) **plus** `wait_for_idle(0)` (the
  heuristic).
- `_wait_for_screen()` is a **barrier, not a clock heuristic** — so OS starvation cannot defeat it,
  only make it wait longer. It snapshots `app.screen` and its child list **once, up front**, posts
  one `call_later` sentinel to each child, and awaits a counter reaching zero (30s timeout). It
  reads no pending-message *count*: a sentinel arrives behind everything already queued on that
  child, so draining it proves those messages were processed. What that does *not* cover is a
  message enqueued **after** its child's sentinel already ran (a delayed side effect of processing
  an earlier one), or a child that did not exist when the list was snapshotted. Note this is why
  *repeating* a drain is not a general fix: a second barrier has the same shape as the first.
- **asyncio's ready-queue ordering is *not* perturbed by OS starvation** — starvation slows the
  whole event loop uniformly; it does not reorder callbacks relative to each other. This is the
  fact that separates a real settle fix from a placebo: a drain added to fix a *reordering* race
  reorders nothing, because starvation never disorders anything to begin with. Only a fix that
  targets `wait_for_idle`'s wall-clock-vs-CPU-time comparison addresses the actual mechanism.

**The rule — two sanctioned patterns, chosen by what's being waited for, not by preference:**

1. **A precondition** ("the screen has transitioned," "the new screen has finished composing") →
   `_wait_until(predicate, description, timeout=...)` (`tests/conftest.py`): polls a real condition
   via `asyncio.sleep`, bounded by a wall-clock `timeout`, and fails loudly — a real
   `AssertionError` naming which condition hung — if it's never met. This is **strictly stronger**
   than a fixed drain count for a precondition: it waits exactly as long as needed and reports
   loudly when that's insufficient, where a fixed count of drains neither waits longer under worse
   load nor reports anything when it fails to be enough.

   **Forbidden use:** never write the predicate in terms of the test's own expected value —
   `_wait_until(lambda: table.cursor_row == 2, ...)` bakes the assertion into the wait, which is
   the retry-on-assertion antipattern (masks a real bug as a slow-to-settle one, and a wrong final
   value just times out instead of failing where it happens). `_wait_until` is for **preconditions
   the test needs before it can act or assert**, never for the value the assertion itself checks.

2. **A stateful, read-back keystroke cascade** (a later keystroke's behavior depends on state an
   earlier keystroke's cascade produced — e.g. incremental search, where `_seek_match` reads
   `table.cursor_row` as its next scan's start) → `_press_and_settle(pilot, *keys)`
   (`tests/conftest.py`): presses one key at a time via `pilot.press(key)` (whose own trailing
   `_wait_for_screen()` is a real drain, not the CPU heuristic), so every keystroke is fully settled
   before the next is dispatched. This is the case `_wait_until` cannot cover: the correct wait
   *is* the assertion's own precondition ("has the previous key's cascade landed"), and expressing
   that as a predicate would mean polling for the exact cursor position the test is about to assert
   on — the forbidden case above. A plain multi-key `pilot.press("down", "down", "down")` remains
   fine and is deliberately left alone elsewhere in the suite: cursor moves like that are
   order-preserving with no read-back dependency between keys, and `press()`'s own trailing drain
   already covers the final read.

**One home: `tests/conftest.py`.** Both helpers now live there — the two rival originals
(`tests/test_tui_reconcile_screen.py`'s `_wait_until` from `lode-64jn`, and
`tests/test_tui_browse_screen.py`'s `_press_and_settle` from `lode-9y68`) were moved verbatim, with
their docstrings updated to point at this section instead of restating the mechanism locally. A
screen-specific predicate like `_reconcile_ready` (checks `isinstance(app.screen, ReconcileScreen)`
before reading a screen-specific widget id) stays local to its own test file — it isn't a generic
settle primitive, just a predicate a test hands to `_wait_until`.

**Out of scope, deliberately:** converting the suite's ~90 remaining bare `pilot.pause()` call
sites to one of these helpers. Most are simple, order-independent waits (a single keystroke, or a
cursor move with no read-back dependency) that `pilot.press()`'s own trailing drain already covers
correctly; forcing every site onto `_wait_until`/`_press_and_settle` would add ceremony with no
mechanism behind it. Reach for one of the two helpers above only when a test hits the actual
load-dependent failure mode this section describes.

## RelatedNotesPanel reserves its maximum height at mount (`lode-35nu.10`)

`RelatedNotesPanel` composes into a `Vertical` alongside the body `TextArea`, whose height is `1fr`.
`Static`'s own `DEFAULT_CSS` height is `auto` and Textual sizes `auto` siblings *before* handing the
remainder to a `1fr` sibling — so every time a passive pass rendered *more* related notes than the
last one, the panel's auto height grew on the next layout pass and took rows away from the
already-laid-out `TextArea`, pushing the line the edit cursor was resting on out of view. The
related-notes list is asynchronous and non-user-initiated: it must never move the ground under an
active edit.

The fix is `RelatedNotesPanel.on_mount` pinning `self.styles.height` to the panel's **maximum
possible** size, before any pass has rendered anything, so content growth is invisible to layout.
Three components, all named in the module (`_HEADER_ROWS`, `_FOCUS_BORDER_ROWS`):

- `settings.related_notes_limit` — the most note lines a pass can ever render.
- **+1** header row (`"Related notes:"`).
- **+2** border rows. `lode.tcss`'s `RelatedNotesPanel:focus` rule draws a `round` border once Ctrl+F
  moves focus onto the panel (`lode-olmi.9`), and Textual's default `box-sizing` is `border-box`, so
  a *fixed* height counts that border inside the box. Reserving only the note lines and the header
  keeps the box stable but silently clips the last two related notes exactly while the panel is
  focused — i.e. exactly while Up/Down can step the selection cursor onto them.

A fixed reservation is only *correct* while the panel's rendered height is predictable, and one note
line is not inherently one row. A rendered line is `"· <age> — <snippet>"` and `related.py` truncates
every snippet to `_SNIPPET_CHARS` (80), so at the default 80-column width a full-length snippet
overruns the panel and **wraps to two rows**. Under wrapping the arithmetic above is optimistic
rather than exact, and because a `Static` does not scroll, the overflow is not merely ugly — the tail
of the list is simply *not on screen*: measured by driving the real app at 80x24 with five
full-length snippets, three of five notes were visible unfocused and two of five focused, while
Ctrl+F Up/Down would still happily step the selection cursor onto an invisible row. That defeats the
panel's entire purpose, so the reservation is paired with a constraint that makes it exact:

```css
RelatedNotesPanel {
    text-wrap: nowrap;
    text-overflow: ellipsis;
}
```

**Every note now occupies exactly one row, at any width**, which is what turns `limit + 1 + 2` from
an estimate into a guarantee. The cost is the snippet's tail, replaced by an ellipsis on a narrow
terminal — accepted deliberately, because the snippet is *already* an arbitrary 80-character
truncation of the passage, so nothing complete was being shown in the first place. (Two alternatives
were rejected: shortening `_SNIPPET_CHARS` gets the same one-row property but perturbs a tuned
display constant with a wider blast radius; reserving the two-rows-per-note worst case is complete
but permanently spends ~13 of a 24-row terminal on the panel, plausibly worse than the original bug;
and making the panel *scroll* its own overflow — the shape §1 above would otherwise point you at,
which would make the reservation merely need to be big enough rather than exact and would cost no
snippet text — requires the panel to stop being a `Static`, since a `Static` is not a `ScrollView`.
That last one is the only rejection that is about cost rather than correctness, and it is the one to
revisit if the ellipsis ever proves too lossy in practice.)

**This couples `related_notes_panel.py` to `lode.tcss` in two directions.** Changing the `:focus`
border, or giving the panel a border/padding in its unfocused state, changes how many rows the box
spends on chrome and requires updating `_FOCUS_BORDER_ROWS` with it; and dropping the `nowrap` rule
silently returns the reservation to being optimistic. With both in place the knowable cost of the
reservation is two blank rows below the notes while the panel is unfocused, plus an ellipsized
snippet tail — that is the accepted price of never displacing the cursor while keeping every note
reachable. The height cannot live in `lode.tcss` instead: it depends on a runtime config knob, and
TCSS cannot read `Settings`.

## RelatedNotesPanel's background pass: the straggler is tolerated, not joined (`lode-du4p`)

`RelatedNotesPanel._search_related` runs its work as `await asyncio.to_thread(find_related_notes,
...)` on the loop's shared default executor. `RelatedNotesPanel.on_unmount` cancels the awaiting
coroutine (`lode-ivu`: `_cancel_related_pass()` stops the debounce timer and calls
`workers.cancel_group(self, "related-notes")`), but `asyncio.to_thread` work is not cancellable —
the OS thread already running `find_related_notes` inside the default `ThreadPoolExecutor` keeps
running to completion regardless, on its own schedule, with nothing in the panel left to wait on
it. `lode-du4p` was filed on the premise that this makes the pass a cross-test contamination
hazard: a pass armed by one test could still be running, touching that test's own torn-down
`tmp_path` (DB + LanceDB paths under `$LODE_HOME`), when a later test starts.

**The premise was refuted by measurement, not accepted at face value.** Every TUI test in this
suite drives the app via a synchronous `def test_...` calling `asyncio.run(_drive())` — there is no
`pytest-asyncio` and no `async def test_` anywhere in `tests/`. `asyncio.run()`'s `Runner.close()`
awaits `loop.shutdown_default_executor()` on the way out, which joins every thread the default
executor is still running (bounded by `asyncio.constants.THREAD_JOIN_TIMEOUT`, 300s on 3.14 — a
thread still running past that is abandoned with a loud `RuntimeWarning`, so the join is never
silently skipped). Measured directly (Python 3.14.5, this repo): an `asyncio.to_thread`
task cancelled mid-flight is still `finished=False` immediately after the cancel, but
`finished=True` once `asyncio.run()` has returned, with zero surviving non-main threads. So a pass
armed by one test's `asyncio.run()` call cannot in fact outlive that call — the straggler class this
ticket was filed to close was already closed, by `Runner.close()`, before this ticket existed.
`tests/conftest.py`'s lode-sx17 straggler-misattribution paragraph is intentionally left as-is by
this decision (see its own text): the general backstop it describes covers any *other*
straggler-producing worker, and stays accurate independent of this specific source.

**A product-side fix was built, measured, and reverted — the numbers are why.** A prior pass at
this ticket gave `RelatedNotesPanel` a private single-worker
`concurrent.futures.ThreadPoolExecutor`, with `on_unmount` doing a synchronous, **unbounded**
`concurrent.futures.wait()` on the in-flight future after cancelling the coroutine side — turning an
escaped thread into a bounded (if slow) teardown. Measured on that branch:

- `_cancel_related_pass` is not reached only from `on_unmount`: `reset()` (Ctrl+S, "Save & New") and
  `update_draft("")` (an emptied draft, e.g. select-all-delete while typing). Both are mid-session,
  event-loop-thread paths, not teardown. Measured: a 600ms stand-in pass blocked the event loop
  514ms via `reset()` and 493ms via `update_draft("")` — the whole TUI freezes for most of a pass's
  remaining duration on an ordinary interactive action.
- `on_unmount` itself fires on every screen pop, not only app exit — `EditScreen` composes the
  panel and is popped on Escape, on the discard-confirm answers, and on reconcile, with the app
  still fully live. Measured: a 1000ms stand-in pass blocked the event loop 980ms on an `EditScreen`
  pop, terminal still in raw mode, un-interruptible (Ctrl+C arrives as a keystroke, not `SIGINT`,
  while the loop is blocked).
- The justification written for making that join unbounded — "`find_related_notes` is local-only
  work, no network call since `lode-7ypf`/`lode-sx17`" — is false in production. `lode-7ypf` and
  `lode-sx17` closed that reach in **tests only** (an autouse offline embedder stub plus an offline
  env var, both in `tests/conftest.py`). Production still reaches HuggingFace: on a cold
  `$LODE_HOME/models/` cache, `find_related_notes → embed_query → FastEmbedEmbedder._load()`
  downloads the pinned `nomic-ai/nomic-embed-text-v1.5` ONNX weights (hundreds of MB). So the
  unbounded join's worst case is minutes, on the quit path and on every `EditScreen` exit, on a
  fresh install — see `lode-06p2`, filed against that branch and dissolved (closed) once the branch
  was reverted, since with no join there is no residual left to bound.

**Decision: tolerate the straggler.** No product-side join. `_search_related` stays exactly as it
is on trunk — `await asyncio.to_thread(find_related_notes, ...)` on the shared default executor —
and `on_unmount` keeps `lode-ivu`'s cancel with no join added. Every join shape considered imposed
the measured costs above to guard against a hazard the refutation above already closes. The
cold-cache download itself is unaffected either way — it happens on the background pass, where it
always has, rather than on a teardown path the user is waiting on.

**What "provable" means here, given the refutation.** The ticket's acceptance bar asked to prove "a
pass armed by one test cannot still be running when a later test starts." The `Runner.close()`
measurement above establishes exactly that, per test, without any code change. A green full suite is
not the evidence (nothing failed before this decision either); the `shutdown_default_executor()`
mechanism is.
