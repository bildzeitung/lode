"""Measure VectorStore memory and per-call time across lancedb Session settings (lode-3gr3).

Offline: writes 5 random 768-float rows per call into a temp LanceDB directory, with
``target_version`` cycling over 50 values. No provider call is made.

Modes (``--mode``):
  reconnect  the lode-ju9d behaviour: default Session, Table dropped after each prune
  held       default Session, Table held, prune in the loop (what lode-ju9d replaced)
  capped     ``lancedb.Session(metadata_cache_size_bytes=--cap)``, Table held, prune in the loop

Run: uv run --frozen python scripts/measure_vectorstore_session.py --mode capped --calls 3000
"""

import random
import tempfile
import time
from datetime import timedelta
from typing import Annotated

import lancedb
import pyarrow as pa
import typer

app = typer.Typer(add_completion=False)

DIM = 768
ROWS = 5
VERSIONS = 50


def _rss_mb() -> float:
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return float("nan")


def _schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("passage_id", pa.string()),
            pa.field("target_version", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), DIM)),
        ]
    )


@app.command()
def main(
    mode: Annotated[str, typer.Option(help="reconnect | held | capped")] = "capped",
    calls: Annotated[int, typer.Option()] = 3000,
    interval: Annotated[int, typer.Option(help="prune every N calls")] = 200,
    cap: Annotated[int, typer.Option(help="metadata cache bytes (capped)")] = 8 * 2**20,
    report_every: Annotated[int, typer.Option()] = 300,
) -> None:
    """Run one measurement and print RSS and per-call times."""
    rng = random.Random(0)
    schema = _schema()
    tmp = tempfile.mkdtemp(prefix="lode-3gr3-")

    def connect():
        if mode == "capped":
            session = lancedb.Session(
                index_cache_size_bytes=cap, metadata_cache_size_bytes=cap
            )
            return lancedb.connect(tmp, session=session)
        return lancedb.connect(tmp)

    def open_table():
        db = connect()
        return db.create_table("embeddings", schema=schema, exist_ok=True)

    table = open_table()
    since = 0
    write_t: list[float] = []
    read_t: list[float] = []
    print(f"mode={mode} calls={calls} interval={interval} cap={cap}")
    print(f"call 0 rss_mb={_rss_mb():.1f}")
    for i in range(1, calls + 1):
        tv = f"{i % VERSIONS:064x}"
        rows = [
            {
                "passage_id": f"{tv}:{j}",
                "target_version": tv,
                "vector": [rng.random() for _ in range(DIM)],
            }
            for j in range(ROWS)
        ]
        t0 = time.perf_counter()
        table.checkout_latest()
        table.delete(f"target_version = '{tv}'")
        table.add(rows)
        since += 1
        if since >= interval:
            table.optimize(cleanup_older_than=timedelta(seconds=60))
            since = 0
            if mode == "reconnect":
                table = open_table()
        write_t.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        table.checkout_latest()
        table.search(rows[0]["vector"]).metric("cosine").limit(5).to_list()
        table.search().where(f"target_version = '{tv}'").limit(100).to_list()
        read_t.append(time.perf_counter() - t0)
        if i % report_every == 0:
            print(f"call {i} rss_mb={_rss_mb():.1f}")
    write_t.sort()
    read_t.sort()
    n = len(write_t)
    print(
        f"write ms median={write_t[n // 2] * 1e3:.2f} p95={write_t[int(n * 0.95)] * 1e3:.2f}"
        f" | read(2 queries) ms median={read_t[n // 2] * 1e3:.2f}"
        f" p95={read_t[int(n * 0.95)] * 1e3:.2f}"
    )


if __name__ == "__main__":
    app()
