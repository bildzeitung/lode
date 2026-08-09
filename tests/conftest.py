"""Shared test fixtures.

The CLI resolves all on-disk state (DB, vector store, logs) under ``$LODE_HOME``
(default ``~/.lode``, lode-qd9). An autouse fixture pins ``$LODE_HOME`` to a
throwaway per-test directory so a test invocation never reads or writes the real
``~/.lode`` — in particular the log file handler the group callback attaches lands
in tmp, not the developer's home. Tests that exercise the default-path behaviour
explicitly still get an isolated, asserted-against directory. That no-touch rule
holds for every piece of lode's *state*; it has exactly one deliberate exception,
the model-weights cache, for the reason below.

**The model-weights cache is the one thing that must NOT be isolated (lode-gmo).**
``$LODE_HOME/models/`` is where every ``fastembed`` loader now caches its ONNX
weights (:func:`lode.config.model_cache_dir`). The slow tier deliberately loads a
*real*, un-mocked ``FastEmbedCrossEncoder`` (see below), so if the throwaway root
carried the weights cache too, every real load would face an empty directory: the
landing gate (``nox -s tests``, which runs the slow tier) would re-download
``BAAI/bge-reranker-base`` from HuggingFace on *every run*, and fail outright with
no network. So the fixture resolves the machine's real cache *before* it overrides
``$LODE_HOME`` and links the throwaway root's ``models/`` at it — the DB, vectors,
and logs stay isolated per test, while the weights are downloaded once per machine
and shared with production.

**A network-touching test must FAIL, never pass silently (lode-85q).** Surfaced by
lode-8xg: a test whose mock was silently a no-op (it patched a symbol ``cli.py``
doesn't have) still reached the *real* ``enrich_version`` -> ``anthropic.Anthropic``
path, and the test still passed. That happens in both directions and neither one
reports "this test touched the network":

- **unkeyed** (CI, no ``ANTHROPIC_API_KEY``): ``anthropic.Anthropic()`` raises
  ``anthropic.AnthropicError`` at *construction* — its own ``active_config``
  profile lookup fails before a socket is ever opened — and
  ``lode.worker.run_one``'s ``except Exception`` (a legitimate, broad job-failure
  handler, not a bug this ticket fixes) swallows it as an ordinary job failure.
  A socket block alone would never fire here: no socket is ever opened.
- **keyed** (a dev machine with the key exported): construction succeeds and a
  real, billed Haiku/Sonnet call goes out.

``_block_unmocked_network_and_llm_access`` below closes both gaps with two
independent guards. Each one **records** the violation to a process-global list
*and* raises ``pytest.fail(...)``, and an autouse teardown check fails the test
if anything was left on that list. The two halves are deliberately redundant;
the redundancy is the whole point (lode-sx17), so read them as one mechanism:

* The **raise** stops the test where it happens, with a traceback pointing at
  the offending line. ``pytest.fail`` raises ``_pytest.outcomes.Failed``, a
  ``BaseException`` (**not** an ``Exception``) subclass, chosen so it blows
  straight through ``run_one``'s ``except Exception`` rather than being
  swallowed as just another job failure.
* The **record** is what makes the guard hold when the raise never reaches
  pytest at all — see "Why the raise alone is not enough" below.

The two guards:

1. **LLM-client construction** — patches ``anthropic.Anthropic.__init__``
   (+ ``AsyncAnthropic`` if present) to fail unconditionally, before the SDK's own
   credential-chain logic runs, so it fires identically whether the environment is
   keyed or not. Also patches ``openai.OpenAI.__init__`` and
   ``openai.AzureOpenAI.__init__`` the same way (lode-568v.3): a test that
   reaches the real ``OpenAIProvider`` construction path with no fake installed
   must fail the same way an unmocked Anthropic path does, not silently
   construct a real client.
2. **Generic outbound socket egress** — patches ``socket.socket.connect`` *and*
   ``socket.socket.connect_ex`` (independent C methods; the latter does not call
   the former, so guarding only ``connect`` would leave the guard failing open)
   to fail on any non-loopback destination. Catches accidental egress through
   anything other than the Anthropic SDK — e.g. a broken ``webfetch`` mock.
   Verified empirically to fire on ``httpx`` (sync *and* async), ``urllib``,
   ``socket.create_connection`` and ``asyncio.open_connection``: they all bottom
   out in a plain ``socket.connect`` (for HTTPS, ``httpcore`` connects the TCP
   socket first and only then wraps it in TLS, so ``ssl.SSLSocket.connect`` is
   never reached).

**Why the raise alone is not enough (lode-sx17).** Until this ticket the guard
rested entirely on ``Failed`` being a ``BaseException``, and that turned out to
be luck rather than design in two distinct ways:

1. **A third-party best-effort swallow.** ``huggingface_hub``'s
   ``utils/_detect_agent.py`` fetches an agent-harness registry from the Hub
   while *building the headers for every Hub request*
   (``utils/_headers.py``'s ``build_hf_headers`` ->
   ``_http_user_agent``), and wraps the whole load in ``except Exception`` —
   its module docstring states outright that "detection must never make a
   process fail" — with a *second* ``except Exception`` inside the fetch
   itself. A ``BaseException`` clears both today, but nothing on their side is
   load-bearing for us: that library is one ``except BaseException`` away from
   making the egress permanently **invisible** rather than merely non-failing,
   and so is any other dependency with a broad best-effort handler. lode does
   not get to pick their except clauses. (That *particular* egress is also cut
   at its source now — see ``HF_HUB_DISABLE_TELEMETRY`` below — but cutting one
   known site is not a mechanism, and the next one will not be announced.)
2. **A connect made off the main thread.** ``pytest.fail`` in a non-main thread
   does not propagate to the test, so the raise prevents the call but reports
   nothing. lode **does** make such calls — this docstring previously claimed
   it did not, which was already false when written: a Textual worker reaching
   ``asyncio.to_thread`` in the related-notes panel produced exactly this, a
   bare "Task exception was never retrieved" block on stderr that failed no
   test (lode-fr3p, lode-7ypf).

The record closes both: it is appended at the moment ``connect`` is
intercepted, before anything downstream can decide what to do with the
exception, and the teardown check reads it regardless of which thread appended
it or what swallowed the raise.

   Known limits, still accepted: a connect made in a **subprocess** is out of
   reach (separate interpreter, separate list). And a record appended *after*
   its own test's teardown — a straggler worker outliving the test that started
   it — is attributed to whichever test is running when it is next checked, so
   every recorded message carries the stack captured at interception time; read
   that stack, not the test name, to find the caller. The guard is a net for
   *accidents*, not an adversary.

**Deliberately tripping the guard: ``@pytest.mark.trips_network_guard``.** A
test that *asserts on the guard's own behaviour* (tests/test_network_guard.py)
trips it on purpose and catches the raise, which would otherwise leave a record
and fail in teardown. That marker consumes the record. It is checked for
staleness in both directions: a marked test that records **nothing** also
fails, because a marker that has stopped being needed silently disables the
teardown backstop for that test.

**Escape hatch (explicit, greppable): ``@pytest.mark.network``** (registered in
``pyproject.toml``) lifts *both* guards for a test that deliberately needs real
network access **or** — the more common case — a real, un-mocked
``anthropic.Anthropic()`` construction that never opens a socket, which guard 1
cannot tell apart from a mock that broke. For the current set, grep rather than
trusting a list here (a hand-maintained inventory silently goes stale, and a
wrong count is worse than no count)::

    grep -rn "@pytest.mark.network" tests/

``@pytest.mark.slow`` additionally lifts *only* guard 2 (not guard 1): the
real-reranker-model-load tier (lode-pql/lode-gmo, see above) may need one HF Hub
download on a cold model cache, a pre-existing, accepted exception this fixture
must not regress — every ``slow`` test already mocks its own Anthropic/QA client,
so guard 1 still protects it.

That relaxation is the guard's **widest residual hole, and it is deliberate**: a
``slow`` test may connect to *any* host, not merely HuggingFace. Host-allowlisting
is not available to us — by the time ``connect()`` is reached the destination has
already been resolved to a bare IP, so there is no hostname left to match on. The
hole is bounded by keeping the ``slow`` tier small (a handful of tests, all of
which load a real model on purpose) and by guard 1 still covering every one of
them. Do not reach for ``slow`` as a way to quiet guard 2 on a test that is not
about a real model load — use ``@pytest.mark.network``, which is greppable and
says what it means.

**Two known egress sources are also cut at the source, so the guard never has
to catch them.** Neither is a substitute for the guard: removing an egress
beats catching it, and the guard still fails anything either one misses.

1. **``HF_HUB_DISABLE_TELEMETRY`` (lode-sx17)** — set process-wide at module
   level below, killing huggingface_hub's agent-harness registry fetch. See
   that assignment's comment for why this env var and not ``HF_HUB_OFFLINE``,
   and why process-wide rather than inside the autouse fixture.
2. **The autouse offline query embedder (lode-7ypf)** —
   :func:`_stub_the_query_embedder_offline` replaces
   :class:`lode.embedding.FastEmbedEmbedder` for every test the socket guard
   polices. The real one downloads/loads the actual ONNX weights on first use,
   and the related-notes panel constructs it from a debounced background worker
   — so any TUI test that put text in a body ``TextArea`` without stubbing armed
   a live call that outlived its own test. (Until lode-dj6m that first
   ``embed_query`` *also* resolved the HuggingFace revision over the network,
   warm cache or not; that probe is now off the query-only path, but the weights
   load alone still justifies the stub.)
   ``@pytest.mark.real_embedder`` opts back out.
"""

import ast
import asyncio
import importlib.util
import ipaddress
import logging
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from textual.pilot import Pilot

import lode
from lode import jobs
from lode.config import model_cache_dir

#: Imported eagerly, not deferred behind TYPE_CHECKING (lode-t402): on this repo's Python
#: (>=3.14, PEP 649/749) annotations are evaluated LAZILY by default, so
#: ``_make_batch_result``'s ``enrichment: EnrichmentResult`` parameter annotation would NOT
#: force this import at def time even if it were deferred -- that is not why it is eager.
#: It stays eager because a deferred import buys nothing here: MEASURED incrementally on top
#: of the ``import lode`` / ``from lode import jobs`` / ``from lode.config import
#: model_cache_dir`` this file already does above, ``import lode.enrich`` costs ~9ms and
#: 5 additional modules (``lode.curation``, ``lode.egress``, ``lode.ids``, ``lode.redact``,
#: itself) -- negligible, because the expensive part of its import graph (pydantic and
#: friends) is already resident from the baseline imports. Contrast ``lode.webfetch``/
#: ``lode.tool_dispatch`` below, which are NOT already resident -- see that block for the
#: cost that earns those two a TYPE_CHECKING-only import. This DOES make
#: ``lode.enrich`` resident from collection onward -- which is safe, and does not weaken
#: lode-4q97: the tests asserting an embed-only drain never imports the SDK go through the
#: ``forget_sdk_imports`` fixture, whose whole purpose is evicting this graph first (see its
#: docstring). ``lode.enrich`` itself keeps ``import anthropic`` deferred, so the SDK is
#: still not pulled at collection -- verified, not assumed.
from lode.enrich import EnrichmentResult

#: NOT imported at runtime, deliberately (lode-pw9o): ``lode.tool_dispatch`` and
#: ``lode.webfetch`` are needed only by ``fake_tool_turn_client`` below, and importing them
#: at module scope costs ~0.8s and ~440 modules (``trafilatura`` -> ``lxml``/``justext``/
#: ``htmldate``/``dateparser``, plus ``numpy``/``pyarrow``) at COLLECTION, in the one conftest
#: every test module loads -- and again in each xdist worker. Measured, not assumed. They are
#: imported inside the builder instead. The TYPE_CHECKING import below is free at runtime:
#: on this repo's Python (>=3.14, PEP 649) annotations are evaluated lazily, so
#: ``StubWebFetcher``'s ``RawResponse`` annotations never resolve the name unless something
#: actually asks for them.
if TYPE_CHECKING:
    from lode.webfetch import RawResponse

