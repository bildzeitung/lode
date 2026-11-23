"""Measure VectorStore memory and per-call time across lancedb Session settings (lode-3gr3).

Offline, and Linux only (it reads ``VmRSS`` from ``/proc``). Drives
``lode.vectorstore.VectorStore`` on a temp LanceDB directory. Each call is one
``replace_vectors`` of 5 random rows at the pinned vector width, with
``target_version`` cycling over 50 values, and, unless ``--no-reads``, one ``search``
and one ``vectors_for``, each on a new ``VectorStore`` as the shipped read callers
build one. No provider call is made.

Modes (``--mode``):
  reconnect  ``VectorStore`` as shipped: default Session, Table dropped after each prune
  held       default Session, Table kept across the prune (what lode-ju9d replaced)
  capped     Table kept across the prune, and every connection in the process gets its
             own ``lancedb.Session`` with both caches capped at ``--cap`` bytes

Run: uv run --frozen python scripts/measure_vectorstore_session.py --mode capped
"""

import random
import tempfile
import time
from contextlib import ExitStack
from enum import StrEnum
from typing import Annotated, Any
from unittest import mock

import lancedb
import typer

from lode.config import Settings
from lode.vectorstore import VectorStore

app = typer.Typer(add_completion=False)

ROWS = 5
VERSIONS = 50


class Mode(StrEnum):
    RECONNECT = "reconnect"
    HELD = "held"
    CAPPED = "capped"


class _HeldStore(VectorStore):
    """A ``VectorStore`` that keeps the Table ``replace_vectors`` drops after its prune."""

    def replace_vectors(
        self, target_version: str, rows: list[dict[str, object]]
    ) -> None:
        table = self._table
        super().replace_vectors(target_version, rows)
        if self._table is None:
            self._table = table


def _capped_connect(cap: int) -> Any:
    connect = lancedb.connect

    def capped(uri: Any) -> Any:
        session = lancedb.Session(
            index_cache_size_bytes=cap, metadata_cache_size_bytes=cap
        )
        return connect(uri, session=session)

    return capped


def _rss_mb() -> float:
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return float("nan")


def _summary(seconds: list[float]) -> str:
    ordered = sorted(seconds)
    n = len(ordered)
    return (
        f"median={ordered[n // 2] * 1e3:.2f} p95={ordered[int(n * 0.95)] * 1e3:.2f}"
        f" mean={sum(ordered) / n * 1e3:.2f}"
    )


@app.command()
def main(
    mode: Annotated[Mode, typer.Option()] = Mode.CAPPED,
    calls: Annotated[int, typer.Option(min=1)] = 3000,
    interval: Annotated[int, typer.Option(min=1, help="prune every N calls")] = 200,
    cap: Annotated[
        int, typer.Option(help="index and metadata cache bytes, each (capped)")
    ] = 8 * 2**20,
    rate: Annotated[
        float, typer.Option(min=0, help="calls per second; 0 runs unpaced")
    ] = 15.0,
    reads: Annotated[bool, typer.Option(help="run the two reads per call")] = True,
    report_every: Annotated[int, typer.Option(min=1)] = 300,
) -> None:
    """Run one measurement and print RSS and per-call times."""
    rng = random.Random(0)
    settings = Settings(vectorstore_optimize_interval=interval)
    write_t: list[float] = []
    after_prune_t: list[float] = []
    read_t: list[float] = []
    with ExitStack() as stack:
        tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="lode-3gr3-"))
        if mode is Mode.CAPPED:
            stack.enter_context(
                mock.patch.object(lancedb, "connect", _capped_connect(cap))
            )
        store = (VectorStore if mode is Mode.RECONNECT else _HeldStore)(tmp, settings)
        print(
            f"mode={mode.value} calls={calls} interval={interval} cap={cap}"
            f" rate={rate} reads={reads}"
        )
        print(f"call 0 rss_mb={_rss_mb():.1f}")
        start = time.perf_counter()
        for i in range(1, calls + 1):
            began = time.perf_counter()
            tv = f"{i % VERSIONS:064x}"
            vectors = [
                [rng.random() for _ in range(settings.embedding_vector_dim)]
                for _ in range(ROWS)
            ]
            rows: list[dict[str, object]] = [
                {
                    "passage_id": f"{tv}:{j}",
                    "target_version": tv,
                    "vector": vector,
                    "model": settings.embedding_model,
                    "model_revision": None,
                }
                for j, vector in enumerate(vectors)
            ]
            t0 = time.perf_counter()
            store.replace_vectors(tv, rows)
            took = time.perf_counter() - t0
            write_t.append(took)
            # The call after a prune is the one that pays the reconnect.
            if i % interval == 1 and i > 1:
                after_prune_t.append(took)
            if reads:
                t0 = time.perf_counter()
                VectorStore(tmp, settings).search(vectors[0], k=5)
                VectorStore(tmp, settings).vectors_for(tv)
                read_t.append(time.perf_counter() - t0)
            if i % report_every == 0:
                elapsed = time.perf_counter() - start
                print(f"call {i} rss_mb={_rss_mb():.1f} elapsed_s={elapsed:.0f}")
            if rate:
                time.sleep(max(0.0, 1 / rate - (time.perf_counter() - began)))
    print(f"write ms {_summary(write_t)}")
    if after_prune_t:
        print(f"write ms on the call after each prune {_summary(after_prune_t)}")
    if read_t:
        print(f"read (2 queries) ms {_summary(read_t)}")


if __name__ == "__main__":
    app()
