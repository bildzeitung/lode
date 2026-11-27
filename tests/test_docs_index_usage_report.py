"""Tests for scripts/docs_index_usage_report.py -- the transcript-mining
baseline script (lode-dozi).

Uses SYNTHETIC transcript fixtures under tmp_path -- never a real
~/.claude/projects transcript -- both to keep the test hermetic and because
this script's whole point is to be pointed at a directory via
--projects-dir/`scan(<dir>, ...)`.
"""

import json
from pathlib import Path

import pytest
from conftest import load_module_from_path
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parent.parent

_report_module = load_module_from_path(
    "docs_index_usage_report", REPO_ROOT / "scripts" / "docs_index_usage_report.py"
)
scan = _report_module.scan
summarize = _report_module.summarize
app = _report_module.app

runner = CliRunner()


def _write_transcript(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _tool_use_entry(
    *, timestamp: str, is_subagent: bool, name: str, tool_input: dict, cwd: str = ""
) -> dict:
    return {
        "timestamp": timestamp,
        "isSidechain": is_subagent,
        "cwd": cwd,
        "message": {
            "content": [
                {"type": "tool_use", "name": name, "input": tool_input},
            ]
        },
    }


def test_scan_classifies_index_invocation_via_bash(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00.000Z",
                is_subagent=False,
                name="Bash",
                tool_input={
                    "command": "python scripts/docs_index_query.py 'lode-nt98'"
                },
            )
        ],
    )
    rows = scan(tmp_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "index"
    assert rows[0]["is_subagent"] is False
    assert rows[0]["day"] == "2026-09-13"


def test_scan_classifies_direct_grep_over_docs(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00.000Z",
                is_subagent=True,
                name="Bash",
                tool_input={"command": "grep -rn lode-nt98 docs/decisions.md"},
            )
        ],
    )
    rows = scan(tmp_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "direct"
    assert rows[0]["is_subagent"] is True


def test_scan_classifies_read_tool_on_a_docs_file(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00.000Z",
                is_subagent=False,
                name="Read",
                tool_input={"file_path": "/repo/docs/design.md"},
            )
        ],
    )
    rows = scan(tmp_path)
    assert rows[0]["kind"] == "direct"


def test_scan_ignores_unrelated_bash_commands(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00.000Z",
                is_subagent=False,
                name="Bash",
                tool_input={"command": "git status"},
            )
        ],
    )
    assert scan(tmp_path) == []


def test_scan_ignores_read_tool_on_a_non_docs_file(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00.000Z",
                is_subagent=False,
                name="Read",
                tool_input={"file_path": "/repo/src/lode/retrieval.py"},
            )
        ],
    )
    assert scan(tmp_path) == []


def test_scan_skips_non_message_lines_and_malformed_json(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "s1.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        '{"type": "mode", "mode": "normal"}\n'
        "not json at all\n"
        + json.dumps(
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00.000Z",
                is_subagent=False,
                name="Bash",
                tool_input={"command": "cat docs/design.md"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    rows = scan(tmp_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "direct"


def test_summarize_splits_totals_by_day_and_role(tmp_path: Path) -> None:
    rows = [
        {"day": "2026-09-13", "session": "a", "is_subagent": False, "kind": "index"},
        {"day": "2026-09-13", "session": "a", "is_subagent": True, "kind": "direct"},
        {"day": "2026-09-14", "session": "b", "is_subagent": False, "kind": "direct"},
    ]
    result = summarize(rows)
    assert result["totals"] == {"index": 1, "direct": 2}
    assert result["per_day"]["2026-09-13"] == {"index": 1, "direct": 1}
    assert result["per_day"]["2026-09-14"] == {"direct": 1}
    assert result["per_role"]["main"] == {"index": 1, "direct": 1}
    assert result["per_role"]["subagent"] == {"direct": 1}


def test_report_cli_prints_totals_and_splits(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00.000Z",
                is_subagent=False,
                name="Bash",
                tool_input={
                    "command": "python scripts/docs_index_query.py 'lode-nt98'"
                },
            ),
            _tool_use_entry(
                timestamp="2026-09-13T11:00:00.000Z",
                is_subagent=True,
                name="Bash",
                tool_input={"command": "grep -n lode-nt98 docs/decisions.md"},
            ),
        ],
    )
    result = runner.invoke(app, ["--projects-dir", str(tmp_path), "--all-projects"])
    assert result.exit_code == 0, result.output
    assert "index invocations: 1" in result.output
    assert "direct docs/*.md access: 1" in result.output
    assert "2026-09-13: index=1 direct=1" in result.output
    assert "main: index=1" in result.output
    assert "subagent: " in result.output and "direct=1" in result.output


def test_report_cli_handles_no_transcripts_found(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["--projects-dir", str(tmp_path / "nonexistent"), "--all-projects"]
    )
    assert result.exit_code == 0, result.output
    assert "index invocations: 0" in result.output


def test_scan_treats_a_launch_dir_cwd_as_a_subagent_when_issidechain_is_false(
    tmp_path: Path,
) -> None:
    """isSidechain reads False on every entry on Claude Code 2.1.270 even
    though subagents ran, so the cwd of the recording session is the fallback
    discriminator -- the same main-checkout-vs-producer proxy the invocation
    log uses. Added at technical review (lode-dozi)."""
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00Z",
                is_subagent=False,
                cwd="/repo/.claude/worktrees/agent-deadbeef",
                name="Read",
                tool_input={"file_path": "/repo/docs/design.md"},
            )
        ],
    )
    rows = scan(tmp_path)
    assert [r["is_subagent"] for r in rows] == [True]