#: lode-kq4v: scrub ambient colour/tty-forcing env vars BEFORE any test module can import
#: ``lode.cli`` and construct its shared ``console``/``err_console`` (see that module's
#: ``console`` docstring, and lode-xgaa). An ambient ``FORCE_COLOR`` in the shell that launched
#: pytest silently reddens every test asserting plain, uncoloured CLI output, on an
#: otherwise-unmodified tree -- OBSERVED landing a real /land pass (lode-kq4v).
#:
#: MECHANISM -- CANONICAL (lode-qv91): ``docs/stack.md``'s ``rich`` row, ``src/lode/cli/__init__.py``'s
#: ``console`` docstring and ``tests/test_cli_console.py``'s module docstring all point HERE
#: rather than restating this, so keep it here and keep it precise.
#: Verified by execution against the installed rich (15.0.0), and stated precisely
#: because the loose version ("``Console()`` freezes its TTY check at construction") invites
#: exactly the wrong simplification. ``is_terminal`` is NOT frozen: it is a live property
#: (``rich/console.py``:931) that re-reads ``os.environ`` on every access. What IS frozen is
#: ``_color_system`` -- computed once in ``Console.__init__`` (:708-712) FROM ``is_terminal``,
#: surfaced by the ``color_system`` property (:909) -- and ``color_system`` is what gates whether
#: any ANSI *style* is emitted. ``no_color`` and ``is_interactive`` are frozen too, as plain
#: instance attributes. ``is_terminal`` staying live is not inert, so do not shorten this to
#: "``is_terminal`` no longer matters": ``_render_buffer`` re-reads it on every write (:2138) to
#: decide whether CONTROL segments are emitted, and ``show_cursor``/``set_window_title`` (:1196,
#: :1258) gate on it too.
#:
#: THAT is why this must be top-level module code and NOT an autouse fixture. A fixture runs at
#: test SETUP, after collection has already imported every test module. Scrubbing there does
#: flip ``console.is_terminal`` back to ``False`` -- which is precisely the trap: it LOOKS like
#: it worked, while ``console.color_system`` stays pinned at ``truecolor`` from import and ANSI
#: keeps flowing. Same shape as the ``monkeypatch.setenv("NO_COLOR", "1")`` no-op lode-xgaa
#: documented. ``conftest.py`` is always imported before any test module in its directory
#: (pytest's collection order), including once per ``pytest-xdist`` worker process -- which is
#: what makes module level early enough (verified under ``-n 8``, the landing gate's config).
#:
#: ORDERING CONSTRAINT, for whoever edits the import block above: ruff keeps imports first, so
#: this scrub necessarily sits below them, and is early enough only while nothing imported above
#: constructs a rich ``Console``. True today -- none of ``lode``, ``lode.config``,
#: ``lode.jobs`` (added lode-x10m; it pulls in only ``sqlite3``) nor ``textual.pilot``
#: constructs one. Adding an import that does would
#: silently half-disable this scrub; ``tests/test_conftest_color_scrub.py`` is what would catch
#: it.
#:
#: ``CLICOLOR_FORCE`` -- named in lode-kq4v's audit requirement -- is read nowhere in this rich
#: version, so there is nothing for it to force and it is deliberately not scrubbed.
#: ``COLORTERM``/``TERM`` only choose a colour *system* once ``is_terminal`` is already ``True``,
#: so they need no scrubbing either.
#:
#: NOT CLOSED BY THIS SCRUB, deliberately (lode-kq4v acceptance A.3): ``pytest -s`` /
#: ``--capture=no`` from a REAL terminal. stdout is then a genuine tty, so ``is_terminal`` is
#: ``True`` at import with none of these four set -- confirmed under a pty -- and the same tests
#: fail. ``tests/test_cli_console.py``'s module docstring records that constraint (lode-3npn). Left unfixed ON
#: PURPOSE: the only way to force colour off is to SET ``NO_COLOR=1`` rather than clear it, which
#: would make ``test_config_output_has_no_ansi_when_piped`` vacuous -- it would then pass because
#: colour was forced off, not because the pipe was detected, which is the one thing it exists to
#: assert. ``nox -s tests`` (the landing gate) never passes ``-s``, so no gate is exposed; only a
#: human running pytest by hand is.
#:
#: What each of the four does, and why all four: ``FORCE_COLOR`` forces ``is_terminal`` True over
#: captured (non-tty) stdout -- the one that actually caused lode-kq4v; ``TTY_COMPATIBLE`` forces
#: it True on ``"1"`` and False on ``"0"``; ``TTY_INTERACTIVE`` forces ``is_interactive``.
#: ``NO_COLOR`` forces colour OFF, so it could not have caused lode-kq4v; it is cleared anyway so
#: the decision is deterministic in BOTH directions rather than ambient -- not because any test
#: here wants colour on.
#:
#: Kept as four straight ``pop`` calls rather than a loop over a tuple: commenting them out is
#: then the whole sabotage recipe ``tests/test_conftest_color_scrub.py`` documents for proving
#: itself non-vacuous. (A loop needed a ``del`` of its own control variable, and deleting only
#: the loop body left that ``del`` raising ``NameError`` -- which broke conftest import outright
#: instead of disabling the scrub, so the recipe demonstrated nothing.)
os.environ.pop("FORCE_COLOR", None)
os.environ.pop("TTY_COMPATIBLE", None)
os.environ.pop("TTY_INTERACTIVE", None)
os.environ.pop("NO_COLOR", None)

#: lode-sx17: cut huggingface_hub's agent-harness registry fetch at its SOURCE, so the guard below
#: never has to catch it. ``utils/_headers.py``'s ``_http_user_agent()`` -- reached from
#: ``build_hf_headers()``, i.e. while building the headers for EVERY Hub request -- calls
#: ``detect_agent()``, and on a cold 24h-TTL cache that
#: is a live ``GET {ENDPOINT}/api/agent-harnesses`` (``utils/_detect_agent.py``). On lode's path
#: (``resolve_model_revision`` -> ``huggingface_hub.model_info``, reached from the write path via
#: ``FastEmbedEmbedder.model_revision`` and from ``lode status``'s drift check directly -- lode-dj6m
#: moved it off the query-only ``embed_query``/``_load`` path entirely, and off ``warm()``) that
#: fetch is the FIRST socket the guard sees, ahead of ``model_info``'s own. ``http_user_agent`` skips
#: ``detect_agent()`` entirely when this is set, so the fetch never happens.
#:
#: WHY THIS VAR AND NOT ``HF_HUB_OFFLINE=1``, which would also stop it: ``HF_HUB_OFFLINE`` disables
#: the Hub outright, which breaks two things that legitimately reach it -- the ``@pytest.mark.slow``
#: reranker tier's one-time cold-cache weights download (see ``$LODE_HOME/models`` above), and any
#: ``@pytest.mark.network`` test that needs a real Hub call. ``HF_HUB_DISABLE_TELEMETRY`` costs
#: nothing functional: verified against the installed huggingface_hub (1.24.0) it is read in three
#: places, of which two are functional -- ``_headers.py``'s user-agent enrichment (the torch version
#: and the agent tag) and ``_telemetry.py``'s fire-and-forget ping -- and the third,
#: ``_runtime.py``'s ``dump_environment_info``, only prints it in the ``huggingface-cli env``
#: bug-report dump. None is an API lode or fastembed depends on. The count is stated exactly so a
#: future auditor re-running the grep matches it instead of having to re-derive whether the
#: justification still holds.
#:
#: WHY PROCESS-WIDE RATHER THAN INSIDE THE AUTOUSE GUARD (so ``@pytest.mark.network`` could lift it):
#: it CANNOT be lifted per-test even if we wanted to. ``huggingface_hub.constants`` reads the
#: environment ONCE, at import, into a module constant (``constants.py``: ``HF_HUB_DISABLE_TELEMETRY
#: = _is_true(os.environ.get(...)) or ...``), so a ``monkeypatch.setenv`` in a fixture is a no-op
#: against an already-imported hub -- the same import-time-freeze trap as the rich ``Console`` below.
#: And nothing wants it lifted: it suppresses telemetry, not Hub access, so a ``network``-marked test
#: that needs the Hub still works with it set.
#:
#: SAME ORDERING CONSTRAINT AS THE SCRUB ABOVE: this is early enough only while nothing in the import
#: block imports ``huggingface_hub`` (true today -- verified that ``lode``, ``lode.config``,
#: ``lode.jobs`` (added lode-x10m) and ``textual.pilot`` pull in neither it nor ``fastembed``).
#: If that changes, the constant freezes at
#: ``False`` before this line runs; ``tests/test_network_guard.py``'s
#: ``test_hub_telemetry_is_disabled_and_skips_the_agent_registry_fetch`` is what would catch it.
#:
#: Assigned, not ``setdefault``-ed: the value is then deterministic rather than ambient, matching the
#: scrub above. ``DISABLE_TELEMETRY`` / ``DO_NOT_TRACK`` are the same knob's other spellings and are
#: deliberately left alone -- setting one is enough, and setting three invites the reader to think
#: they do different things.
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

#: Root of the checkout that owns *this* conftest — the anchor guard 0 compares
#: against. Deliberately derived from ``__file__`` rather than ``Path.cwd()``:
#: the invariant being asserted is "the tests being collected and the ``lode``
#: being imported come from the same checkout", and ``__file__`` states that
#: directly. ``cwd`` only coincides with it when pytest happens to be invoked
#: from the repo root, so anchoring on it would false-positive on a plain
#: ``pytest tests/foo.py`` run from a subdirectory.
_CHECKOUT_ROOT = Path(__file__).resolve().parent.parent


def _wrong_source_tree_message(lode_file: str, checkout_root: Path) -> str | None:
    """Guard 0: is the imported ``lode`` from a *different* checkout than these tests?

    Returns the operator-facing message when it is, or ``None`` when all is
    well. Split out from :func:`pytest_configure` as a pure function purely so
    it is directly testable — the hook itself can only be exercised by running
    pytest under pytest.

    The hazard (lode-jh80, discovered reviewing lode-7abi): ``noxfile.py`` sets
    ``default_venv_backend = "none"``, so gates run in whatever venv is already
    active rather than provisioning one — deliberate, for speed. But
    ``scripts/python-init.sh`` always installs the local package editable
    (``-e .``, whether via the locked default path or ``--unlocked`` --
    lode-g274.1), so a venv's ``lode`` resolves to the ``src`` of *whichever
    checkout it was built in*. Activate the main checkout's venv while sitting
    in a worktree and
    pytest collects **this** checkout's ``tests/`` while importing **that**
    one's ``src``. Nothing warns, and the run reports a result for the wrong
    tree in either direction: a false FAIL when this branch's fix is never
    exercised, or a false PASS when this branch's regression is masked by the
    other checkout's already-correct code.

    The comparison is against ``<checkout_root>/src``, not ``checkout_root``
    itself, and that precision is load-bearing in both directions. Worktrees
    live *inside* the main checkout (``.claude/worktrees/`` — see CLAUDE.md),
    so a plain "is it under the checkout root?" containment test waves through
    the mirror-image mistake: sitting in the main checkout with a *worktree's*
    venv active imports ``/repo/.claude/worktrees/x/src/lode`` while collecting
    ``/repo/tests`` — still under ``/repo``, still the wrong tree, equally
    silent. Anchoring on ``src`` states the real invariant ("the ``lode`` being
    imported is the one whose source this checkout owns") as one rule instead
    of two special cases, and it also rejects a stale non-editable copy
    installed into a ``site-packages`` inside this same checkout. The repo only
    ever installs editable (``scripts/python-init.sh`` always passes ``-e``,
    locked or ``--unlocked`` -- lode-g274.1), so the ``src`` layout is the
    only shape that legitimately occurs.
    """
    expected_src = checkout_root / "src"
    resolved = Path(lode_file).resolve()
    if expected_src in resolved.parents:
        return None
    return (
        "the active venv's `import lode` resolves to\n"
        f"    {resolved}\n"
        f"which is not under the source tree of the checkout that owns these "
        f"tests ({expected_src}).\n"
        "pytest would collect this checkout's tests/ but exercise the other "
        "checkout's src -- a false FAIL if this branch's fix is never run, or "
        "a false PASS if its regression is masked by the other checkout's "
        "already-correct code (lode-jh80).\n"
        "Fix: build and activate THIS checkout's own venv --\n"
        "    ./scripts/python-init.sh && . ./venv/bin/activate"
    )


