"""Offline tests for scripts/measure_enrich_batch.py (lode-eadh).

The script sends real notes to a live provider and no gate ever runs it, so these
cover what can be checked with the provider stubbed: the scratch-``LODE_HOME``
refusal, which bodies leave the source DB, that the source DB is opened read-only,
and that everything sent is redacted.
"""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import fake_batch_client, load_module_from_path, structured_response
from typer.testing import CliRunner

from lode import versions
from lode.llm_provider import AnthropicProvider
from lode.storage import init_db

REPO_ROOT = Path(__file__).resolve().parent.parent

_mod = load_module_from_path(
    "measure_enrich_batch", REPO_ROOT / "scripts" / "measure_enrich_batch.py"
)
runner = CliRunner()

_SECRET = "AKIAIOSFODNN7EXAMPLE"  # seeded AWS-access-key-id pattern


def _seed(db_path: Path) -> None:
    """One sendable note, and one note per way a body must stay out of the batch."""
    conn = init_db(db_path)
    try:
        versions.save(conn, "kept", "kept body")
        first = versions.save(conn, "edited", "superseded body")
        versions.save(conn, "edited", "edited head body", parent=first.version_id)
        gone = versions.save(conn, "deleted", "deleted body")
        versions.delete(conn, "deleted", parent=gone.version_id)
        versions.save(conn, "purged", "purged body")
        versions.purge(conn, "purged")
        versions.save(conn, "private", "private body")
        versions.set_no_egress(conn, "private")
        versions.save(conn, "secret", f"note key {_SECRET} end")
    finally:
        conn.close()


def _succeeded(custom_id: str, text: str, stop_reason: str) -> SimpleNamespace:
    message = structured_response(
        None, text=text, stop_reason=stop_reason, output_tokens=10
    )
    message.usage.input_tokens = 100
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(type="succeeded", message=message),
    )


@pytest.mark.parametrize(
    "lode_home", [None, "", "~/.lode", "{home}/.lode", "{home}/link-to-lode"]
)
def test_refuses_the_real_lode_home(
    lode_home: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset, empty, unexpanded, literal and symlinked spellings of ~/.lode all refuse."""
    home = tmp_path / "home"
    (home / ".lode").mkdir(parents=True)
    (home / "link-to-lode").symlink_to(home / ".lode", target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    if lode_home is None:
        monkeypatch.delenv("LODE_HOME")
    else:
        monkeypatch.setenv("LODE_HOME", lode_home.format(home=home))
    read: list[Path] = []
    monkeypatch.setattr(_mod, "_real_bodies", lambda db, _settings: read.append(db))

    result = runner.invoke(
        _mod.app,
        ["--source-db", str(tmp_path / "lode.db"), "--docs-dir", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert "scratch" in result.output
    assert read == []


def test_sends_only_redacted_live_heads_and_never_writes_the_source_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "?" and "#" in the path: unescaped, either would cut the SQLite URI short and
    # drop its mode=ro.
    db_path = tmp_path / "a?b#c" / "lode.db"
    db_path.parent.mkdir()
    _seed(db_path)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "page.md").write_text(f"doc key {_SECRET} end")

    resolve = _mod._resolve_enrich_target

    def _resolve_on_a_read_only_connection(conn: sqlite3.Connection, *args: object):
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM versions")
        return resolve(conn, *args)

    monkeypatch.setattr(
        _mod, "_resolve_enrich_target", _resolve_on_a_read_only_connection
    )

    client = fake_batch_client("msgbatch_test")
    create = client.beta.messages.batches.create

    def _results(_batch_id: str):
        ids = [r["custom_id"] for r in create.call_args.kwargs["requests"]]
        yield SimpleNamespace(custom_id=ids[0], result=SimpleNamespace(type="errored"))
        yield _succeeded(ids[1], '{"tags": []', "max_tokens")
        yield _succeeded(ids[2], "{}", "end_turn")
        for custom_id in ids[3:]:
            yield _succeeded(custom_id, '{"tags": ["a"]}', "end_turn")

    client.beta.messages.batches.results.side_effect = _results
    monkeypatch.setattr(_mod, "build_provider", lambda _s: AnthropicProvider(client))
    out_jsonl = tmp_path / "out.jsonl"

    result = runner.invoke(
        _mod.app,
        [
            f"--source-db={db_path}",
            f"--docs-dir={docs_dir}",
            "--repeats=2",
            "--proxy-count=1",
            f"--out-jsonl={out_jsonl}",
        ],
    )

    assert result.exit_code == 0, result.output
    prompts = [
        r["params"]["messages"][0]["content"]
        for r in create.call_args.kwargs["requests"]
    ]
    sent = "\n".join(prompts)
    # 3 sendable notes x 2 repeats, plus 1 doc passage.
    assert len(prompts) == 7
    for body in ("kept body", "edited head body", "note key ", "doc key "):
        assert body in sent
    for body in ("superseded", "deleted", "purged", "private", _SECRET):
        assert body not in sent

    assert "calls=7 succeeded=6 non-succeeded=1 truncated=1 empty=1" in result.output
    assert "output_tokens min=10 median=10.0 max=10 total=60" in result.output
    stop_reasons = result.output.split("stop_reasons=")[1]
    assert json.loads(stop_reasons) == {"max_tokens": 1, "end_turn": 5}
    assert len(out_jsonl.read_text().splitlines()) == 7