def test_scan_buckets_an_unnarrowed_grep_as_ambiguous_not_direct(
    tmp_path: Path,
) -> None:
    """A Grep with neither path nor glob defaults to the whole repo, so it
    only MIGHT have read docs/ -- it must not inflate the headline
    index-vs-direct ratio. A docs-scoped Grep still counts as direct.
    Added at technical review (lode-dozi)."""
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00Z",
                is_subagent=False,
                name="Grep",
                tool_input={"pattern": "anything"},
            ),
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00Z",
                is_subagent=False,
                name="Grep",
                tool_input={"pattern": "anything", "path": "docs"},
            ),
        ],
    )
    assert summarize(scan(tmp_path))["totals"] == {"ambiguous": 1, "direct": 1}


def test_scan_does_not_count_a_command_name_spelled_inside_an_argument(
    tmp_path: Path,
) -> None:
    """(i) Command names match at command positions, not as substrings: a bd
    issue body that happens to contain 'docs/design.md' and the word 'head' is not
    a read of docs/. AMENDED criterion 3 (lode-dozi)."""
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-14T10:00:00Z",
                is_subagent=False,
                name="Bash",
                tool_input={
                    "command": (
                        "bd create --title=x "
                        '--description="see docs/design.md, ahead of the cat '
                        'and the catalogue"'
                    )
                },
            ),
            _tool_use_entry(
                timestamp="2026-09-14T10:00:01Z",
                is_subagent=False,
                name="Bash",
                tool_input={"command": "head -20 docs/design.md"},
            ),
        ],
    )
    assert [r["kind"] for r in scan(tmp_path)] == ["direct"]


@pytest.mark.parametrize(
    "command",
    [
        "cat > docs/stack.md",
        "cat <<'EOF' >> docs/stack.md\nbody\nEOF",
        "sed -i 's/a/b/' docs/design.md",
        "echo hi | tee docs/design.md",
        "git add docs/design.md",
        "git commit -m 'tweak' docs/design.md",
        "git mv docs/stack.md docs/storage.md",
        "git rm docs/externals.md",
        # These pipe a version-control read into a read command: the
        # version-control segment carries the docs path but is not an allowed
        # command name, and the piped segment names no docs file at all.
        "git show HEAD:docs/design.md | grep -n foo",
        "git diff -- docs/design.md | head -30",
        "git log --oneline docs/design.md | tail -5",
        "git show HEAD:docs/design.md | wc -l",
    ],
)
def test_scan_excludes_a_write_or_version_control_form(
    tmp_path: Path, command: str
) -> None:
    """(ii) Writes and version-control operations are not reads of docs/.
    AMENDED criterion 3 (lode-dozi)."""
    _write_transcript(
        tmp_path / "proj" / "s1.jsonl",
        [
            _tool_use_entry(
                timestamp="2026-09-14T10:00:00Z",
                is_subagent=False,
                name="Bash",
                tool_input={"command": command},
            )
        ],
    )
    assert scan(tmp_path) == []