def pytest_configure(config: pytest.Config) -> None:
    """Fail the run outright if ``lode`` resolves outside this checkout (guard 0).

    Lives here rather than as a ``nox`` preflight so it covers **every** pytest
    invocation — ``nox -s tests``/``unit``/``eval`` and a bare ``pytest -k foo``
    alike — with nothing to remember to wire up per session. A per-session
    opt-in reproduces the very bug class it guards: a silent omission nothing
    catches (the first cut of lode-jh80 wired two of the three source-exercising
    sessions and missed ``eval``).

    Skipped in ``pytest-xdist`` workers (``workerinput``): the controller has
    already made this check, and letting 8 workers each raise the same
    ``UsageError`` would bury one clear message under eight copies.
    """
    if hasattr(config, "workerinput"):
        return
    message = _wrong_source_tree_message(lode.__file__, _CHECKOUT_ROOT)
    if message is not None:
        raise pytest.UsageError(message)


#: Socket families guard 2 polices. Anything else (``AF_UNIX``, ``AF_NETLINK``,
#: …) cannot reach a remote host at all, so blocking it would be a pure false
#: positive — and a baffling one, since the failure message talks about network
#: egress.
_EGRESS_FAMILIES = frozenset({socket.AF_INET, socket.AF_INET6})

#: Every guard violation intercepted since the last teardown check, as a ready-to-print
#: block (the operator message plus the stack captured at interception time). Read and
#: cleared by :func:`_block_unmocked_network_and_llm_access`'s teardown.
#:
#: Module-level, not fixture-local, and that is the whole design (lode-sx17): a violation
#: can be appended from a *thread* — or from inside a third-party ``except Exception`` —
#: that the raising half of the guard cannot report from. Under ``pytest-xdist`` each
#: worker is its own process, so "process-global" is per-worker, which is exactly the
#: scope wanted: a worker only ever runs one test at a time.
#:
#: Deliberately NOT cleared at test *setup*. A record appended between one test's teardown
#: and the next test's setup — a straggler worker outliving the test that started it — must
#: still be reported by someone, even though the someone will be the wrong test. The
#: captured stack in the message is what identifies the real caller.
_GUARD_VIOLATIONS: list[str] = []

#: Guards the list above. ``list.append`` is atomic under CPython's GIL, so this is
#: belt-and-braces for the append — but the teardown check's read-then-clear is genuinely
#: two operations, and the appends it races against come from threads by design.
_GUARD_VIOLATIONS_LOCK = threading.Lock()


def _record_and_fail(message: str) -> None:
    """Record a guard violation, then fail the current test with ``message``.

    The single door both guards go through, so recording can never drift out of
    sync with failing: there is no way to write a guard that raises without
    recording.

    ``traceback.format_stack()`` is captured *here*, at interception, because by
    the time the teardown check reads the list the offending frames are long
    gone — and in the swallow/thread cases that stack is the only thing that
    identifies the caller. Its last entry (this function's own frame) is
    dropped so the trace ends at the guard, not inside the recorder.
    """
    stack = "".join(traceback.format_stack()[:-1])
    with _GUARD_VIOLATIONS_LOCK:
        _GUARD_VIOLATIONS.append(f"{message}\n\nintercepted at:\n{stack}")
    pytest.fail(message)


def _unconsumed_violations_message(
    recorded: list[str], *, expected: bool
) -> str | None:
    """The teardown verdict on what the guards recorded during one test.

    Returns the operator-facing message when the test must fail, or ``None``
    when it is clean. A pure function for the same reason
    :func:`_wrong_source_tree_message` is one — the fixture teardown that calls
    it can otherwise only be exercised by running pytest under pytest.

    ``expected`` is "this test carries ``@pytest.mark.trips_network_guard``",
    i.e. it trips a guard on purpose and catches the raise itself. That marker
    is checked in **both** directions: a marked test that recorded nothing is
    also a failure, because the marker disables this backstop for that test and
    a marker nobody needs any more disables it for nothing.
    """
    if expected:
        if recorded:
            return None
        return (
            "test is marked @pytest.mark.trips_network_guard but tripped no "
            "guard -- the marker is stale, and while it is present the "
            "swallowed-violation backstop is disabled for this test. Remove it "
            "(tests/conftest.py)."
        )
    if not recorded:
        return None
    return (
        f"{len(recorded)} network/LLM-guard violation(s) were intercepted during "
        "this test, but no failure reached pytest -- something in the call chain "
        "swallowed it, or it was raised off the main thread where pytest.fail() "
        "cannot propagate. The call was still BLOCKED; what failed is the "
        "reporting, which is why this fires in teardown instead (lode-sx17).\n\n"
        "If the stack below names a test other than this one, it is a straggler "
        "from an earlier test's background worker -- trust the stack, not the "
        "test name.\n\n" + "\n\n".join(recorded)
    )


def _is_loopback(address: object) -> bool:
    """Is this ``connect()`` destination the local machine?

    Loopback stays permitted: existing offline tests deliberately connect to a
    *refused* loopback port to exercise a real ``ConnectionRefusedError``
    without standing up a fake server (e.g.
    ``tests/test_webfetch.py::TestHttpxFetcher::test_connection_error_is_transient``
    against ``127.0.0.1:1``). Decided by ``ipaddress``, not a hardcoded set of
    strings, so the whole ``127.0.0.0/8`` block counts — ``127.0.1.1`` is the
    stock Debian/Ubuntu ``/etc/hosts`` alias for the machine's own hostname and
    is every bit as loopback as ``127.0.0.1``.
    """
    host = address[0] if isinstance(address, tuple) else address
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        # Not a literal IP -- a hostname. ``connect()`` is normally handed an
        # already-resolved address (``socket.create_connection`` resolves via
        # ``getaddrinfo`` first), so this is the rare direct-``connect()`` case.
        return str(host) == "localhost"


def _make_guarded_connect(method_name: str):
    """Wrap ``socket.socket.<method_name>`` so non-loopback egress fails the test.

    Passes straight through for a loopback destination, and for any socket
    family that cannot reach a remote host in the first place (see
    :data:`_EGRESS_FAMILIES`).
    """
    real = getattr(socket.socket, method_name)

    def _guarded(
        self: socket.socket, address: object, *args: object, **kwargs: object
    ) -> object:
        if self.family in _EGRESS_FAMILIES and not _is_loopback(address):
            _record_and_fail(
                f"test attempted a real outbound network connection to "
                f"{address!r} (socket.{method_name}) -- no fake was installed "
                "for it. If this test genuinely needs live network access, opt "
                "in with @pytest.mark.network (tests/conftest.py)."
            )
        return real(self, address, *args, **kwargs)

    return _guarded


@pytest.fixture(autouse=True)
def _isolate_lode_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Resolve the real, durable weights cache BEFORE $LODE_HOME is redirected --
    # this is the machine-level cache production uses. Via model_cache_dir(), so
    # the layout is resolved in exactly one place: hand-building $LODE_HOME/models
    # here would silently aim the symlink at a stale path the day that function
    # moves, reinstating the very re-download this fixes (lode-gmo).
    durable_models = model_cache_dir()
    durable_models.mkdir(parents=True, exist_ok=True)

    home = tmp_path_factory.mktemp("lode-home")
    (home / "models").symlink_to(durable_models, target_is_directory=True)
    monkeypatch.setenv("LODE_HOME", str(home))


@pytest.fixture(autouse=True)
def _restore_root_logger_state():
    """Snapshot and restore the root logger's level + handler set (lode-kmes).

    ``lode.logconfig.configure_logging`` sets the root logger's level directly
    (``root.setLevel(resolved)``) and never restores it — a global,
    process-wide mutation with no matching teardown. Under ``pytest-xdist``, a
    worker process runs many tests in sequence, so this is not scoped to one
    test: ``tests/test_cli.py``'s ``--debug`` flag tests (e.g.
    ``test_debug_flag_sets_debug_log_level``) resolve an explicit ``DEBUG``
    level, which — with nothing restoring it — leaves the ENTIRE root logger
    at ``DEBUG`` for every later test that worker process happens to run.

    That is the confirmed root cause of an intermittent whole-suite hang at
    ~98% completion under xdist (OBSERVED TWICE in one ``/land`` pass, see the
    ticket): a later test in the same worker that mounts
    :class:`~lode.tui.screens.capture.CaptureScreen` unknowingly enables its
    DEBUG-gated, never-returning latency-probe worker
    (:func:`lode.tui.latency_probe.probe_event_loop_lag` — its own docstring:
    "Run forever ... it never exits on its own", stopped only by Textual's
    worker cancellation on a clean screen unmount). If that test's teardown
    doesn't get a clean unmount, the worker keeps that test's asyncio event
    loop alive indefinitely and the whole session never finishes — exactly the
    observed symptom (a live, DEBUG-logging Textual event loop with zero CPU
    progress on the pytest master).

    Restoring the level — and closing/removing any handler the test attached
    that wasn't there before it (``configure_logging`` also attaches a file
    handler when given a ``log_dir``, e.g. via the CLI group callback under
    ``$LODE_HOME/logs``) — after every test closes the leak at its source,
    independent of which specific test happens to trigger it. This deliberately
    does more than restore the level — it also closes/removes any handler a
    test attached (it does not re-add a handler a test *removed*, nor strip a
    filter added to a pre-existing handler; neither leak is known to matter
    here) — per the ticket's acceptance criterion to check for other global
    state leaking the same restore-nothing way.
    """
    root = logging.getLogger()
    level_before = root.level
    handlers_before = list(root.handlers)
    yield
    root.setLevel(level_before)
    for handler in list(root.handlers):
        if handler not in handlers_before:
            root.removeHandler(handler)
            handler.close()


#: Set ONLY by ``tests/test_conftest_jobs_clock_anchor.py``'s own nested-subprocess repro, to
#: measure this fixture's own effect on demand (lode-up8x) -- never set in a real test run, and
#: never read anywhere else. Prefixed and namespaced defensively precisely because it disables a
#: safety fixture: an accidental ambient hit would silently re-open the class of bug lode-x10m
#: exists to close.
_DISABLE_JOBS_CLOCK_ANCHOR_RESET_ENV_VAR = "_LODE_TEST_DISABLE_JOBS_CLOCK_ANCHOR_RESET"


