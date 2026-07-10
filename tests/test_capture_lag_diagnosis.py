"""Lag-diagnosis spike (lode-0wj.2): does the passive related-notes pass block typing?

Feedback said typing in the capture screen feels laggy. The prime suspect
(``docs/decisions.md`` / the lode-0wj debate) was the passive related-notes pass
(:mod:`lode.tui.related`, wired into :mod:`lode.tui.screens.capture`): it is
*already* off the UI thread via ``asyncio.to_thread`` + a Textual
``@work(exclusive=True)`` worker, so on paper it should not block input. The one
mechanism that would make it block anyway: ``asyncio.to_thread`` only relieves
the event loop if the offloaded call actually releases the GIL while it runs — if
``fastembed``'s ONNX inference (or the SQLite/LanceDB calls alongside it) holds
the GIL, the "background" pass stalls the UI thread despite running "off" it.

This module is the spike's DATA: it reproduces the pass against a REALISTIC
seeded corpus (the lode-5y8.4 fixture, ``lode.eval.seed`` — an empty/small DB
would make the pass return instantly and hide exactly the effect being measured),
using the REAL ``fastembed``/ONNX embedder (not the offline hash stub the eval
*scorer* uses, since that stub is deliberately model-free and would hide the one
question this spike exists to answer). No behaviour change: this only observes
the landed pipeline through its existing public seams
(:func:`lode.tui.related.find_related_notes`, :class:`lode.embedding.FastEmbedEmbedder`).

**Pass/fail target.** ``LATENCY_TARGET_P95_MS`` (100ms) is this spike's keystroke
-> render latency target: the standard "feels instant" interactive-response
threshold (Nielsen's response-time limits). It is measured as **event-loop tick
lag** while the pass runs — how much longer a fixed-interval ``asyncio.sleep`` on
the same loop Textual's key handling/repaint run on comes back late by. That is a
direct proxy for keystroke->render latency: Textual's key handling is just
another coroutine on that same loop, so a tick delayed by N ms is a keystroke
delayed by roughly N ms too. This is a *closer* proxy for what a user feels than
timing ``find_related_notes`` itself: if the GIL is released, the pass can take
hundreds of ms without a single delayed keystroke; if the GIL is held, even a
short pass stalls the loop for its duration.

Downloads the real pinned embedding model on first run and takes real wall-clock
time (whole-corpus embed + repeated inference), so this spike is **opt-in**: set
``LODE_DIAGNOSE_LAG=1`` to run it (mirrors ``tests/test_models_smoke.py``'s
``LODE_SMOKE_MODELS`` convention). Run with ``-s`` to see the printed findings::

    LODE_DIAGNOSE_LAG=1 pytest -s tests/test_capture_lag_diagnosis.py

**Findings are recorded on lode-0wj.2** (bd ticket notes), not just here — this
module is the reproduction, not the record.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

import pytest

from lode.config import Settings, lance_dir
from lode.embedding import EmbeddingCacheBackend, FastEmbedEmbedder
from lode.eval.seed import seed_notes
from lode.lexical import LexicalCacheBackend
from lode.repository import CompositeCache, Repository
from lode.storage import init_db
from lode.tui.related import find_related_notes

pytestmark = pytest.mark.skipif(
    os.environ.get("LODE_DIAGNOSE_LAG") != "1",
    reason="lag-diagnosis spike (lode-0wj.2): downloads the real embedder and "
    "takes real wall-clock time; set LODE_DIAGNOSE_LAG=1 to run",
)

#: This spike's pass/fail target for keystroke -> render latency (p95), measured
#: as event-loop tick lag (see the module docstring). 100ms is the standard
#: "feels instant" interactive-response threshold.
LATENCY_TARGET_P95_MS = 100.0

#: Heartbeat tick interval for the event-loop-lag probe below. Finer than
#: lode.tui.latency_probe.HEARTBEAT_INTERVAL_S (50ms): a single
#: find_related_notes call or embed_query call can finish in well under 50ms
#: (see the measured costs this spike found), which at a 50ms tick yields 0-1
#: samples per run -- not enough to trust a p95 off of. 10ms plus repeating the
#: workload (below) gives a real sample count.
_TICK_S = 0.01

#: How many times each probed workload repeats inside its timed window, so the
#: heartbeat accumulates enough ticks (~total time / _TICK_S) regardless of how
#: fast any single call is -- this is also a closer model of a real typing
#: session, where the debounced pass fires repeatedly, not once.
_REPEAT = 10

#: Realistic mid-typing drafts, long enough to clear
#: ``Settings.related_notes_min_chars`` and topically overlap the seed corpus
#: (``src/lode/eval/corpus/*.md``) so the pipeline does real retrieval work
#: rather than short-circuiting on an empty result set.
_DRAFTS = [
    "debugging the postgres autovacuum settings again, seeing bloat on the orders table",
    "investigating a checkout latency spike, might be related to the incident last month",
    "how do kubernetes readiness probes interact with the load balancer during a rollout",
    "git bisect is taking forever on this repo, need a better strategy for next time",
    "renewing the tls cert before it expires, want to automate this so it never happens again",
]


def _percentile(values: list[float], pct: float) -> float:
    """The ``pct`` percentile (0-100) of ``values`` via nearest-rank; ``values`` non-empty."""
    ordered = sorted(values)
    idx = min(int(len(ordered) * pct / 100), len(ordered) - 1)
    return ordered[idx]


def _build_seeded_store(db_path: Path, *, settings: Settings) -> None:
    """Load the lode-5y8.4 seed corpus into a fresh store, REAL embedder, both legs live.

    Same Repository + CompositeCache save path production drives (mirrors
    :func:`lode.eval.harness._build_seed_store`), but wired to the real
    :class:`~lode.embedding.FastEmbedEmbedder` rather than the eval scorer's
    offline hash stub — this spike's whole question is the cost and GIL
    behaviour of the real ONNX inference that stub deliberately avoids paying.
    """
    conn = init_db(db_path)
    try:
        repo = Repository(
            conn,
            CompositeCache(
                [
                    LexicalCacheBackend(conn, settings=settings),
                    EmbeddingCacheBackend(
                        conn,
                        lance_dir=lance_dir(db_path),
                        embedder=FastEmbedEmbedder(settings),
                        settings=settings,
                    ),
                ]
            ),
        )
        for note in seed_notes():
            repo.save(note.note_id, note.body, settings=settings)
    finally:
        conn.close()


async def _run_with_heartbeat(
    coro: asyncio.Future | asyncio.Task,
) -> tuple[object, list[float]]:
    """Run ``coro`` to completion while sampling event-loop tick lag every ``_TICK_S``.

    Each sample is how much *longer* a ``_TICK_S`` sleep actually took than
    requested — the same signal a blocked/starved event loop would inflict on a
    keystroke's render (Textual's key handling and repaint are just another
    coroutine on this same loop). If the loop is free, sleeps return on time and
    lag stays near zero; if something sharing this thread pool starves the GIL
    out from under it, sleeps (and therefore keystrokes) run late by the same
    amount.
    """
    lags: list[float] = []
    stop = False

    async def _heartbeat() -> None:
        while not stop:
            start = time.perf_counter()
            await asyncio.sleep(_TICK_S)
            lags.append(max(time.perf_counter() - start - _TICK_S, 0.0) * 1000)

    heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        result = await coro
    finally:
        stop = True
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
    return result, lags


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A realistic seeded store (lode-5y8.4's 10-note corpus), real embedder + LanceDB.

    Module-scoped: the related-notes pass never writes, so building the store
    once keeps the spike's total wall time down (embedding the corpus is the
    slow part).
    """
    db_path = tmp_path_factory.mktemp("lag-diagnosis") / "lode.db"
    _build_seeded_store(db_path, settings=Settings())
    return db_path


