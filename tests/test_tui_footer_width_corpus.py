"""Corpus gate: every footer-bearing screen fits 100 columns (lode-2rv2).

Discovered while technically reviewing lode-2bt3.3: that ticket promotes to
doctrine (``docs/tui.md``, ``docs/keybindings.md``) that an App-level label
change is judged against the TIGHTEST of the eleven footer-bearing screens --
and simultaneously demonstrated that the answer moved (EditScreen ->
BrowseScreen) with nothing failing to signal it. Before this ticket, only
three screens (capture, browse, edit) carried a 100-column footer-width
test, each a hand-copied ``_drive()``/``consumed`` harness -- the other eight
could silently overflow the day the next App-level binding lands, and the
next ticket would have to re-measure by hand and trust a comment.

## The corpus, derived not hand-listed

:func:`_footer_bearing_screen_classes` walks :mod:`lode.tui.screens` the same
mechanical way ``tests/test_tui_app.py``'s
``test_no_screen_module_imports_the_stock_footer`` does -- module-by-module
via ``pkgutil``, then the same **object-identity** test on the module's own
namespace -- except it looks for the OPPOSITE signal: ``module.LodeFooter is
LodeFooter`` rather than ``module.Footer is Footer``. Identity, deliberately,
and not a source-text scan for ``"LodeFooter()"`` inside ``compose``: a text
scan silently drops a screen the day it writes ``LodeFooter(id=...)``, hoists
the footer into a shared chrome helper, or inherits ``compose`` from a base
class -- reopening exactly the silent-gap failure mode this ticket exists to
close. Identity fails in the safe direction instead: a module that imports
``LodeFooter`` without composing one is over-discovered, and over-discovery
is loud (see below), never silent.

A screen that starts composing a footer is picked up automatically, with no
edit to this file, the moment it lands -- and if this file has no factory
registered for it yet (see :data:`_FACTORIES` below),
:func:`test_discovered_screens_and_factories_agree` fails LOUDLY naming it,
rather than the corpus scan silently walking past it the way the three old
hand-copied tests did for the other eight screens. That test is bidirectional,
so the reverse -- a ``_FACTORIES`` entry the walk no longer discovers
(renamed, footer removed, module deleted) -- is caught in the same breath.

## Building each screen

Each of the eleven needs different constructor args (some none, some a
``note_id``, one a whole ``Conflict``), so there is no way around a per-class
factory -- what's derived is the *membership* of the corpus, not how to
build each member. :data:`_FACTORIES` maps every discovered class to a
``(ctx) -> Screen`` callable; :class:`_Ctx` bundles the one shared seed (a
saved note, its head version, a snapshot, a CAS conflict) every factory below
draws from, built once per DB in :func:`_seed`.

## Precedent

Same shape as ``tests/test_cli_help_corpus_gate.py`` (lode-ii25.9): scan a
corpus, assert one gate over every member, fail loudly rather than skip on
an unrecognised new one.
"""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from textual.screen import Screen
from textual.widgets import Footer
from textual.widgets._footer import FooterKey

import lode.tui.screens
from lode.externals import ingest_snapshot
from lode.storage import init_db
from lode.tui.app import LodeApp
from lode.tui.screens.ask import AskScreen
from lode.tui.screens.browse import BrowseScreen
from lode.tui.screens.capture import CaptureScreen
from lode.tui.screens.config import ConfigScreen
from lode.tui.screens.edit import EditScreen
from lode.tui.screens.external_picker import ExternalPickerScreen
from lode.tui.screens.reconcile import ReconcileScreen
from lode.tui.screens.snapshot_viewer import SnapshotViewerScreen
from lode.tui.screens.tags import TagsScreen
from lode.tui.screens.version_history import VersionHistoryScreen
from lode.tui.screens.version_view import VersionViewScreen
from lode.tui.services.reconcile import Conflict, write_draft
from lode.tui.widgets.lode_footer import LodeFooter
from lode.versions import save


def _footer_bearing_screen_classes() -> dict[str, type[Screen]]:
    """Every ``Screen`` subclass defined directly in a ``lode.tui.screens``
    module that imports :class:`LodeFooter` -- keyed by ``module.ClassName``
    for a stable, readable parametrize id.

    The membership signal is object identity on the module namespace, not a
    source-text scan (see this file's docstring for why). ``docs/conventions``'
    one-Screen-per-module fiat is what makes "the module imports it" and "this
    screen composes it" the same statement; a module that imported it without
    composing one would be over-discovered, and over-discovery fails loudly in
    :func:`test_discovered_screens_and_factories_agree` rather than silently.
    """
    found: dict[str, type[Screen]] = {}
    for info in pkgutil.iter_modules(lode.tui.screens.__path__):
        module = importlib.import_module(f"lode.tui.screens.{info.name}")
        if getattr(module, "LodeFooter", None) is not LodeFooter:
            continue
        for name, obj in vars(module).items():
            if (
                isinstance(obj, type)
                and issubclass(obj, Screen)
                and obj.__module__ == module.__name__
            ):
                found[f"{info.name}.{name}"] = obj
    return found


@dataclass
class _Ctx:
    """The one shared seed every factory below draws from."""

    db_path: Path
    note_id: str
    version_id: str
    snapshot_id: str
    conflict: Conflict