@pytest.mark.parametrize(
    "command",
    [
        "git checkout trunk -- docs/design.md && cat docs/design.md",
        "sed -n '1,5p' docs/design.md; git diff --stat",
    ],
)
def test_scan_counts_a_read_sharing_a_line_with_a_version_control_command(
    tmp_path: Path, command: str
) -> None:
    """A genuine read counts even when a version-control command shares its
    line: under per-segment classification the read is a segment of its own,
    and the whole-line version-control exclusion these used to fall under was
    dead code once segments carried the verdict (lode-wtk2)."""
    _write_transcript(
        tmp_path / "proj" / "s1.jsonl",
        [
            _tool_use_entry(
                timestamp="2026-09-14T10:00:00Z",
                is_subagent=False,
                name="Bash",
                tool_input={"command": command},
            )
        ],
    )
    assert [r["kind"] for r in scan(tmp_path)] == ["direct"]


def test_scan_classifies_a_read_piped_into_tee_as_direct(tmp_path: Path) -> None:
    """(iii) A genuine read of docs/*.md piped into `tee` (to also save the
    output) is a direct read -- the write-form check applies to `tee`'s OWN
    segment, not to the earlier segment that actually reads the file
    (lode-wtk2)."""
    _write_transcript(
        tmp_path / "proj" / "s1.jsonl",
        [
            _tool_use_entry(
                timestamp="2026-09-14T10:00:00Z",
                is_subagent=False,
                name="Bash",
                tool_input={"command": "cat docs/design.md | tee /tmp/out.txt"},
            )
        ],
    )
    assert [r["kind"] for r in scan(tmp_path)] == ["direct"]


def test_scan_does_not_count_a_docs_path_paired_with_an_unrelated_read(
    tmp_path: Path,
) -> None:
    """(iv) A line that merely MENTIONS a docs path in one segment and reads
    something unrelated in another segment is not a read of docs/*.md -- each
    segment is classified independently (lode-wtk2)."""
    _write_transcript(
        tmp_path / "proj" / "s1.jsonl",
        [
            _tool_use_entry(
                timestamp="2026-09-14T10:00:00Z",
                is_subagent=False,
                name="Bash",
                tool_input={"command": "echo see docs/design.md && cat /etc/hostname"},
            )
        ],
    )
    assert scan(tmp_path) == []


def test_scan_does_not_split_inside_quotes(tmp_path: Path) -> None:
    """(v) A separator character INSIDE a quoted argument is not a segment
    boundary -- `grep -n "foo|bar" docs/design.md` is one read, not two
    unparseable halves. Splitting on the raw characters dropped a third of
    the genuine direct reads on the calibration corpus (lode-wtk2)."""
    _write_transcript(
        tmp_path / "proj" / "s1.jsonl",
        [
            _tool_use_entry(
                timestamp="2026-09-14T10:00:00Z",
                is_subagent=False,
                name="Bash",
                tool_input={"command": 'grep -n "foo|bar; baz" docs/design.md'},
            )
        ],
    )
    assert [r["kind"] for r in scan(tmp_path)] == ["direct"]


def test_scan_keeps_a_quoted_command_substitution_intact(tmp_path: Path) -> None:
    """(vi) A quoted ``$(...)`` keeps the docs path attached to the command
    that reads it, instead of the parentheses cutting the path into its own
    nameless segment (lode-wtk2)."""
    _write_transcript(
        tmp_path / "proj" / "s1.jsonl",
        [
            _tool_use_entry(
                timestamp="2026-09-14T10:00:00Z",
                is_subagent=False,
                name="Bash",
                tool_input={"command": 'head -50 "$(pwd)/docs/design.md"'},
            )
        ],
    )
    assert [r["kind"] for r in scan(tmp_path)] == ["direct"]


def test_scan_survives_an_unbalanced_quote(tmp_path: Path) -> None:
    """(vii) A line shlex cannot tokenize is classified whole rather than
    dropped or crashing the scan (lode-wtk2)."""
    _write_transcript(
        tmp_path / "proj" / "s1.jsonl",
        [
            _tool_use_entry(
                timestamp="2026-09-14T10:00:00Z",
                is_subagent=False,
                name="Bash",
                tool_input={"command": 'cat docs/design.md "unclosed'},
            )
        ],
    )
    assert [r["kind"] for r in scan(tmp_path)] == ["direct"]