@pytest.fixture(scope="module")
def warm_embedder() -> FastEmbedEmbedder:
    """A single loaded embedder, reused across the timed experiments below.

    ``FastEmbedEmbedder`` lazily loads the ONNX model on first call (model
    download/graph load); doing that once here keeps that one-time cost out of
    every timed experiment's numbers.
    """
    embedder = FastEmbedEmbedder(Settings())
    embedder.embed_query("warm up the onnx runtime")
    return embedder


# --- Experiment B: the MUST-ANSWER question -- does the pass yield the loop? -------


def test_event_loop_lag_during_related_notes_pass(
    seeded_db: Path, warm_embedder: FastEmbedEmbedder
) -> None:
    """THE load-bearing question: does the passive pass actually yield the event loop?

    Reproduces ``RelatedNotesPanel._search_related`` (lode-aoc; originally
    capture.py's, before that extraction) exactly: ``asyncio.to_thread``
    wrapping the full ``find_related_notes`` call (FTS5 + the ONNX embedder +
    LanceDB, the whole pass a real keystroke would trigger) on an asyncio event
    loop, while a heartbeat samples loop tick lag throughout. The pass repeats
    ``_REPEAT`` times (a burst of debounced passes, as a real typing session
    would produce) so the heartbeat accumulates enough ticks to trust a p95 off
    of — a single ~65ms pass at a 10ms tick yields too few samples. Compared
    against an idle baseline (the loop with nothing running) to separate real
    starvation from ordinary scheduler jitter.
    """
    settings = Settings()
    draft = _DRAFTS[1]  # topically overlaps the seeded incident/latency note

    async def _idle() -> None:
        for _ in range(_REPEAT):
            await asyncio.sleep(0.05)

    async def _repeated_passes() -> None:
        for _ in range(_REPEAT):
            await asyncio.to_thread(
                find_related_notes,
                seeded_db,
                draft,
                settings=settings,
                embedder=warm_embedder,
            )

    _, lags_idle = asyncio.run(_run_with_heartbeat(_idle()))
    _, lags_during = asyncio.run(_run_with_heartbeat(_repeated_passes()))

    p95_idle = _percentile(lags_idle, 95) if lags_idle else 0.0
    p95_during = _percentile(lags_during, 95) if lags_during else 0.0
    gil_released = p95_during < LATENCY_TARGET_P95_MS

    print(
        f"\n[lode-0wj.2] event-loop tick lag (keystroke->render proxy), "
        f"{len(lags_during)} ticks during the pass vs {len(lags_idle)} idle: "
        f"idle p95={p95_idle:.1f}ms max={max(lags_idle, default=0.0):.1f}ms | "
        f"during-pass p95={p95_during:.1f}ms max={max(lags_during, default=0.0):.1f}ms "
        f"(target p95 < {LATENCY_TARGET_P95_MS:.0f}ms)"
    )
    print(
        f"[lode-0wj.2] VERDICT (full find_related_notes via asyncio.to_thread): "
        f"the pass {'DOES' if gil_released else 'does NOT'} yield the event loop "
        f"-- a keystroke during this pass would "
        f"{'render on time' if gil_released else 'visibly stall'}"
    )

    assert p95_during < LATENCY_TARGET_P95_MS, (
        f"event-loop lag during the related-notes pass (p95={p95_during:.1f}ms) "
        f"exceeds the {LATENCY_TARGET_P95_MS:.0f}ms keystroke->render target -- "
        "asyncio.to_thread is not relieving input during this pass"
    )