@pytest.fixture(autouse=True)
def _reset_jobs_clock_anchor() -> None:
    """Reset ``lode.jobs``'s process-global clock anchor before every test (lode-x10m).

    ``jobs.now()`` ratchets a module-global, ``jobs._now_epoch``, forward-only
    and never restores it (the ratchet itself is owned by
    :func:`lode.jobs.now` and ``docs/storage.md`` -- read either for *why* it
    ratchets; this only covers what that costs the test suite). That is fine
    as long as nothing patches the *inputs* to ``now()``'s anchor computation
    (``time.monotonic()``) out from under it, and until lode-e8lo something did:
    ``tests/test_cli.py``'s ``test_work_wait_times_out_naming_outstanding_jobs``
    and ``test_work_wait_does_not_duplicate_the_one_shot_outstanding_line`` both
    did ``monkeypatch.setattr(cli.time, "monotonic", _fake_monotonic)``. Because
    ``lode/cli/__init__.py`` does a plain ``import time``, ``cli.time`` IS the shared
    ``time`` module object, not a module-local alias -- so that patch was
    PROCESS-GLOBAL and reached ``lode.jobs`` (which also does a plain
    ``import time``) too, not just ``lode.cli``. Both now rebind the *name*
    ``time`` inside ``lode.cli`` instead (``test_cli.py``'s
    ``_patch_cli_clock_past_deadline``), so no poisoner is live today.

    With the fake monotonic substituted, ``now()``'s anchor computation
    (``real_now - fake_monotonic``) comes out far larger than the true anchor
    (real ``time.monotonic()`` on Linux is seconds-since-boot, commonly
    hundreds of hours), and ``max()`` only ever grows ``_now_epoch``. Nothing
    restores it afterward: ``monkeypatch`` reverts ``time.monotonic`` itself,
    but ``_now_epoch`` was never the thing patched, so the poisoned value
    survives for the rest of that worker process. Every later test in the
    same ``pytest-xdist`` worker then sees ``jobs.now()`` running hours to
    days ahead of the wall clock -- confirmed to flip
    ``tests/test_worker.py::test_reset_leaves_future_failed_alone``,
    ``test_claim_respects_future_next_attempt_at``, and
    ``test_run_refresh_dead_letter_still_tombstones_over_older_content``
    (whichever of those a poisoned worker happens to run next), since none of
    them request the ``clock`` fixture that resets this anchor itself.

    Resetting the anchor before every test -- not merely restoring whatever a
    scoped ``monkeypatch`` changed -- closes this off for the three known
    victims *and* for any **future** test that patches the shared ``time``
    module (anywhere, not just ``test_cli.py``): the poison re-arms harmlessly
    instead of outliving the test that created it. Tests that drive
    ``jobs.now()`` directly (``tests/test_worker.py``'s own ``clock`` fixture,
    which resets this same anchor) still run after this and still see a
    freshly reset anchor. That forward-looking half is now the WHOLE of this
    fixture's justification: lode-e8lo removed the only live poisoner, so this
    stays as defence in depth against the next one, not because anything leaks
    today. Deleting it turns nothing red on its own, and since lode-e8lo it no
    longer turns ``tests/test_conftest_jobs_clock_anchor.py``'s nested repro
    red either -- that file's NON-VACUITY section is where the measurement
    lives, and it owns what is and is not pinned today, plus the follow-up.

    SCOPE OF THAT CLAIM, stated precisely, because an unbounded version of it
    is what let this bug survive three sightings:

    - It closes the class of poisoned ``_now_epoch``, not the broader class of
      "a test patches an attribute on a shared stdlib module object." What
      makes that sufficient *today* is that ``_now_epoch`` is the only
      persistent sink for such a patch: it is the only module-global rebound
      under a ``global`` statement anywhere in ``src/lode/``, and every other
      ``time.monotonic()`` call site in ``src/lode/`` binds the reading to a
      local that dies with the call. A future module-global derived from a
      patchable clock would need its own reset here.
    - This fixture is function-scoped, so it cannot protect *module*- or
      *session*-scoped fixture setup, which pytest runs before it. A
      higher-scoped fixture that reaches ``jobs.now()`` therefore still sees
      whatever the previous test left behind -- reachable today only via
      ``tests/test_capture_lag_diagnosis.py``'s ``seeded_db``, which is
      ``skipif``-gated and asserts nothing about job timestamps.
    - It bounds a poison's lifetime; it does not prevent the leak. lode-e8lo
      fixed the leak at its source for the two ``test_cli.py`` tests, so no
      test currently runs under a process-global fake clock -- but a future
      one that does would still see the poison for its own duration, and only
      this fixture stops that outliving it. The two are complementary, not
      alternatives.

    ESCAPE HATCH (lode-up8x): returns early, doing nothing, when
    ``_DISABLE_JOBS_CLOCK_ANCHOR_RESET_ENV_VAR`` is set in the environment. This exists solely so
    ``tests/test_conftest_jobs_clock_anchor.py`` can measure this fixture's own effect on demand,
    from a nested subprocess it controls -- see that file's NON-VACUITY section for what it drives
    and why lode-e8lo left this fixture with no live poisoner to prove it against otherwise. Never
    set this in a real test run; nothing else reads it.
    """
    if os.environ.get(_DISABLE_JOBS_CLOCK_ANCHOR_RESET_ENV_VAR):
        return
    jobs._now_epoch = datetime.min.replace(tzinfo=UTC)


@pytest.fixture
def set_console_width(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[int], None]:
    """Force ``lode.cli``'s shared ``console`` to a given width for one test.

    rich's ``Console()`` reads ``COLUMNS`` from the environment ONCE, at
    CONSTRUCTION -- but only CONDITIONALLY: ``Console.__init__`` sets
    ``self._width`` from ``os.environ["COLUMNS"]`` whenever the ``width``
    constructor arg is ``None`` (lode's case) AND ``COLUMNS`` happens to be
    present in the environment at that moment (verified against the installed
    rich 15.0.0). Once baked, ``Console.size`` short-circuits on ``self._width
    is not None`` and never re-reads the environment again -- for the REST OF
    THE PROCESS'S LIFETIME.

    For a real one-shot ``lode config`` invocation that is harmless (import time
    and render time are the same moment). It is NOT harmless in this test suite:
    pytest-xdist imports ``lode.cli`` ONCE per worker process and reuses the same
    ``console`` singleton across every test that worker runs -- so whichever
    ``COLUMNS`` happened to be in THAT worker's environment at its first import
    of ``lode.cli`` (observed here: '80', inherited from outside pytest's own
    control) freezes the console's width for every subsequent test in that
    worker, and a later ``runner.invoke(..., env={"COLUMNS": ...})`` override has
    NO effect -- confirmed empirically: it changes ``os.environ`` for the call,
    but ``console.size``'s early-return never re-reads it.

    This reaches past that freeze by monkeypatching the private
    ``_width``/``_height`` attributes (auto-reverted after the test). Reaching
    into a private attribute is deliberate: lode-l38d.1 decided AGAINST adding a
    production test seam to the shared ``Console`` (``docs/stack.md``), so a
    test-only reach-in is the compromise that decision implies.

    It is also deliberately NOT the NO_COLOR subprocess technique used in
    tests/test_cli_console.py: that module exists to verify rich's *env detection
    mechanism itself*, whereas callers of this fixture only care about
    Table/``overflow="fold"`` *rendering* behaviour at a given width, which does
    not require re-proving env detection.

    Shared here rather than copied per module (lode-l38d.4 review): the rationale
    above is pinned to rich's internals, so two copies would silently drift apart
    on a rich upgrade.
    """

    def _set(width: int) -> None:
        # Imported lazily: lode.cli's import graph is expensive (~1.3s), and a
        # module-level import here would charge every test session for it.
        from lode.cli import console

        monkeypatch.setattr(console, "_width", width)
        monkeypatch.setattr(console, "_height", 24)

    return _set


#: Every module that must NOT be resident for an "is the SDK imported?" assertion
#: to mean anything: the SDK itself, plus the two lode modules whose job is to stay
#: cheap to import so a credential-free drain never pulls it in (lode-4q97).
_SDK_IMPORT_GRAPH = ("anthropic", "lode.auth", "lode.enrich")