def test_report_scopes_to_the_current_project_unless_widened(
    tmp_path: Path, monkeypatch
) -> None:
    """(iii) Default scope is the current project's transcript directory,
    derived from cwd the way Claude Code encodes it, and it still resolves to
    the whole project when run from a worktree; --all-projects widens.
    AMENDED criterion 3 (lode-dozi)."""
    encode = _report_module.encode_project_dir_name
    project_root = tmp_path / "PROJECTS" / "lode"
    worktree = project_root / ".claude" / "worktrees" / "agent-dead"
    worktree.mkdir(parents=True)
    projects_dir = tmp_path / "projects"
    for directory, command in (
        (projects_dir / encode(project_root), "grep -n foo docs/design.md"),
        (projects_dir / encode(worktree), "grep -n foo docs/storage.md"),
        (
            projects_dir / encode(tmp_path / "PROJECTS" / "elsewhere"),
            "grep -n foo docs/retrieval.md",
        ),
    ):
        _write_transcript(
            directory / "s.jsonl",
            [
                _tool_use_entry(
                    timestamp="2026-09-14T10:00:00Z",
                    is_subagent=False,
                    name="Bash",
                    tool_input={"command": command},
                )
            ],
        )

    monkeypatch.chdir(worktree)
    scoped = runner.invoke(app, ["--projects-dir", str(projects_dir)])
    assert scoped.exit_code == 0, scoped.output
    assert "direct docs/*.md access: 2" in scoped.output

    widened = runner.invoke(
        app, ["--projects-dir", str(projects_dir), "--all-projects"]
    )
    assert widened.exit_code == 0, widened.output
    assert "direct docs/*.md access: 3" in widened.output


@pytest.mark.parametrize(
    ("opener", "terminator"),
    [
        ("<<EOF", "EOF"),
        ("<<'EOF'", "EOF"),
        ('<<"EOF"', "EOF"),
        ("<<-EOF", "\tEOF"),
    ],
)
def test_scan_ignores_docs_paths_inside_a_heredoc_body(
    tmp_path: Path, opener: str, terminator: str
) -> None:
    """A heredoc BODY is data, not commands: prose inside one that mentions a
    read of docs/*.md must not classify as direct (lode-wtk2)."""
    transcript = tmp_path / "proj" / "s1.jsonl"
    command = "\n".join(
        [
            f"cat >> docs/stack.md {opener}",
            "Earlier we ran grep foo docs/design.md to find it.",
            terminator,
        ]
    )
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-14T10:00:00.000Z",
                is_subagent=False,
                name="Bash",
                tool_input={"command": command},
            )
        ],
    )
    assert scan(tmp_path) == []


def test_scan_still_counts_a_read_after_a_heredoc_terminator(tmp_path: Path) -> None:
    """Stripping stops at the terminator -- a real read on a later line is still
    classified (lode-wtk2)."""
    transcript = tmp_path / "proj" / "s1.jsonl"
    command = (
        "cat >> docs/stack.md <<'EOF'\n"
        "grep foo docs/design.md\n"
        "EOF\n"
        "grep -n bar docs/decisions.md"
    )
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-14T10:00:00.000Z",
                is_subagent=False,
                name="Bash",
                tool_input={"command": command},
            )
        ],
    )
    rows = scan(tmp_path)
    assert [r["kind"] for r in rows] == ["direct"]


