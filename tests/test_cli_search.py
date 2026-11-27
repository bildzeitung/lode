"""``lode search`` -- offline BM25 note search (lode-oeb3b)."""

import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from lode.cli import app
from lode.lexical import LexicalCacheBackend
from lode.notes_read import search_notes
from lode.storage import init_db
from lode.versions import save

runner = CliRunner()


def _seed(db_path: Path, note_id: str, body: str) -> None:
    conn = init_db(db_path)
    try:
        result = save(conn, note_id, body)
        LexicalCacheBackend(conn).index(note_id, result.version_id, body)
    finally:
        conn.close()


def test_search_lists_matches_best_first_and_matches_search_notes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lode.db"
    _seed(db_path, "note-weak", "widget mentioned once")
    _seed(db_path, "note-strong", "widget widget widget widget")
    _seed(db_path, "note-other", "grocery list")

    result = runner.invoke(app, ["search", "widget", "--db", str(db_path)])

    assert result.exit_code == 0, result.output
    expected = [row.note_id for row in search_notes(db_path, "widget")]
    assert expected == ["note-strong", "note-weak"]
    out = result.stdout
    assert out.index("note-strong") < out.index("note-weak")
    assert "note-other" not in out


def test_search_no_usable_token_says_no_matching_notes(tmp_path: Path) -> None:
    db_path = tmp_path / "lode.db"
    _seed(db_path, "note-1", "some body")

    result = runner.invoke(app, ["search", "!!!", "--db", str(db_path)])

    assert result.exit_code == 0
    assert result.stdout.strip() == "no matching notes"


def test_search_builds_no_provider_client_or_embedder(tmp_path: Path) -> None:
    """Fresh interpreter: after `lode search`, no LLM SDK or embedding runtime is loaded."""
    db_path = tmp_path / "lode.db"
    _seed(db_path, "note-1", "rotate certificates")
    code = (
        "import sys\n"
        "from typer.testing import CliRunner\n"
        "from lode.cli import app\n"
        f"r = CliRunner().invoke(app, ['search', 'cert', '--db', {str(db_path)!r}])\n"
        "assert r.exit_code == 0, r.output\n"
        "assert 'note-1' in r.stdout\n"
        "bad = [m for m in ('anthropic', 'openai', 'litellm', 'fastembed', 'onnxruntime')"
        " if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