def _seed(db_path: Path) -> _Ctx:
    conn = init_db(db_path)
    try:
        version_id = save(conn, "note-a", "hello world").version_id
        # ingest_snapshot, not hand-rolled INSERTs: it owns the externals-row
        # creation, the content-addressed snapshot_id, and the head move, so
        # SnapshotViewerScreen is driven against the row shape production
        # actually writes rather than a fabricated "snap-1".
        ingested = ingest_snapshot(conn, "https://example.com/x", "web", "body text")
        assert ingested is not None
        snapshot_id = ingested.snapshot_id
    finally:
        conn.close()
    draft_path = write_draft(db_path, "note-a", "an unsaved edit")
    conflict = Conflict(
        note_id="note-a",
        expected_parent="some-stale-parent",
        rejected_buffer="an unsaved edit",
        actual_head=version_id,
        actual_head_body="hello world",
        draft_path=draft_path,
    )
    return _Ctx(
        db_path=db_path,
        note_id="note-a",
        version_id=version_id,
        snapshot_id=snapshot_id,
        conflict=conflict,
    )


#: One factory per discovered footer-bearing screen class. A screen that
#: starts composing a LodeFooter with no entry here fails
#: ``test_discovered_screens_and_factories_agree`` below -- loudly, naming the
#: class -- rather than being silently unscanned.
_FACTORIES: dict[type[Screen], Callable[[_Ctx], Screen]] = {
    CaptureScreen: lambda ctx: CaptureScreen(),
    ConfigScreen: lambda ctx: ConfigScreen(),
    AskScreen: lambda ctx: AskScreen(),
    ReconcileScreen: lambda ctx: ReconcileScreen(ctx.conflict),
    BrowseScreen: lambda ctx: BrowseScreen(),
    TagsScreen: lambda ctx: TagsScreen(),
    EditScreen: lambda ctx: EditScreen(ctx.note_id),
    VersionHistoryScreen: lambda ctx: VersionHistoryScreen(ctx.note_id),
    VersionViewScreen: lambda ctx: VersionViewScreen(ctx.note_id, ctx.version_id),
    SnapshotViewerScreen: lambda ctx: SnapshotViewerScreen(ctx.snapshot_id),
    ExternalPickerScreen: lambda ctx: ExternalPickerScreen([]),
}

_DISCOVERED = _footer_bearing_screen_classes()


def test_discovered_screens_and_factories_agree() -> None:
    """Non-vacuity + the drift gate itself, in both directions.

    Forward: a new screen composing LodeFooter with no :data:`_FACTORIES`
    entry is caught HERE, not by the parametrized scan below silently having
    nothing to say about it. Reverse: an entry the walk no longer discovers
    (renamed, footer removed, module deleted, or the walk itself broken) is a
    stale entry -- the same hygiene ``test_cli_help_corpus_gate.py`` enforces
    for its own allowlist.
    """
    assert _DISCOVERED, (
        "no footer-bearing screens discovered -- the walk itself is broken"
    )
    missing = sorted(
        qualname for qualname, cls in _DISCOVERED.items() if cls not in _FACTORIES
    )
    assert not missing, (
        f"discovered footer-bearing screen(s) with no factory registered in "
        f"tests/test_tui_footer_width_corpus.py's _FACTORIES: {missing} -- "
        "add one so the 100-column scan covers it"
    )
    stale = sorted(
        cls.__name__ for cls in _FACTORIES if cls not in set(_DISCOVERED.values())
    )
    assert not stale, f"stale _FACTORIES entries, no longer discovered: {stale}"


@pytest.mark.parametrize(
    "screen_cls",
    [cls for _, cls in sorted(_DISCOVERED.items())],
    ids=sorted(_DISCOVERED),
)
def test_footer_bearing_screen_fits_100_columns(
    tmp_path: Path, screen_cls: type[Screen]
) -> None:
    factory = _FACTORIES[screen_cls]
    db_path = tmp_path / "lode.db"
    ctx = _seed(db_path)
    app = LodeApp(db_path=db_path)

    async def _drive() -> tuple[bool, int, list[str]]:
        async with app.run_test(size=(100, 24)) as pilot:
            app.push_screen(factory(ctx))
            await pilot.pause()
            footer = app.screen.query_one(Footer)
            keys = [c for c in footer.children if isinstance(c, FooterKey)]
            descriptions = [c.description for c in keys]
            # Natural width, immune to the gutter-squeeze trap documented in
            # tests/test_tui_app.py's capture-footer test comment block:
            # show_horizontal_scrollbar alone is necessary but not
            # sufficient, since Textual can squeeze 1-column gutters to 0 to
            # absorb a small overflow while still reporting hscroll=False.
            consumed = sum(k.region.width for k in keys) + (len(keys) - 1)
            return footer.show_horizontal_scrollbar, consumed, descriptions

    has_hscroll, consumed, descriptions = asyncio.run(_drive())

    assert has_hscroll is False, (
        f"{screen_cls.__name__} footer overflows: {descriptions}"
    )
    assert consumed <= 100, (
        f"{screen_cls.__name__} footer really consumes {consumed}/100 columns: "
        f"{descriptions}"
    )


def test_no_screen_module_missed_by_pkgutil() -> None:
    """Sanity that the ``pkgutil`` walk reaches every screen module on disk,
    not a subset -- mirrors ``tests/test_cli_help_corpus_gate.py``'s own
    non-vacuity check. Derived from the directory listing rather than a
    hand-typed screen list: the *membership* of the footer-bearing corpus is
    already pinned bidirectionally against ``_FACTORIES`` above, so re-typing
    those names here would be the hand-maintenance this file exists to end.
    """
    walked = {info.name for info in pkgutil.iter_modules(lode.tui.screens.__path__)}
    on_disk = {
        path.stem
        for path in Path(lode.tui.screens.__path__[0]).glob("*.py")
        if path.stem != "__init__"
    }
    assert walked == on_disk, (
        f"pkgutil walk disagrees with the screens directory: "
        f"missed {sorted(on_disk - walked)}, invented {sorted(walked - on_disk)}"
    )
