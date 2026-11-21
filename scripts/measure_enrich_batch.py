"""Measure enrichment output tokens, truncation and empty-{} rate on the BATCH route.

Reproduces lode-eadh's measurement (the batch-route twin of lode-qcth's immediate-route
run). It makes LIVE Anthropic batch calls, so CLAUDE.md directive 10 binds: run it only
with a maintainer spend approval on the ticket, and only with ``LODE_HOME`` pointed at a
scratch directory, never ``~/.lode`` (the script refuses otherwise).

It submits through the real ``enrich._build_batch_request`` and
``AnthropicProvider.submit_batch``, so the wire shape is the production one, then reads
the raw Batches results to capture ``usage.output_tokens`` and ``stop_reason``, which
``BatchResult`` discards. Source notes are read from ``--source-db`` (read-only), gated
and redacted the way ``submit_enrich_batch`` does; a doc-passage proxy fills out the
call count with long text.
"""

import json
import os
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Annotated

import typer

from lode.config import Settings, load_settings
from lode.enrich import _build_batch_request, _resolve_enrich_target
from lode.llm_provider import AnthropicProvider, build_provider
from lode.redact import redact_before_egress_counting

app = typer.Typer(add_completion=False)


def _real_bodies(source_db: Path, settings: Settings) -> list[tuple[str, str]]:
    uri = f"file:{source_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT version_id FROM versions WHERE op != 'delete' "
                "AND purged_at IS NULL"
            )
        ]
        out: list[tuple[str, str]] = []
        for vid in ids:
            target = _resolve_enrich_target(conn, vid, settings.no_egress_scopes)
            if target is None or not target.live or target.no_egress:
                continue
            body, _ = redact_before_egress_counting(target.body, settings)
            out.append((f"note-{vid[:8]}", body))
        return out
    finally:
        conn.close()


def _doc_passages(docs_dir: Path, count: int) -> list[tuple[str, str]]:
    sizes = [300, 1500, 4000, 8000, 12000, 16000]
    files = sorted(docs_dir.glob("*.md"))
    out: list[tuple[str, str]] = []
    for i in range(count):
        f = files[i % len(files)]
        text = f.read_text()
        size = sizes[i % len(sizes)]
        start = (
            (len(text) // (count + 1))
            * (i // len(files) + 1)
            % max(1, len(text) - size)
        )
        out.append((f"doc-{i:02d}-{f.stem}", text[start : start + size]))
    return out


@app.command()
def main(
    source_db: Annotated[Path, typer.Option(help="lode.db to draw real notes from.")],
    docs_dir: Annotated[Path, typer.Option(help="Directory of *.md proxy passages.")],
    repeats: Annotated[int, typer.Option(help="Calls per real note.")] = 3,
    proxy_count: Annotated[int, typer.Option(help="Doc-passage calls.")] = 12,
    out_jsonl: Annotated[Path | None, typer.Option(help="Per-call records.")] = None,
    poll_s: Annotated[float, typer.Option(help="Seconds between polls.")] = 20.0,
) -> None:
    """Run the batch-route measurement and print the summary."""
    home = os.environ.get("LODE_HOME", "")
    if not home or Path(home).resolve() == (Path.home() / ".lode").resolve():
        raise typer.BadParameter("LODE_HOME must be a scratch dir, not ~/.lode")

    settings = load_settings()
    items = [
        (f"{cid}-r{r}", body)
        for cid, body in _real_bodies(source_db, settings)
        for r in range(repeats)
    ]
    items += _doc_passages(docs_dir, proxy_count)
    requests = [_build_batch_request(cid, body, settings) for cid, body in items]
    typer.echo(f"submitting {len(requests)} batch request(s)")

    provider = build_provider(settings)
    assert isinstance(provider, AnthropicProvider)
    batch_id = provider.submit_batch(requests, timeout_s=settings.enrich_call_timeout_s)
    typer.echo(f"batch {batch_id}")
    client = provider._client  # raw results: BatchResult drops usage/stop_reason
    while client.beta.messages.batches.retrieve(batch_id).processing_status != "ended":
        time.sleep(poll_s)

    chars = {cid: len(body) for cid, body in items}
    records = []
    for res in client.beta.messages.batches.results(batch_id):
        rec: dict[str, object] = {
            "custom_id": res.custom_id,
            "chars": chars.get(res.custom_id),
            "result_type": res.result.type,
        }
        if res.result.type == "succeeded":
            msg = res.result.message
            text = "".join(b.text for b in msg.content if b.type == "text")
            rec |= {
                "stop_reason": msg.stop_reason,
                "output_tokens": msg.usage.output_tokens,
                "input_tokens": msg.usage.input_tokens,
                "thinking_blocks": sum(b.type == "thinking" for b in msg.content),
                "empty": text.strip() in ("", "{}"),
            }
        records.append(rec)
    if out_jsonl:
        out_jsonl.write_text("".join(json.dumps(r) + "\n" for r in records))

    ok = [r for r in records if r["result_type"] == "succeeded"]
    outs = [int(r["output_tokens"]) for r in ok]  # type: ignore[call-overload]
    ins = [int(r["input_tokens"]) for r in ok]  # type: ignore[call-overload]
    trunc = sum(r["stop_reason"] == "max_tokens" for r in ok)
    empty = sum(bool(r["empty"]) for r in ok)
    typer.echo(
        f"calls={len(records)} succeeded={len(ok)} "
        f"non-succeeded={len(records) - len(ok)} "
        f"truncated={trunc} empty={empty} "
        f"thinking_blocks={sum(int(r['thinking_blocks']) for r in ok)}"  # type: ignore[call-overload]
    )
    if outs:
        typer.echo(
            f"output_tokens min={min(outs)} median={statistics.median(outs)} "
            f"max={max(outs)} total={sum(outs)}; input min={min(ins)} "
            f"max={max(ins)} total={sum(ins)}"
        )
    typer.echo(
        "stop_reasons="
        + json.dumps(
            {
                k: sum(r.get("stop_reason") == k for r in ok)
                for k in {r["stop_reason"] for r in ok}
            }
        )
    )


if __name__ == "__main__":
    app()