@pytest.mark.parametrize(
    "layout",
    [
        {"proj/s1.jsonl": ["blob", "assistant"]},
        {"proj/s1.jsonl": ["assistant", "blob"]},
        # The same subagent transcript under two session directories, one copy
        # carrying the call only on its api-request-blob line.
        {
            "proj/s1/subagents/agent-a.jsonl": ["blob"],
            "proj/s2/subagents/agent-a.jsonl": ["blob", "assistant"],
        },
        {
            "proj/s1/subagents/agent-a.jsonl": ["assistant", "blob"],
            "proj/s2/subagents/agent-a.jsonl": ["blob"],
        },
        {"proj/s1.jsonl": ["blob"], "proj--worktree/s1.jsonl": ["assistant"]},
    ],
    ids=[
        "blob-first",
        "assistant-first",
        "session-copy-blob-only-first",
        "session-copy-blob-only-last",
        "two-scanned-directories",
    ],
)
def test_scan_counts_a_tool_use_id_once_with_the_assistant_lines_day_and_role(
    tmp_path: Path, layout: dict[str, list[str]]
) -> None:
    """Each top-level directory of ``layout`` is scanned as its own root, the
    way ``report`` passes one per transcript directory in scope."""
    block = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "Read",
        "input": {"file_path": "/repo/docs/design.md"},
    }
    lines = {
        "blob": {"type": "api-request-blob", "message": {"content": [block]}},
        "assistant": {
            "type": "assistant",
            "timestamp": "2026-11-20T10:00:00.000Z",
            "isSidechain": False,
            "cwd": "/repo/.claude/worktrees/agent-x",
            "message": {"content": [block]},
        },
    }
    for name, kinds in layout.items():
        _write_transcript(tmp_path / name, [lines[k] for k in kinds])
    rows = scan(*sorted(tmp_path.iterdir()))
    assert len(rows) == 1
    assert rows[0]["day"] == "2026-11-20"
    assert rows[0]["is_subagent"] is True


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("sed -n 1,5p scripts/docs_index_query.py", False),
        ("pytest tests/test_docs_index_query.py", False),
        ("bd update lode-x --append-notes 'see scripts/docs_index_query.py'", False),
        # The script as a FILE handed to what uv or python really runs.
        ("uv run --frozen ruff format scripts/docs_index_query.py", False),
        ("uv run --frozen python /tmp/edit.py scripts/docs_index_query.py", False),
        ("python -m py_compile scripts/docs_index_query.py", False),
        ('uv run --frozen python scripts/docs_index_query.py "x"', True),
        ('.venv/bin/python scripts/docs_index_query.py "x"', True),
        ('cd /r && python3 scripts/docs_index_query.py "x"', True),
        ('scripts/docs_index_query.py "x"', True),
    ],
)
def test_scan_index_marker_needs_an_invocation_not_a_mention(
    tmp_path: Path, command: str, expected: bool
) -> None:
    _write_transcript(
        tmp_path / "proj" / "s1.jsonl",
        [
            _tool_use_entry(
                timestamp="2026-11-20T10:00:00.000Z",
                is_subagent=False,
                name="Bash",
                tool_input={"command": command},
            )
        ],
    )
    assert [r["kind"] for r in scan(tmp_path)] == (["index"] if expected else [])


_MULTILINE_QUOTE = 'python3 -c "\nprint(1)\n";'


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            _MULTILINE_QUOTE
            + ' uv run --frozen python scripts/docs_index_query.py "x"',
            "index",
        ),
        (_MULTILINE_QUOTE + " grep -n lode-x docs/decisions.md", "direct"),
        ('python3 -c "\ngrep x docs/design.md\n"', None),
        ('python3 -c "\nuv run python scripts/docs_index_query.py x\n"', None),
    ],
)
def test_classify_honours_quotes_across_newlines(
    command: str, expected: str | None
) -> None:
    assert _report_module._classify("Bash", {"command": command}) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            '# look it up\nuv run --frozen python scripts/docs_index_query.py "x"',
            "index",
        ),
        ("ls -la  # list\ngrep -n foo docs/design.md", "direct"),
        ("echo $#\ngrep -n foo docs/design.md", "direct"),
        ("# don't\ncd x\ngrep y docs/design.md\n# it's fine", "direct"),
        ('echo "unclosed\ngrep -n foo docs/design.md', "direct"),
        ("# grep -n foo docs/design.md\nls", None),
    ],
)
def test_classify_keeps_the_lines_after_a_hash_or_an_open_quote(
    command: str, expected: str | None
) -> None:
    """A ``#`` -- a comment, or mid-word as in ``$#`` -- costs at most its
    own line, an apostrophe in a comment opens no quote, and a quote never
    closed falls back to one physical line at a time."""
    assert _report_module._classify("Bash", {"command": command}) == expected


def _use(tid: str, name: str, tool_input: dict) -> dict:
    return {
        "message": {
            "content": [
                {"type": "tool_use", "id": tid, "name": name, "input": tool_input}
            ]
        }
    }


def _result(tid: str, text: str) -> dict:
    return {
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]
        }
    }