def test_event_loop_lag_isolated_to_onnx_embed_call(
    warm_embedder: FastEmbedEmbedder,
) -> None:
    """Surgical version of the question above: isolate JUST the ONNX embed call.

    The full pass above also does SQLite (FTS5) and LanceDB work alongside the
    embed call, so a lag finding there doesn't by itself pin the GIL behaviour on
    ``fastembed``/ONNX specifically (the acceptance criterion's literal wording).
    This isolates ``embedder.embed_query`` alone via the same
    ``asyncio.to_thread`` + heartbeat harness, no DB/LanceDB involved, repeated
    (see ``_REPEAT``) so the heartbeat gets enough ticks to trust a p95 off of --
    one embed call alone is faster than a single tick.
    """
    draft = _DRAFTS[1]
    repeats = _REPEAT * 3  # a single embed call is much shorter than one pass

    async def _idle() -> None:
        for _ in range(_REPEAT):
            await asyncio.sleep(0.05)

    async def _repeated_embeds() -> None:
        for _ in range(repeats):
            await asyncio.to_thread(warm_embedder.embed_query, draft)

    _, lags_idle = asyncio.run(_run_with_heartbeat(_idle()))
    _, lags_during = asyncio.run(_run_with_heartbeat(_repeated_embeds()))

    p95_idle = _percentile(lags_idle, 95) if lags_idle else 0.0
    p95_during = _percentile(lags_during, 95) if lags_during else 0.0
    gil_released = p95_during < LATENCY_TARGET_P95_MS

    print(
        f"\n[lode-0wj.2] event-loop tick lag isolated to embed_query alone, "
        f"{len(lags_during)} ticks during vs {len(lags_idle)} idle: "
        f"idle p95={p95_idle:.1f}ms | during-embed p95={p95_during:.1f}ms "
        f"max={max(lags_during, default=0.0):.1f}ms "
        f"(target p95 < {LATENCY_TARGET_P95_MS:.0f}ms)"
    )
    print(
        f"[lode-0wj.2] VERDICT (fastembed/ONNX embed_query alone): "
        f"the ONNX call {'DOES' if gil_released else 'does NOT'} release the GIL "
        "during inference"
    )

    assert p95_during < LATENCY_TARGET_P95_MS, (
        f"event-loop lag isolated to the ONNX embed call (p95={p95_during:.1f}ms) "
        f"exceeds the {LATENCY_TARGET_P95_MS:.0f}ms target -- fastembed/ONNX is "
        "holding the GIL during inference, so asyncio.to_thread does not relieve "
        "typing while it runs"
    )