@pytest.fixture
def forget_sdk_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evict the Anthropic import graph from ``sys.modules`` (lode-4q97).

    Tests that assert "this code path did not import the SDK" are **vacuously
    green** unless the modules are evicted first -- an earlier test in the session
    (or the asserting test file's own module-level imports) will already have them
    resident. Centralised here so the eviction set has ONE home: a hand-copied set
    that misses a newly-added SDK-importing module does not fail, it silently stops
    guarding, which is the expensive failure mode.

    **Restoring `sys.modules` alone is not enough.** A test using this fixture may
    still legitimately re-import one of these modules (``worker.drain``, for one,
    imports ``lode.auth`` unconditionally on every drain). Importing ``lode.X``
    binds the new module object as an **attribute of the ``lode`` package** as well
    as into ``sys.modules`` -- and ``monkeypatch.delitem`` only restores the latter.
    Left unrestored, ``lode.enrich`` (the attribute) and ``sys.modules[...]`` end up
    as two *different* module objects, and a later test that patches one while the
    code under test resolves the other silently talks to the real Anthropic SDK.
    That is not hypothetical: it broke lode-9yy's
    ``test_drain_still_runs_embed_jobs_when_credentials_are_missing``, which patches
    ``lode.enrich.build_client``. So record the package attributes too -- setting
    each to its current value makes monkeypatch restore that value at teardown.
    """
    for name in _SDK_IMPORT_GRAPH:
        monkeypatch.delitem(sys.modules, name, raising=False)

    for name in _SDK_IMPORT_GRAPH:
        pkg_name, _, attr = name.rpartition(".")
        pkg = sys.modules.get(pkg_name)
        if pkg is not None and hasattr(pkg, attr):
            monkeypatch.setattr(pkg, attr, getattr(pkg, attr))


def _egress_guard_applies(node: pytest.Item) -> bool:
    """Does guard 2 (outbound socket egress) police this test?

    ``@pytest.mark.network`` lifts it outright; ``@pytest.mark.slow``
    additionally lifts it for the real-model tier (see the module docstring).

    A named predicate rather than the condition inlined twice, because
    :func:`_stub_the_query_embedder_offline` below must install its stub over
    **exactly** this set and no other (lode-7ypf) — the stub exists only to
    remove egress this guard would otherwise block, so the day the two
    conditions disagree, one of them is wrong.
    """
    return (
        node.get_closest_marker("network") is None
        and node.get_closest_marker("slow") is None
    )


class _OfflineQueryEmbedder:
    """Offline stand-in for :class:`lode.embedding.FastEmbedEmbedder` (lode-7ypf).

    Zero vectors of the configured width — the same shape as the ``_StubEmbedder``
    six test modules had each written for themselves before this fixture existed
    (tests/test_tui_app.py, tests/test_tui_capture_save_and_new.py,
    tests/test_tui_edit_related_notes.py, tests/test_tui_open_link.py,
    tests/test_cli.py, tests/test_skeleton_gate.py). Those local stubs are
    deliberately left in place: several of them count constructions or record
    calls, which is the point of the test they belong to, and a test's own
    ``monkeypatch.setattr`` runs after this fixture's and so still wins.

    ``embedding_vector_dim``-wide, not a fixed length: a query vector of the
    wrong width is a LanceDB error, not an empty result, so reading the width
    from the settings handed in is what keeps this a *stand-in* rather than a
    second failure mode.

    It mirrors the real class's whole **duck-typed** surface, not just the two
    :class:`~lode.embedding.Embedder` protocol methods, because lode probes the
    rest by ``hasattr``: ``warm()`` is what ``lode models pull`` calls
    (``cli/models.py``), ``model_revision()`` is what ``embedding.py``'s
    ``_embedder_model_revision`` duck-types on to stamp provenance on written
    vectors, and ``reset_revision_probe()`` is what ``worker.drain()`` calls
    once per pass to retry a failed probe (``lode-fxse``). Omitting any of
    these does not fail loudly — it silently routes the code under test down
    the *absent-method* branch, which is a different path from production.
    ``None`` is the honest revision for a stub, and is exactly what that
    helper already documents an absent method to mean; ``reset_revision_probe``
    is a genuine no-op here since this stub's ``model_revision()`` never
    caches anything to reset in the first place.
    """

    def __init__(self, settings: object) -> None:
        self._dim = settings.embedding_vector_dim

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self._dim for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        del text
        return [0.0] * self._dim

    def warm(self) -> None:
        return None

    def model_revision(self) -> str | None:
        return None

    def reset_revision_probe(self) -> None:
        return None


#: Every binding of ``FastEmbedEmbedder`` a test can reach at call time. Two, not
#: one, and both are load-bearing:
#:
#: * ``lode.embedding`` is what the deferred imports resolve against —
#:   ``RelatedNotesPanel._ensure_embedder`` (the actual lode-7ypf leak) and
#:   ``lode.cli``'s ``ask``/``models pull`` paths all import it *inside* the
#:   function, so patching the attribute reaches them.
#: * ``lode.tui.services.related`` holds its own import-time binding, used by
#:   ``find_related_notes``'s ``embedder or FastEmbedEmbedder(settings)``
#:   fallback. No test reaches it today (every direct caller passes an embedder,
#:   and the rest return early on the enabled/min-chars/missing-db gates), but it
#:   is a live production path one test away from leaking exactly as the panel
#:   did. Patching one binding and not the other is the half-fix that gets
#:   rediscovered.
#:
#: A module that binds the name at *import* time and is imported before the
#: fixture runs — tests/test_capture_lag_diagnosis.py, the lag spike that wants
#: the real ONNX model — keeps its own reference and is untouched. That is the
#: correct outcome, and it is why that file needs no opt-out marker (it is also
#: skipped unless ``LODE_DIAGNOSE_LAG=1``).
_FASTEMBED_BINDINGS = ("lode.embedding", "lode.tui.services.related")


@pytest.fixture(autouse=True)
def _stub_the_query_embedder_offline(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the real ONNX query embedder out of every guarded test (lode-7ypf).

    Even setting aside the HF revision probe (fixed product-side by lode-dj6m:
    ``embed_query`` no longer resolves it at all), the real
    :class:`~lode.embedding.FastEmbedEmbedder` still downloads/loads the actual
    ONNX weights on first use -- hundreds of MB, and real disk/CPU cost no test
    here wants to pay. Any test that lets that run reaches the network (a cold
    cache) or burns real time (a warm one).

    The path that made this a real problem is
    :meth:`~lode.tui.widgets.related_notes_panel.RelatedNotesPanel._ensure_embedder`:
    a debounced background worker constructs the embedder lazily, so **any** TUI
    test that puts text in a body ``TextArea`` and does not stub it arms a live
    network call — one that outlives the test's own app teardown and fires
    whenever the CPU gets to it. ~40 such call sites across five files had each
    to remember to stub, and most did not; the six that did wrote the same
    ``_StubEmbedder`` six times. This is that stub, once, on by default.

    **Scoped to exactly** :func:`_egress_guard_applies` — the tests where the
    socket guard is live. That is not a coincidence of convenience: the stub's
    whole job is to remove egress the guard would block, so a test allowed to
    reach the network (``@pytest.mark.network``) or to load a real model
    (``@pytest.mark.slow``) must get the real class. Sharing one predicate is
    what keeps that true without anyone maintaining two lists.

    ``@pytest.mark.real_embedder`` is the third way out, for a test that is
    neither of those and still wants the genuine class: today exactly one, the
    canary that pins the *installed* fastembed's exhausted-sources error string
    (tests/test_cli.py). It is hermetic — ``HF_HUB_OFFLINE=1`` against a cold
    ``$LODE_HOME`` — so it must keep the socket guard, which rules out
    ``slow``/``network``, and it is worthless against a stub, since the whole
    point is what the real package raises.
    """
    if not _egress_guard_applies(request.node):
        return
    if request.node.get_closest_marker("real_embedder") is not None:
        return
    for module in _FASTEMBED_BINDINGS:
        monkeypatch.setattr(
            f"{module}.FastEmbedEmbedder", _OfflineQueryEmbedder, raising=True
        )


@pytest.fixture(autouse=True)
def _block_unmocked_network_and_llm_access(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Fail loudly, not silently, if a test reaches real network/LLM access.

    See the module docstring ("A network-touching test must FAIL...", lode-85q)
    for the full rationale. ``@pytest.mark.network`` lifts both guards below;
    ``@pytest.mark.slow`` additionally lifts guard 2 only.

    The teardown half is the swallowed-violation backstop (lode-sx17): both
    guards record before they raise, and a record that survives to teardown
    means the raise never reached pytest. It runs for **every** test, marker or
    not — a lifted guard records nothing, so there is nothing to skip, and a
    straggler from an earlier test must still be reported by someone.
    """
    marked_network = request.node.get_closest_marker("network") is not None

    # Guard 1: real anthropic.Anthropic()/AsyncAnthropic() construction.
    # Unconditional regardless of ANTHROPIC_API_KEY -- it fires before the SDK's
    # own credential-chain logic runs, so it closes the unkeyed-CI gap (where
    # construction itself raises, before any socket opens) exactly as it closes
    # the keyed-dev-machine gap.
    if not marked_network:
        import anthropic

        def _blocked_init(self: object, *args: object, **kwargs: object) -> None:
            _record_and_fail(
                "test constructed a real anthropic.Anthropic client -- no fake "
                "was installed for it. If this test genuinely needs live "
                "Anthropic access, opt in with @pytest.mark.network "
                "(tests/conftest.py)."
            )

        monkeypatch.setattr(anthropic.Anthropic, "__init__", _blocked_init)
        if hasattr(anthropic, "AsyncAnthropic"):
            monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", _blocked_init)

        # Same guard for the OpenAI/Azure provider (lode-568v.3) -- a test
        # reaching real openai.OpenAI()/AzureOpenAI() construction with no
        # fake installed must fail loudly, the same as the Anthropic case
        # above.
        import openai

        def _blocked_openai_init(self: object, *args: object, **kwargs: object) -> None:
            _record_and_fail(
                "test constructed a real openai.OpenAI/AzureOpenAI client -- "
                "no fake was installed for it. If this test genuinely needs "
                "live OpenAI/Azure access, opt in with @pytest.mark.network "
                "(tests/conftest.py)."
            )

        monkeypatch.setattr(openai.OpenAI, "__init__", _blocked_openai_init)
        monkeypatch.setattr(openai.AzureOpenAI, "__init__", _blocked_openai_init)
        if hasattr(openai, "AsyncOpenAI"):
            monkeypatch.setattr(openai.AsyncOpenAI, "__init__", _blocked_openai_init)
        if hasattr(openai, "AsyncAzureOpenAI"):
            monkeypatch.setattr(
                openai.AsyncAzureOpenAI, "__init__", _blocked_openai_init
            )

    # Guard 2: generic outbound socket egress (any non-loopback destination).
    # Relaxed for @pytest.mark.slow too -- see module docstring.
    #
    # Both connect() AND connect_ex() are patched: they are independent C
    # methods (connect_ex does not call connect), so guarding only the former
    # would leave the guard failing *open* on the latter -- and a guard that
    # silently misses is worse than no guard, because it licenses false
    # confidence.
    if _egress_guard_applies(request.node):
        for _method in ("connect", "connect_ex"):
            monkeypatch.setattr(
                socket.socket, _method, _make_guarded_connect(_method), raising=True
            )

    yield

    with _GUARD_VIOLATIONS_LOCK:
        recorded = list(_GUARD_VIOLATIONS)
        _GUARD_VIOLATIONS.clear()
    message = _unconsumed_violations_message(
        recorded,
        expected=request.node.get_closest_marker("trips_network_guard") is not None,
    )
    if message is not None:
        pytest.fail(message)


@pytest.fixture
def guard_violations() -> list[str]:
    """The live record list both guards append to, for tests that assert on it.

    Handed out as a fixture rather than imported from this module by name, so
    the one place that decides what "the record" is stays here — a test that did
    ``from conftest import _GUARD_VIOLATIONS`` would silently bind a stale
    object the day this list is ever rebound rather than mutated.

    Only ``@pytest.mark.trips_network_guard`` tests have any business asking for
    it; anyone else finds it empty, since the autouse fixture drains it at every
    teardown.
    """
    return _GUARD_VIOLATIONS


@pytest.fixture(scope="session", autouse=True)
def _cache_cross_encoder_model_load():
    """Session-cache the real, un-mocked ``FastEmbedCrossEncoder`` model load.

    Slow-tier tests (``@pytest.mark.slow``, lode-pql) deliberately leave the
    reranker un-mocked to exercise the real path, but ``retrieval.rerank``
    constructs a *fresh* ``FastEmbedCrossEncoder(settings)`` per call (no
    ``scorer`` passed in) — so each slow test paid the full ``fastembed``
    ``TextCrossEncoder`` model load again, which ``pytest --durations``
    measured as the dominant per-test cost (3-9s each, lode-b4w.6).

    The loaded model is stateless inference weights, so caching one instance
    per cache key for the whole test session and handing it to every
    ``FastEmbedCrossEncoder`` that asks for it changes no test's observable
    behavior (identical to a production process that reuses one warm reranker
    across multiple ``ask``s) — it just pays the load cost once instead of once
    per slow test. Scoped to the session (not narrower) so it survives across
    test *functions*; under ``pytest-xdist`` each worker is its own process,
    so the cache is naturally per-worker (loads once per worker, not once per
    test) rather than shared globally.

    That "no observable behavior change" claim holds only because the key is
    ``(model name, TextCrossEncoder class)`` and not the model name alone
    (lode-vzwn). The cached value is a product of *both*: ``_load`` builds it by
    calling whichever ``TextCrossEncoder`` is bound on the ``fastembed`` module
    at call time. So a test that observes a *side effect of the real load* rather
    than its return value — by monkeypatching that constructor to record its
    kwargs, the way ``test_load_passes_durable_model_cache_dir`` (in
    tests/test_retrieval.py) does — is keyed on its own fake class, always
    MISSES, and always gets a real load whose constructor call actually runs.

    Keying on the model name alone under-keys the cache: that test would take a
    HIT off whatever slow-tier test already loaded the same model name on that
    xdist worker, the real constructor would never run, its recorded side effect
    would never happen, and the assertion would fail with ``KeyError:
    'cache_dir'`` — a coin flip on test order (``pytest-randomly``). That was
    the bug. The key, not a convention, is what prevents it: a test that fakes
    the constructor cannot take a hit even if it uses the default model name.

    Two things this does NOT cover, so don't read it as a blanket guarantee:

    * It only protects a side effect that is observed *through the constructor*.
      A test that asserted on some other side effect of a real load (a file
      appearing on disk, say) without faking ``TextCrossEncoder`` would still
      take a hit and still be order-dependent.
    * It only applies to ``FastEmbedCrossEncoder``. ``FastEmbedEntailmentScorer``
      (lode/faithfulness.py) has a near-identical ``_load`` and a near-identical
      test, but is deliberately *not* cached here — which is the only reason its
      test is safe. If that load is ever session-cached too, key it the same way,
      or that test starts flaking exactly as this one did.
    """
    from lode.retrieval import FastEmbedCrossEncoder

    cache: dict[tuple[str, object], object] = {}
    original_load = FastEmbedCrossEncoder._load

    def _cached_load(self: FastEmbedCrossEncoder) -> object:
        if self._model is None:
            from fastembed.rerank import cross_encoder

            # The class, not just the name: see the docstring. A monkeypatched
            # TextCrossEncoder is a different key, so it can never take a hit.
            key = (self._model_name, cross_encoder.TextCrossEncoder)
            if key not in cache:
                cache[key] = original_load(self)
            self._model = cache[key]
        return self._model

    patcher = pytest.MonkeyPatch()
    patcher.setattr(FastEmbedCrossEncoder, "_load", _cached_load)
    yield
    patcher.undo()


# --- Load a repo-root script/module by explicit file path (lode-7ed9) ------
#
# scripts/ is a plain directory of standalone scripts, not an installed
# package, and noxfile.py sits one level above tests/ -- neither is reachable
# by a name-based import, so both are loaded straight from their file path.
#
# Three call sites (tests/test_check_links.py, tests/test_model_cache_key_
# script.py, tests/test_noxfile_venv_tool.py) had independently reimplemented
# this spec_from_file_location -> module_from_spec -> exec_module sequence,
# and had already drifted: the first two registered the loaded module in
# sys.modules BEFORE exec_module and the third did not, while only the third
# narrowed the Optional spec. This helper is the union, so a fourth call site
# gets both without whoever writes it having to know about either.


def load_module_from_path(name: str, path: Path) -> ModuleType:
    """Load the script/module at ``path`` under module name ``name``.

    Registers the module in ``sys.modules`` before executing it. That is
    load-bearing, not defensive: a module defining a dataclass fails outright
    without it, because ``dataclasses`` looks the class's own module up via
    ``sys.modules`` during class creation. scripts/check_links.py has a frozen
    ``@dataclass`` and dies with ``AttributeError: 'NoneType' object has no
    attribute '__dict__'`` if it is executed unregistered.

    The registration is permanent for the session -- nothing evicts it -- so
    ``name`` must not be a name anything else imports: a hypothetical
    scripts/build.py loaded as ``"build"`` would displace the real ``build``
    distribution for every later test in that worker. The assert below does
    NOT catch that; the module being displaced is typically not yet resident
    when the load happens. It catches the collision that always *is*
    detectable -- loading the same name a second time -- which is a genuine
    hazard for any caller whose module has import-time side effects.
    """
    assert name not in sys.modules, (
        f"{name!r} is already in sys.modules -- loading {path} under that "
        f"name would replace it for the rest of the session"
    )
    spec = importlib.util.spec_from_file_location(name, path)
    # Type-narrowing only, not a real failure mode: spec_from_file_location
    # returns None only when no loader claims the suffix, which cannot happen
    # for the .py paths this is called with.
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --- MagicMock Anthropic batch client (lode-ylh2) --------------------------
#
# The shared builder for tests/test_enrich.py's and tests/test_worker.py's
# batch tests. batch_id is REQUIRED and has no default on purpose: the two
# per-module originals this replaced each baked in a different literal, so any
# single default would silently change one side's behaviour. Callers pass the
# id they mean, whether or not they assert on it.
#
# NOT moved into tests/_anthropic_rig.py: that module's own docstring
# disclaims a general "shared test helpers live outside conftest.py" rule --
# it holds the REAL-SDK MockTransport rig, justified by a large body of
# Anthropic-batch-wire trivia and ~40 lines of SDK-shaped fixture data needing
# lockstep updates when the pinned SDK's MessageBatch required fields change.
# This is a ~15-line MagicMock builder with no such fixture-drift risk, so it
# meets none of that module's stated bar for moving out. conftest.py already
# deliberately holds several plain non-fixture helpers (see
# load_module_from_path above).


def fake_batch_client(
    batch_id: str,
    results: list | None = None,
    processing_status: str = "ended",
) -> mock.MagicMock:
    """Mock Anthropic client with a Batches API stub.

    ``results`` is a list of mock result objects; each needs:
    - ``.custom_id`` (version_id)
    - ``.result.type`` ('succeeded' | 'errored')
    - ``.result.message.content`` (list of blocks) when type='succeeded'
    """
    client = mock.MagicMock()

    # Batch creation
    batch = mock.MagicMock()
    batch.id = batch_id
    client.beta.messages.batches.create.return_value = batch

    # Batch retrieve (status)
    status_obj = mock.MagicMock()
    status_obj.processing_status = processing_status
    client.beta.messages.batches.retrieve.return_value = status_obj

    # Batch results
    client.beta.messages.batches.results.return_value = iter(results or [])

    return client


# --- MagicMock Anthropic batch RESULT payload (lode-0i0k) ------------------
#
# The complement to fake_batch_client above: that builds the client, this
# builds the individual result objects handed to its ``results=`` argument --
# exactly the shape its docstring documents. Shared by tests/test_enrich.py
# and tests/test_worker.py, which between them had five hand-written copies.
#
# ``enrichment`` is read only on the succeeded branch; the errored branch
# needs no payload, and its three callers in test_enrich.py pass a throwaway
# ``EnrichmentResult()``. Left required rather than defaulted to None so the
# succeeded path -- the overwhelmingly common one -- cannot silently build a
# result with no tool_use input.
#
# Placement matches fake_batch_client's for the same reason: a small
# MagicMock builder with no SDK-shaped fixture data, so it meets none of
# tests/_anthropic_rig.py's stated bar for moving out.


def _make_batch_result(
    version_id: str,
    enrichment: EnrichmentResult,
    result_type: str = "succeeded",
) -> mock.MagicMock:
    """Build a mock batch result object (succeeded or errored)."""
    r = mock.MagicMock()
    r.custom_id = version_id
    r.result.type = result_type

    if result_type == "succeeded":
        tool_block = mock.MagicMock()
        tool_block.type = "tool_use"
        tool_block.input = enrichment.model_dump()
        r.result.message.content = [tool_block]

    return r


# --- Shared Anthropic tool-turn fake + stub Fetcher (lode-pw9o) ------------
#
# tests/test_qa.py's and tests/test_cited_answer.py's end-to-end tool-turn
# tests each drove an identical fetch -> tool_result -> forced-schema-turn
# scenario against a real, unmodified answer_question()/ask() and the real
# faithfulness gate: a fetch tool call persists a snapshot, a second free
# turn ends the loop early, and the final turn echoes the snapshot_id back
# into a claim exactly the way the model itself would from the tool_result.
# The two copies were near-verbatim (~70 lines each, byte-identical stub
# Fetcher) -- an SDK or run_tool_turns contract change (block
# .type/.name/.input/.id, stop_reason values, the "second free turn ends the
# loop" trick) needed both edited. Hoisted here so there is one copy; each
# caller keeps its own final call (answer_question vs ask) and its own
# assertions and comments, per the ticket's acceptance criteria.
#
# Placement matches fake_batch_client's / _make_batch_result's above, and for
# the same reason: tests/_anthropic_rig.py exists for fakes built on a REAL
# anthropic.Anthropic + httpx.MockTransport, whose dict payloads break when the
# pinned SDK's required model fields change. This is duck-typed MagicMock -- no
# SDK model validates it -- so it does not carry that fixture-drift mode. (The
# block-shape coupling described above is real, but it is coupling to
# lode.llm_provider's own reader, not to the SDK's models.)


class StubWebFetcher:
    """Stub Fetcher (lode.webfetch.Fetcher protocol) returning one canned response.

    Not a queue despite the shape of its callers' scenario: it returns the SAME
    response on every call, unbounded. ``tests/test_jira_fetch.py``'s
    ``_QueueFetcher`` is the one that actually pops a list, if that is what you
    need.
    """

    def __init__(self, response: RawResponse) -> None:
        self._response = response
        self.calls: list[str] = []

    def fetch(self, url: str) -> RawResponse:
        self.calls.append(url)
        return self._response


def fake_tool_turn_client(
    conn: sqlite3.Connection, url: str, html: str, quoted_span: str
) -> tuple[mock.MagicMock, StubWebFetcher]:
    """Build the (client, web_fetcher) pair for the shared fetch-then-cite
    tool-turn scenario.

    ``client.messages.create`` is wired for exactly three calls:

    1. a free tool turn that calls the fetch tool against ``url``;
    2. a second free turn with no further tool call, ending the loop early
       (rather than exhausting run_tool_turns' default max_tool_turns=8);
    3. the final forced-schema turn, which reads the snapshot the fetch tool
       just persisted back out of ``conn`` and echoes its snapshot_id into a
       claim citing ``quoted_span`` -- the same way a real model would from
       the tool_result content.

    Pass the returned ``web_fetcher`` as the ``web_fetcher=`` kwarg so the
    fetch tool call resolves ``html`` for ``url`` without a real network call.
    """
    # Deferred, not module-scope -- see the import note at the top of this file.
    from lode.tool_dispatch import FETCH
    from lode.webfetch import RawResponse

    web_fetcher = StubWebFetcher(RawResponse(final_url=url, status_code=200, text=html))

    fetch_block = mock.MagicMock()
    fetch_block.type = "tool_use"
    fetch_block.name = FETCH
    fetch_block.input = {"source_type": "web", "external_id": url}
    fetch_block.id = "toolu_1"
    free_turn_response = mock.MagicMock()
    free_turn_response.content = [fetch_block]
    free_turn_response.stop_reason = "tool_use"

    text_block = mock.MagicMock()
    text_block.type = "text"
    second_free_turn_response = mock.MagicMock()
    second_free_turn_response.content = [text_block]
    second_free_turn_response.stop_reason = "end_turn"

    _responses = [free_turn_response, second_free_turn_response]

    def _create_side_effect(**_kwargs):
        if _responses:
            return _responses.pop(0)
        # Third call, the final forced-schema turn: the fetch has already run
        # (first free turn) and persisted a snapshot by now -- read it back to
        # build a claim that cites the real snapshot_id, the same way a model
        # would echo back what the tool_result told it.
        snapshot_id, body = conn.execute(
            "SELECT snapshot_id, body FROM snapshots WHERE external_id = ?",
            (url,),
        ).fetchone()
        assert quoted_span in body
        claim_block = mock.MagicMock()
        claim_block.type = "tool_use"
        claim_block.name = "_ClaimsEnvelope"
        claim_block.input = {
            "claims": [
                {
                    "text": quoted_span,
                    "support": [
                        {"snapshot_id": snapshot_id, "quoted_span": quoted_span}
                    ],
                }
            ]
        }
        claim_block.id = "toolu_2"
        response = mock.MagicMock()
        response.content = [claim_block]
        response.stop_reason = "tool_use"
        return response

    client = mock.MagicMock()
    client.messages.create.side_effect = _create_side_effect

    return client, web_fetcher


# --- Read noxfile.py's session set without executing it (lode-dis6) --------
#
# The complement to load_module_from_path above: two test files ask "which
# @nox.session functions exist?" and neither can answer it by importing
# noxfile.py a second time -- load_module_from_path asserts the name is not
# already resident, and executing the module twice re-registers every session
# in nox's global registry (see test_noxfile_venv_tool.py's _load_noxfile).
# Parsing is the way in, so the parser lives here once rather than as a copy
# per caller -- the same drift lode-7ed9 removed from the loader above.
#
# Deliberately uncached: parsing a ~500-line file costs well under a
# millisecond and the callers ask a handful of times per run, so a cache
# would buy nothing measurable while handing every test in the session a
# shared, mutable AST.


def noxfile_tree(noxfile_path: Path) -> ast.Module:
    """Parse ``noxfile_path``. The single read+parse both callers share, so
    neither has to re-decide the encoding or the ``filename=`` a SyntaxError
    is reported against."""
    return ast.parse(
        noxfile_path.read_text(encoding="utf-8"), filename=str(noxfile_path)
    )


def nox_session_nodes(noxfile_path: Path) -> dict[str, ast.FunctionDef]:
    """Every top-level ``@nox.session``-decorated function in ``noxfile_path``.

    Parsed rather than string-sliced: an earlier form of these tests located
    each session by ``source.index("\\n@nox.session\\ndef <next-one>(")``,
    which silently depended on the *order* sessions happen to appear in and
    had to be updated whenever one moved. The AST asks the question directly.

    Matches both ``@nox.session`` and ``@nox.session(tags=[...])``. Callers
    that need the decorator itself (tags, ``name=``) read it off the returned
    node's ``decorator_list``.
    """

    def is_nox_session(dec: ast.expr) -> bool:
        target = dec.func if isinstance(dec, ast.Call) else dec
        return isinstance(target, ast.Attribute) and target.attr == "session"

    return {
        node.name: node
        for node in noxfile_tree(noxfile_path).body
        if isinstance(node, ast.FunctionDef)
        and any(is_nox_session(dec) for dec in node.decorator_list)
    }


# --- Fenced ```bash/```sh block parsing (lode-ovgs, lode-p4qb) --------------
#
# THE ONE parser for "which bash does an agent actually execute", for the gates
# listed below, after private copies of it drifted apart -- verified by
# inspection, NOT by a gate, which is why that claim has been falsified from
# this same prose comment five separate times (lode-ovgs, lode-p4qb, lode-kjei,
# lode-jm4a, lode-oqqw) before lode-k5qb stopped asserting it here on comment
# authority alone: it is now a mechanical, AST-based gate,
# tests/test_no_private_fence_state_machine.py, that fails the suite the
# moment ANY module under tests/*.py or scripts/*.py (other than this file)
# hand-rolls a fence-toggle open/close flag again. Trust that gate, not this
# paragraph. The count is deliberately not restated here either: it has been
# hand-incremented (and gone stale) once per unification ticket, so the list
# below carries it and nothing else does.
#
# The bug that forced the unification is worth keeping, because it is the shape
# any re-implementation reinvents: tests/test_land_lock.py matched the fence
# marker with `line.startswith("```")`, so a fence INDENTED under a markdown
# list item (e.g. `.claude/skills/land/SKILL.md`'s Section 3 isolation-replay
# merge loop) never opened at all -- 4 of that file's 24 bash fences were
# invisible to it, and every fence in `.claude/skills/code/SKILL.md` was. Four
# other modules each rediscovered and re-fixed that independently before
# lode-ovgs unified three of them here, lode-p4qb folded in the fourth
# (tests/test_assert_main_checkout.py's text-gate half, since split out to
# tests/test_land_skill_guard_coverage.py by lode-2thl), and lode-jm4a folded
# in the fifth
# (tests/test_sweep_digest_id.py).
#
# Consumers of the FUNCTION: tests/test_land_lock.py,
# tests/test_land_conflicts_state.py, tests/test_skill_bash_state.py,
# tests/test_land_skill_guard_coverage.py, tests/test_sweep_digest_id.py.
# tests/test_bd_list_limit_gate.py's inline-span scan (`inline_violations`) is
# a further consumer -- of `fence_scan` directly, since lode-kjei collapsed its
# own open/close loop onto the same generator `bash_fence_blocks` is now built
# on (see `fence_scan` below). A change to the rules stated in `fence_scan`'s
# docstring changes what every gate above considers "executed"/"fenced", so the
# rules are stated ONCE there and nowhere else -- on the TEST side.
# `scripts/check_links.py`'s `_content_lines` makes the same "single home of
# the fence rule" claim for its own two consumers (the heading and link
# scanners) -- deliberately a SEPARATE single home, not a competing one: it is
# production code and cannot import anything under `tests/` (lode-jm4a). Two
# homes, each sole owner of its own side of the import boundary.


# A markdown blockquote marker: optional leading whitespace, one `>`, one
# optional following space -- CommonMark's own blockquote-marker shape.
# Stripped from EVERY line (fence delimiters AND content) before either is
# matched, so a fence nested inside a blockquote (`> ```bash`) is an ordinary
# fence, and its CONTENT lines lose their own literal `> ` prefix too --
# without that second half, a same-block assign-then-use pair like
# `> REPO_ROOT=...` / `> echo "$REPO_ROOT"` would still show REPO_ROOT as
# unassigned, since the leading `> ` defeats the `^`-anchored assignment
# regexes in tests/test_skill_bash_state.py (lode-wroz). Doing it HERE means every
# caller gets the fix, not just one. Since lode-kjei there is exactly ONE strip site on
# any PARSING path -- `fence_scan` below, which every consumer (fenced and inline alike)
# reads its lines through -- so the "both paths must unmark to the same shape" hazard this comment used
# to describe is structural rather than a convention: there is no second pass left to
# disagree. That also keeps lode-3pyo's finding moot: stripping twice is a no-op on
# today's corpus, measured, but not in general -- a `>>`-leading line double-strips to a
# bare one -- and nothing strips twice any more. (tests/test_land_lock.py's independent
# fence COUNTER strips through this same constant off-path, by design: it must not call
# `fence_scan` at all, and must not re-type the marker shape either -- lode-bi9h.)
_BLOCKQUOTE_MARKER = re.compile(r"^[ \t]*>[ \t]?")

# A fence marker: three-or-more backticks, or three-or-more tildes, plus
# whatever info string follows (lode-p4qb). Deliberately the same ALTERNATION
# as ``scripts/check_links.py``'s ``_FENCE_RE`` -- but re-declared, not
# imported. TWO state machines consume a marker of this shape and they do not
# agree. This one is `fence_scan` below, the single partitioner every test-side
# consumer now runs through (lode-kjei folded tests/test_bd_list_limit_gate.py's
# inline-span scan into it, so it no longer keeps a loop -- or an import of this
# constant -- of its own). ``check_links.py`` toggles on ANY marker, so there a
# ``~~~`` line does close a ```-opened block; do not read that one as
# documentation for this one.
_FENCE_MARKER_RE = re.compile(r"^(`{3,}|~{3,})(.*)$")


def _closes_fence(stripped: str, fence: str) -> bool:
    """Whether ``stripped`` closes an open ``fence`` -- CommonMark's closing
    rule, stated in ``fence_scan``'s docstring below. Kept a named helper, though
    ``fence_scan`` is now its only caller, because that docstring's
    unterminated-fence and four-backtick rules cite it by name.
    """
    return len(stripped) >= len(fence) and set(stripped) == {fence[0]}


def fence_scan(
    markdown: str,
) -> Iterator[tuple[int, str, str | None, int]]:
    """Partition every CONTENT line of ``markdown`` by which fence, if any, it
    sits inside -- the ONE state machine both ``bash_fence_blocks`` (fenced
    ```bash/```sh execution) and ``tests/test_bd_list_limit_gate.py``'s
    ``inline_violations`` (inline-backtick prose) are built on, so the two
    partition one document identically by construction rather than by two
    loops staying in sync by hand (lode-kjei).

    Yields ``(lineno, line, enclosing_info, block_ordinal)`` per content
    line, in document order:

    * ``lineno`` -- 1-based, into the ORIGINAL ``markdown`` (not shifted by
      any fence removal).
    * ``line`` -- the line with one leading blockquote marker removed
      (``_BLOCKQUOTE_MARKER``), matching ``bash_fence_blocks``'s existing
      normalization -- a fence nested inside a blockquote is an ordinary
      fence. Only that marker is removed; leading/trailing whitespace inside a
      fenced block survives untouched, so a caller matching *content* against a
      pattern normally ``.strip()``s it itself.
    * ``enclosing_info`` -- the still-open fence's info string (``"bash"``,
      ``"text"``, ``""`` for a bare fence with nothing after it, ...) for a
      line INSIDE a fence, or ``None`` for a line outside every fence.
    * ``block_ordinal`` -- 0-based count of fence blocks opened so far in the
      document. Meaningful only while ``enclosing_info is not None``; lets a
      caller regroup content lines by which physical fence produced them
      without re-deriving fence boundaries of its own.

    A fence DELIMITER line itself (the opening ```` ```lang ```` or the
    closing ```` ``` ````) is never yielded -- neither consumer has ever
    wanted the delimiter text, only what is inside it.

    Matches the fence marker on the stripped line, never
    ``line.startswith("```")``: a fence indented under a markdown list item is
    legitimately indented, and a column-0-anchored scanner reports such a file
    as carrying no bash at all -- the lode-ovgs bug. Measured on
    ``.claude/skills/code/SKILL.md``, the one consumed file that exercises
    both shapes: of its nine bash fences, five are plainly indented, three are
    indented AND inside a blockquote, and one (line 65) is a top-level
    blockquote with no indentation at all. Not one of the nine puts its
    backticks at column 0, which is why a column-0 scanner sees zero blocks
    there.

    Three further rules, all settled by lode-p4qb and all latent on today's
    corpus -- zero instances of any of them exist in any of the repo's
    markdown files, measured, so this is hardening rather than a live-bug fix:

    * a FOUR-OR-MORE-backtick fence and a TILDE (``~~~bash``) fence are both
      scanned (see ``_FENCE_MARKER_RE`` above), not silently skipped.
    * a closing run must be the SAME character as the opening one and AT LEAST
      AS LONG (CommonMark), so a ```-prefixed line inside a four-backtick
      block is content, not a close -- which is the whole reason an author
      reaches for the four-backtick form.
    * an UNTERMINATED final fence is FLUSHED, not dropped. Dropping is the
      same false-assurance shape this generator exists to delete: a gate
      would report "clean" for a block it never parsed.

    **Only ONE fence is ever tracked open at a time (lode-kjei)** -- this is
    what closes the asymmetry that survived lode-xqc7's constant-sharing: the
    old ``bash_fence_blocks`` opened ``current`` (its own tracking state) only
    on a bash/sh info string, so it never recorded that it was already inside
    an ENCLOSING non-bash fence, and read a ```bash run nested inside a
    ````text block as executable -- a false POSITIVE, not a silent miss
    (measured latent: zero nested fence openers across all 58 tracked .md
    files, 203 top-level fences scanned, at the time this was found). This
    state machine tracks the CURRENTLY OPEN fence regardless of its info
    string, so a ```bash-looking line encountered while ``enclosing_info`` is
    already ``"text"`` is correctly reported as a content line of that outer
    ``text`` fence (i.e. never opens its own nested tracking), matching what
    CommonMark itself does: a fence cannot open inside an already-open fence,
    only close it (or fail to, and remain content).

    One remaining known boundary, and the only one left of the OPPOSITE kind
    -- corruption rather than a silent skip: the blockquote strip cannot tell
    a blockquote marker from a redirection, so it also fires on a CONTENT line
    whose first non-blank character is ``>``. ``>&2 echo hi`` extracts as
    ``&2 echo hi``, ``>> log`` as ``> log``, ``> out`` as ``out``. Unlike the
    three above this is silent CORRUPTION, not a silent skip, so a gate
    asserting on exact command text would assert against the mangled form.
    Measured under lode-wroz: no consumed file has such a line today -- every
    block the pre-strip parser saw is byte-identical after it, across every
    ``.claude/skills/*/SKILL.md`` and ``.claude/agents/*.md`` -- so re-measure
    rather than assume if one is ever added.
    """
    fence = ""  # the opening run, e.g. "```" or "````" or "~~~"
    info: str | None = None
    ordinal = -1
    for lineno, raw_line in enumerate(markdown.splitlines(), 1):
        line = _BLOCKQUOTE_MARKER.sub("", raw_line, count=1)
        stripped = line.strip()
        if fence:
            if _closes_fence(stripped, fence):
                fence = ""
                info = None
                continue
            yield (lineno, line, info, ordinal)
            continue
        m = _FENCE_MARKER_RE.match(stripped)
        if m:
            fence = m.group(1)
            info = m.group(2).strip()
            ordinal += 1
            continue
        yield (lineno, line, None, ordinal)


def bash_fence_blocks(markdown: str) -> list[str]:
    """Every fenced ```bash/```sh block in ``markdown``, as separate strings,
    in document order -- what an agent actually EXECUTES, one Bash tool
    invocation per block.

    Built on :func:`fence_scan` (lode-kjei): groups its content lines whose
    ``enclosing_info`` is ``"bash"`` or ``"sh"`` by ``block_ordinal``, in
    document order. A caller that wants every block concatenated into one
    string (e.g. to check for an offending token whose position within the
    file doesn't matter) can ``"\\n".join(bash_fence_blocks(markdown))`` the
    result. See :func:`fence_scan`'s docstring for the full rule set
    (indentation, blockquotes, four-backtick/tilde fences, unterminated
    fences, the nested-fence fix, and the known blockquote/redirection
    corruption boundary) -- stated once, there, not duplicated here.
    """
    blocks: list[str] = []
    current: list[str] = []
    current_ordinal: int | None = None
    for _lineno, line, info, ordinal in fence_scan(markdown):
        if info not in {"bash", "sh"}:
            continue
        if ordinal != current_ordinal:
            if current_ordinal is not None:
                blocks.append("\n".join(current))
            current = []
            current_ordinal = ordinal
        current.append(line)
    if current_ordinal is not None:  # unterminated final fence -- flushed
        blocks.append("\n".join(current))
    return blocks


def only_block_with(blocks: list[str], *needles: str, what: str) -> str:
    """The single block in ``blocks`` containing every needle -- asserts exactly
    one (lode-pm37).

    Asserts exactly one hit rather than taking ``next(..., None)``, which would
    silently pin the first of several near-identical-looking blocks; each
    caller's docstring names the live pair it would misfire on.

    Takes ``blocks`` rather than a markdown path so it composes with either
    caller's own ``_skill_blocks()`` (each closes over a different SKILL.md via
    :func:`bash_fence_blocks`), instead of this function picking the file.
    """
    hits = [b for b in blocks if all(n in b for n in needles)]
    assert len(hits) == 1, (
        f"expected exactly 1 fenced block for {what}, found {len(hits)} -- this "
        "test's assumption about SKILL.md's structure has drifted; re-check by "
        "hand before adjusting the locator"
    )
    return hits[0]


def fake_bin_env(bin_dir: Path) -> dict[str, str]:
    """``os.environ``, overlaid so ``bin_dir`` is first on ``PATH``.

    How a test puts a fake tool (usually a fake ``bd``) in front of the real
    one for a subprocess under test: the real environment untouched, except
    ``PATH`` gaining ``bin_dir`` at the front. Callers needing further keys
    overlay them on the result, as :func:`run_block` does with ``TMPDIR``.

    An unset ``PATH`` yields ``bin_dir`` alone rather than a trailing empty
    entry, which POSIX reads as the current directory.
    """
    existing = os.environ.get("PATH", "")
    path = f"{bin_dir}{os.pathsep}{existing}" if existing else str(bin_dir)
    return dict(os.environ, PATH=path)


def run_block(
    block: str, sweep_tmp: Path, bin_dir: Path
) -> subprocess.CompletedProcess[str]:
    """Run one fenced ```bash block as its own, fresh subprocess (lode-n6q0).

    This is the execution convention every test that runs a skill's real
    fenced blocks (rather than merely locating them, see
    :func:`only_block_with`) is built on: one fresh ``bash`` subprocess PER
    block, mirroring an agent's own one-Bash-tool-invocation-per-fence
    execution model, so nothing a block sets in its own shell survives into
    the next one (lode-sfnb/lode-x495). ``bin_dir`` is prepended to ``PATH``
    ahead of the real one -- the caller's fake ``bd`` (or other faked tool)
    lives there, via :func:`fake_bin_env`. ``TMPDIR`` is redirected to
    ``sweep_tmp``'s parent so a block's own ``${TMPDIR:-/tmp}/lode-sweep-state``
    derivation lands exactly on the ``sweep_tmp`` fixture's directory -- that
    derivation is ``/sweep``'s §0 convention SPECIFICALLY, so a caller testing
    a different skill's blocks inherits a redirection it did not ask for.
    ``cwd`` is the checkout root (:data:`_CHECKOUT_ROOT`, worktree-aware)
    rather than a hand-rolled ``Path(__file__).parent.parent`` in each caller.
    """
    env = dict(fake_bin_env(bin_dir), TMPDIR=str(sweep_tmp.parent))
    return subprocess.run(
        ["bash", "-c", block],
        capture_output=True,
        text=True,
        env=env,
        cwd=_CHECKOUT_ROOT,
        check=False,
    )


@pytest.fixture
def sweep_tmp(tmp_path: Path) -> Path:
    """Mirrors ``/sweep``'s own §0 layout: ``$SWEEP_TMP =
    $TMPDIR/lode-sweep-state`` (lode-n6q0).

    Shared by every pin of that layout, so a future change to it moves here
    once.
    """
    d = tmp_path / "lode-sweep-state"
    d.mkdir()
    return d


def _fenced_bash(markdown: str) -> str:
    """The ```bash fences only, concatenated into one string -- what an agent
    actually EXECUTES.

    Scanning the whole file would also match prose that merely *describes* a
    command (quoting it while explaining a past defect), so a pin that wants
    to assert something about what actually runs has to separate the executed
    fences from the surrounding description. That split is also the point:
    the fence is the one part of these skill docs no other gate parses, which
    is how more than one bug here survived unnoticed until someone read the
    file by hand.

    ``tests/test_land_lock.py`` and ``tests/test_assert_main_checkout.py``
    each carried their own copy of this until lode-0mkv; the parser's rules
    and blind spots live next to :func:`bash_fence_blocks`, not here.
    """
    return "\n".join(bash_fence_blocks(markdown))


#: The land skill doc. Three of its readers parse its fenced bash blocks (via
#: :func:`bash_fence_blocks`/:func:`_fenced_bash` above), which is why it lives
#: here; test_worktree_gc_classify.py reads the same file but scans its
#: ``case "$BUCKET"`` dispatch directly rather than through the fence parser.
#: Was defined byte-identically in four modules (test_worktree_gc_classify.py,
#: test_land_conflicts_state.py, test_land_lock.py,
#: test_assert_main_checkout.py) until lode-va47 consolidated it here. The last
#: of those has since been split (lode-2thl) and no longer references
#: LAND_SKILL at all; its text-gate half is test_land_skill_guard_coverage.py.
LAND_SKILL = _CHECKOUT_ROOT / ".claude" / "skills" / "land" / "SKILL.md"

#: The land skill doc's text, read once per session rather than once per test
#: (lode-6mt3/lode-ulkf). ``tests/test_land_lock.py`` alone carried eleven
#: static ``LAND_SKILL.read_text(encoding="utf-8")`` call sites against this
#: 174KB file -- and more reads than that per run, since two of them sit in
#: per-test block locators (``_acquire_block``/``_pass_start_block``) rather
#: than in a test body. Every call site there now reads this cached value
#: instead.
#: A test that is pinning the *parser itself*
#: (``test_fenced_bash_sees_every_bash_marker_including_indented_ones``) still
#: calls :func:`bash_fence_blocks` directly on this text rather than going
#: through :data:`LAND_SKILL_BLOCKS`/:data:`LAND_SKILL_BASH` below, since the
#: point of that test is to exercise the parser, not to reuse a pre-parsed
#: result.
LAND_SKILL_TEXT = LAND_SKILL.read_text(encoding="utf-8")

#: :func:`bash_fence_blocks` applied to :data:`LAND_SKILL_TEXT` once per
#: session. See :data:`LAND_SKILL_TEXT` above for why this is cached at all.
LAND_SKILL_BLOCKS = bash_fence_blocks(LAND_SKILL_TEXT)

#: :func:`_fenced_bash`'s result on :data:`LAND_SKILL_TEXT` once per session --
#: equivalently, ``"\n".join(LAND_SKILL_BLOCKS)``. See :data:`LAND_SKILL_TEXT`
#: above for why this is cached at all.
LAND_SKILL_BASH = "\n".join(LAND_SKILL_BLOCKS)

#: The sweep skill doc, derived the same way as LAND_SKILL above. Was
#: hand-derived independently in every tests/ module that pins a sweep block
#: (five of them by then) until lode-b8jc consolidated it here. Import this
#: rather than re-deriving it: the duplication had already re-forked once, on a
#: file that landed after the first consolidation attempt was written.
SWEEP_SKILL = _CHECKOUT_ROOT / ".claude" / "skills" / "sweep" / "SKILL.md"

#: The code-reviewer agent definition, derived the same way as LAND_SKILL /
#: SWEEP_SKILL above. Added by lode-xdg3's technical review, whose
#: ``tests/test_validate_sha40_call_sites.py`` is the first module to pin a
#: fenced bash block in an ``.claude/agents/*.md`` file rather than a
#: ``SKILL.md`` -- ``tests/test_no_hand_derived_skill_md_path.py`` covers both
#: roots, so the constant belongs here for the same reason the skill ones do.
CODE_REVIEWER_AGENT = _CHECKOUT_ROOT / ".claude" / "agents" / "code-reviewer.md"

#: :func:`bash_fence_blocks` over :data:`CODE_REVIEWER_AGENT`, once per session.
#: See :data:`LAND_SKILL_TEXT` for why this is cached at all. No separate
#: ``_TEXT`` constant: unlike ``LAND_SKILL_TEXT`` (several modules read the raw
#: prose), nothing needs the text itself yet, so the read is inlined here rather
#: than exported dead.
CODE_REVIEWER_AGENT_BLOCKS = bash_fence_blocks(
    CODE_REVIEWER_AGENT.read_text(encoding="utf-8")
)

#: The sweep skill doc's text, read once per session rather than once per test
#: (lode-pxwn) -- the same fix LAND_SKILL_TEXT above applied to LAND_SKILL.
#: All five tests/test_sweep_*.py modules that previously called
#: ``SWEEP_SKILL.read_text(encoding="utf-8")`` directly now read this instead.
SWEEP_SKILL_TEXT = SWEEP_SKILL.read_text(encoding="utf-8")

#: :func:`bash_fence_blocks` applied to :data:`SWEEP_SKILL_TEXT` once per
#: session. See :data:`SWEEP_SKILL_TEXT` above for why this is cached at all.
SWEEP_SKILL_BLOCKS = bash_fence_blocks(SWEEP_SKILL_TEXT)

# DECISION (lode-pxwn) -- deliberately a plain `#` block, not the `#:`
# attribute-doc form used above: it documents no single constant, and an `#:`
# run here would silently become the rendered doc for whatever constant is
# added below it next.
#
# Why the per-skill constants above rather than a generic
# ``@functools.cache`` on :func:`bash_fence_blocks`: that function returns a
# plain, MUTABLE ``list[str]``, so decorating it would hand every caller in
# the session the *same* list object -- an unenforced contract across ~10
# modules that a future caller could break by mutating its "own" result. The
# constants above (``LAND_SKILL_BLOCKS``/``LAND_SKILL_BASH``,
# ``SWEEP_SKILL_BLOCKS``) narrow that exposure to a fixed, reviewable set
# computed once at import time from a fixed input.
#
# They do NOT eliminate it, and the honest version of the read-only claim is:
# every consumer today is read-only EXCEPT
# ``tests/test_land_conflicts_state.py::test_section_3_regate_precedes_push_is_sabotage_proven``,
# which copies (``sabotaged = list(blocks)``) before reordering. That copy was
# incidental when each call re-parsed the file; it is load-bearing now, and is
# annotated as such at its own call site.
#
# A module that gains its own fresh SKILL.md to pin should add its own
# ``<NAME>_TEXT``/``<NAME>_BLOCKS`` pair here, following this same shape,
# rather than reaching for a shared cached helper. Making
# :func:`bash_fence_blocks` return a ``tuple[str, ...]`` would enforce the
# contract instead of documenting it; it was left alone here because it also
# changes the parser's own pinned equality assertions, well outside a
# tests-only hoist (tracked separately).


# --- TUI test settle helpers (lode-lcju) -----------------------------------
#
# The ONE home for both of lode's settle-under-load patterns for driving a
# Textual pilot -- see docs/tui.md's "Settling TUI tests under load" section
# for the ruling (which helper applies when) and the verified mechanism
# (wait_for_idle's CPU-vs-wall-clock heuristic is the only load-sensitive
# element in the path; asyncio's ready-queue ordering is not perturbed by OS
# starvation). Moved here verbatim from tests/test_tui_reconcile_screen.py
# (_wait_until, lode-64jn) and tests/test_tui_browse_screen.py
# (_press_and_settle, lode-9y68), which had independently invented the same
# fix twice with no cross-reference. Do not add a third dialect in a new test
# file -- import one of these two instead.

#: How often :func:`_wait_until` re-checks its predicate.
_POLL_INTERVAL = 0.01


async def _wait_until(
    predicate: Callable[[], bool], description: str, *, timeout: float = 5.0
) -> None:
    """Poll ``predicate`` until true, bounded by a real ``timeout`` (lode-64jn).

    Use for a PRECONDITION (e.g. "the new screen has finished composing"),
    never for the test's own expected assertion value -- see docs/tui.md's
    "Settling TUI tests under load" section (lode-lcju) for the full rule and
    why: baking the assertion's expected value into the predicate is the
    retry-on-assertion antipattern, which masks a real bug as a slow-to-settle
    one instead of failing where it happens.

    ``description`` names the condition, so a timeout says *which* wait hung.

    Yields the event loop via ``asyncio.sleep`` between checks -- a genuine
    cooperative yield -- rather than Textual's ``pilot.pause()`` no-arg form,
    which ultimately waits on a CPU-idle *heuristic*
    (``textual._wait.wait_for_idle``): it compares this process's own CPU time
    against wall-clock time and calls it "idle" once CPU time stops advancing.
    Under real machine contention (several agents gating at once, e.g.
    ``/code`` fan-out) that heuristic can misfire -- a process starved of
    scheduler time by unrelated load barely advances its own CPU time
    *regardless* of whether the screen transition it is supposed to be waiting
    for has finished, and the heuristic reads that starvation as idleness.
    Polling the real condition instead waits exactly as long as it takes, and
    fails loudly (an explicit ``AssertionError``, never a silent false-idle
    pass) if the condition genuinely never becomes true.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"timed out after {timeout}s waiting until {description}"
            )
        await asyncio.sleep(_POLL_INTERVAL)


async def _press_and_settle(pilot: Pilot, *keys: str) -> None:
    """Press each key one at a time, settling after EACH one (lode-9y68).

    Use for a STATEFUL, read-back keystroke cascade -- a later key's behavior
    depends on state an earlier key's cascade produced (e.g. incremental
    search, where a scan reads the cursor's current row as its start) -- where
    a precondition predicate would have to restate the test's own expected
    value and so ``_wait_until`` is the wrong tool. See docs/tui.md's
    "Settling TUI tests under load" section (lode-lcju) for the full rule.

    ``pilot.press(*keys)`` (``pilot.py``) is::

        await self._app._press_keys(keys)   # ALL keys, paced by heuristic only
        await self._wait_for_screen()       # ONE real drain, at the very end

    ``App._press_keys`` (``app.py``) paces BETWEEN keystrokes with nothing but
    ``wait_for_idle()`` -- and that is a wall-clock-vs-``process_time()``
    comparison, so a process merely *starved of timeslices* (this machine
    legitimately runs several concurrent ``nox -s tests`` invocations) reads as
    "idle" while a cascade is still in flight. The real drain comes only once,
    after the last key. So the next key can be dispatched mid-cascade.

    The fix is just to press ONE key per call: ``pilot.press(key)`` ends with
    its own ``_wait_for_screen()``, so a real message-count drain -- not the
    CPU heuristic -- separates every keystroke from the next.

    NARROW BY DESIGN: a plain multi-key ``pilot.press("down", "down", "down")``
    is fine and deliberately left alone where used -- cursor moves are
    order-preserving and carry no read-back dependency between keys, and
    ``press()``'s trailing drain covers the final read. The trigger is the
    stateful read-back above, not multi-key presses in general.

    The trailing ``pilot.pause()`` below is NOT part of that mechanism -- it is
    just this suite's ordinary post-keystroke dialect, kept so call sites read
    like their ~90 siblings. ``press(key)`` alone is already sufficient. Do not
    read it as load-bearing, and do not add more drains to settle a future
    flake: a fixed count of drains neither waits longer under worse load nor
    reports anything when it is insufficient. ``wait_for_idle``'s clock
    comparison is the only load-sensitive element in this path.
    """
    for key in keys:
        await pilot.press(key)
        await pilot.pause()