def _index_call(tid: str, query: str, result: str) -> list[dict]:
    command = f'uv run --frozen python scripts/docs_index_query.py "{query}"'
    return [_use(tid, "Bash", {"command": command}), _result(tid, result)]


_CITES = (
    "/wt/a/docs/stack.md:100-120  Title\n    snippet\n"
    "/wt/a/docs/design.md:5-9  Other\n    snippet\n"
)


def _follow_rows(tmp_path: Path, entries: list[dict], **kwargs: int) -> list[dict]:
    _write_transcript(tmp_path / "proj" / "s.jsonl", entries)
    return _report_module.scan_follow(tmp_path, **kwargs)


@pytest.mark.parametrize(
    ("follow_call", "status"),
    [
        (
            (
                "Read",
                {"file_path": "/other/wt/docs/stack.md", "offset": 110, "limit": 5},
            ),
            "followed",
        ),
        (
            ("Read", {"file_path": "/x/docs/stack.md", "offset": 121, "limit": 5}),
            "not-followed",
        ),
        (("Bash", {"command": "sed -n 90,100p docs/stack.md"}), "followed"),
        (("Bash", {"command": "sed -n 101,130p docs/stack.md"}), "followed"),
        (("Bash", {"command": "sed -n 121,130p docs/stack.md"}), "not-followed"),
        (("Bash", {"command": "sed -n '10,20p;118,119p' docs/stack.md"}), "followed"),
        (
            ("Bash", {"command": "sed -n '10,20p;121,130p' docs/stack.md"}),
            "not-followed",
        ),
        (("Bash", {"command": "sed -n 110p docs/stack.md"}), "followed"),
        (("Bash", {"command": "sed -i 100,120p docs/stack.md"}), "not-followed"),
        (("Bash", {"command": "cat docs/stack.md"}), "followed"),
        (("Bash", {"command": "head docs/stack.md"}), "not-followed"),
        (("Bash", {"command": "head -n 50 docs/stack.md"}), "not-followed"),
        (("Bash", {"command": "head -n 105 docs/stack.md"}), "followed"),
        (("Bash", {"command": "head -n105 docs/stack.md"}), "followed"),
        (("Bash", {"command": "head -105 docs/stack.md"}), "followed"),
        (("Bash", {"command": "awk 'NR==100,NR==120' docs/stack.md"}), "followed"),
        (("Bash", {"command": "tail -n 20 docs/stack.md"}), "not-followed"),
        (("Bash", {"command": "awk 'NR>=118 && NR<=140' docs/stack.md"}), "followed"),
        (
            ("Bash", {"command": "awk 'NR>=121 && NR<=140' docs/stack.md"}),
            "not-followed",
        ),
        (("Bash", {"command": "tail -n +119 docs/stack.md"}), "followed"),
        (("Bash", {"command": "grep -n foo docs/stack.md"}), "not-followed"),
        (("Bash", {"command": "sed -n 100,120p docs/storage.md"}), "not-followed"),
    ],
)
def test_follow_requires_a_read_range_overlapping_a_cited_range(
    tmp_path: Path, follow_call: tuple[str, dict], status: str
) -> None:
    entries = _index_call("i1", "retrieval pipeline", _CITES)
    entries.append(_use("c1", *follow_call))
    assert _follow_rows(tmp_path, entries)[0]["status"] == status


def test_follow_window_and_row_kinds(tmp_path: Path) -> None:
    def ls(prefix: str, count: int) -> list[dict]:
        return [_use(f"{prefix}{n}", "Bash", {"command": "ls"}) for n in range(count)]

    sed = {"command": "sed -n 100,110p docs/stack.md"}
    entries = [
        *_index_call("i1", "inside window", _CITES),
        *ls("f", 3),
        _use("r1", "Bash", sed),
        *_index_call("i2", "outside window", _CITES),
        *ls("g", 4),
        _use("r2", "Bash", sed),
        *_index_call("i3", "nothing", "No results."),
        _use(
            "i4",
            "Bash",
            {"command": 'python scripts/docs_index_query.py "lode-abc1"'},
        ),
        *_index_call("i5", "lode-t6o1 history", _CITES),
        _use("r3", "Read", {"file_path": "docs/stack.md"}),
    ]
    rows = _follow_rows(tmp_path, entries, window=4)
    assert [r["status"] for r in rows] == [
        "followed",
        "not-followed",
        "zero-hit",
        "no-result",
        "followed",
    ]
    assert [r["idless"] for r in rows] == [True, True, True, False, False]
    assert rows[4]["whole_file_only"] is True
    assert rows[0]["whole_file_only"] is False

    summary = _report_module.summarize_follow(rows)
    assert summary["overall"]["cited"] == 3
    assert summary["overall"]["followed"] == 2
    assert summary["idless"]["cited"] == 2
    assert summary["idless"]["followed"] == 1
    assert summary["idless"]["rate"] == 0.5
    assert summary["idless"]["zero_hit"] == 1


def test_follow_reads_a_result_recorded_as_a_list_of_text_blocks(
    tmp_path: Path,
) -> None:
    entries = _index_call("i1", "x y", "")
    entries[1]["message"]["content"][0]["content"] = [{"type": "text", "text": _CITES}]
    entries.append(_use("c1", "Bash", {"command": "sed -n 100,110p docs/stack.md"}))
    assert [r["status"] for r in _follow_rows(tmp_path, entries)] == ["followed"]


@pytest.mark.parametrize("cut", [1, 2, 3])
def test_follow_takes_the_copy_of_a_call_that_shows_the_most(
    tmp_path: Path, cut: int
) -> None:
    """A call held by two transcripts is one row. The copy that sorts first
    may stop before the result (1) or before the read (2), or be whole (3),
    and must not fix the row."""
    entries = _index_call("i1", "dup", _CITES)
    entries.append(_use("c1", "Bash", {"command": "sed -n 100,110p docs/stack.md"}))
    _write_transcript(tmp_path / "a" / "s.jsonl", entries[:cut])
    _write_transcript(tmp_path / "b" / "s.jsonl", entries)
    assert [r["status"] for r in _report_module.scan_follow(tmp_path)] == ["followed"]


_LOOP_BODY = 'do echo "= $q"; python scripts/docs_index_query.py "$q"; done'


@pytest.mark.parametrize(
    ("command", "idless"),
    [
        ('uv run --frozen python scripts/docs_index_query.py "lode-abc1 words"', False),
        (
            'bd show lode-abc1; uv run python scripts/docs_index_query.py "words" | head',
            True,
        ),
        (
            'python scripts/docs_index_query.py "words"; grep -n lode-abc1 docs/stack.md',
            True,
        ),
        (f'for q in lode-abc1 "two words"; {_LOOP_BODY}', False),
        (f"for q in one two; {_LOOP_BODY}", True),
    ],
)
def test_id_query_is_judged_on_the_segment_that_runs_the_index(
    tmp_path: Path, command: str, idless: bool
) -> None:
    entries = [_use("i1", "Bash", {"command": command}), _result("i1", _CITES)]
    assert _follow_rows(tmp_path, entries)[0]["idless"] is idless


@pytest.mark.parametrize(
    ("result", "status"),
    [
        ("No results.", "zero-hit"),
        ("Exit code 127\n/bin/bash: ./venv/bin/python: No such file", "uncited"),
        (" Usage: docs_index_query.py [OPTIONS] {text}", "uncited"),
    ],
)
def test_follow_zero_hit_is_only_the_index_saying_no_results(
    tmp_path: Path, result: str, status: str
) -> None:
    rows = _follow_rows(tmp_path, _index_call("i1", "x y", result))
    assert rows[0]["status"] == status
    summary = _report_module.summarize_follow(rows)["overall"]
    assert (summary["zero_hit"], summary["uncited"]) == (
        int(status == "zero-hit"),
        int(status == "uncited"),
    )
    assert summary["cited"] == 0


def test_report_cli_prints_the_follow_rate_with_its_window(tmp_path: Path) -> None:
    entries = _index_call("i1", "x y", _CITES)
    entries.append(_use("c1", "Read", {"file_path": "docs/stack.md", "offset": 100}))
    _write_transcript(tmp_path / "proj" / "s.jsonl", entries)
    result = runner.invoke(
        app,
        ["--projects-dir", str(tmp_path), "--all-projects", "--window", "3"],
    )
    assert result.exit_code == 0, result.output
    assert "window: next 3 tool calls" in result.output
    assert "id-less queries: 1/1 followed = 100%" in result.output
