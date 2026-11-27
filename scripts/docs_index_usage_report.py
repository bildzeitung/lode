"""Transcript-mining baseline for the docs/ lookup index (``lode-dozi``).

Reproduces the retrospective comparison a 2026-09-13 review did by hand:
index invocations (``scripts/docs_index_query.py``, via the Bash tool) vs
read-style DIRECT access to ``docs/*.md`` (grep/sed/head/cat via Bash, the
Read tool, or the Grep tool), split by main session vs subagent
(``isSidechain`` where populated, else the recording session's ``cwd`` -- see
:func:`_is_subagent`), per day. It reads ``~/.claude/projects/**/*.jsonl``
(Claude Code session transcripts) and writes nothing there -- read-only by
design, same as the log this ticket's other half writes
(``scripts/docs_index_log.py``).

"Direct" means a READ of ``docs/*.md``, which costs three narrowings --
:func:`_segment_command_name`, :func:`_is_write_form` and
:func:`project_scope_dirs`, each documented where it lives. Why each was
needed, and what it was worth on real transcripts: docs/decisions.md's
``lode-dozi`` entries. A Bash command LINE has its heredoc bodies stripped
and is then split into segments (:func:`_split_segments`) before either of
the first two narrowings runs -- ``lode-wtk2``, documented at
:data:`_SEGMENT_PUNCTUATION_CHARS`.

The invocation log records the index's OWN use; it cannot show the
comparison against direct grep/read access, because a producer that greps
``docs/*.md`` by hand never touches the index at all. This script is the
only instrument that can measure that comparison, at the cost of parsing an
externally-defined transcript format this repo does not own.

TRANSCRIPT-ENCODING CAVEATS (hit while writing this):

- A ``tool_result`` (and occasionally ``tool_use.input``) is not reliably
  one shape: content can be a plain string, or a list of content blocks
  (``{"type": "text", "text": ...}``). This script only reads ``tool_use``
  blocks, which are consistently a dict with ``name``/``input`` keys in every
  transcript sampled -- it never depends on ``tool_result`` shape at all, so
  that particular inconsistency doesn't reach this script's own parsing.
- Not every JSON line is a message turn (``type`` values like
  ``"last-prompt"``, ``"mode"``, ``"bridge-session"``, or a hook-attachment
  record carry no ``message`` key at all) -- skipped via a plain ``.get``,
  not an assumed schema.
- A line that fails to parse as JSON (a truncated write, a hand-edited file)
  is skipped, not fatal -- consistent with ``docs_index_log.read_log``'s own
  degrade-not-crash rule.
"""

from __future__ import annotations

import json
import re
import shlex
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import typer

app = typer.Typer(add_completion=False)

#: Bash command names that count as a docs-index invocation vs a
#: direct, read-style access to docs/*.md. Order matters: index check first,
#: since "python scripts/docs_index_query.py ... docs/design.md" (unlikely,
#: but possible in an argument) must not double-count as a direct access too.
_INDEX_SCRIPT_MARKER = "docs_index_query.py"
_DIRECT_BASH_COMMANDS = frozenset({"grep", "sed", "head", "cat", "tail", "awk"})

#: Separators that break one Bash command LINE into independent segments --
#: ``;``, ``|``/``||``, ``&``/``&&``, and subshell grouping ``(``/``)``, plus an
#: unquoted newline (both cut in :func:`_split_segments`).
#: ``_classify`` used to judge a whole line at once (lode-wtk2): a genuine
#: read piped into ``tee`` was excluded wholesale because the write check saw
#: ``tee`` anywhere on the line, and a line merely pairing a docs path with
#: an unrelated read still counted as direct because the docs-path check saw
#: ``docs/*.md`` anywhere on the line. Splitting first and classifying each
#: piece independently collapses both failure modes into one rule.
#:
#: Splitting honours quoting (:func:`_mask_line`), not a regex, because a
#: separator only separates OUTSIDE quotes: a raw-character split cuts
#: ``grep -n "foo|bar" docs/design.md`` in half, leaving two fragments with
#: unbalanced quotes that nothing downstream can tokenize, so a genuine read
#: is dropped. That is worth a third of the direct rows on the calibration
#: corpus, all in the direction that inflates the index share this script
#: exists to measure. Redirects (``<``/``>``) are deliberately NOT separators:
#: :data:`_REDIRECT_TO_DOCS_RE` and the heredoc check need them to stay inside
#: the segment they qualify.
_SEGMENT_PUNCTUATION_CHARS = "();&|"

#: A ``#`` opens a comment (:func:`_mask_line`) only at the start of a word:
#: at the start of a line, or after whitespace or one of these separators. It
#: does not take ``$#``, ``${x#y}`` or ``a#b`` for a comment.
_COMMENT_PRECEDERS = " \t" + _SEGMENT_PUNCTUATION_CHARS

#: Argument-forwarding wrappers a real command name can follow -- same
#: allowlist and same under-count-not-over-count rationale as lode-dozi's
#: original design (a wrapper not on this list is simply missed).
_WRAPPER_COMMANDS = frozenset({"xargs", "sudo", "time", "env", "nohup"})
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_COMMAND_NAME_RE = re.compile(r"^[A-Za-z][\w.-]*$")

#: Forms that WRITE a docs file rather than read it. The criterion is
#: "direct" == a READ of docs/*.md, so each of these disqualifies the whole
#: SEGMENT, not just one token of it. A version-control segment needs no
#: clause of its own: `git` is not in :data:`_DIRECT_BASH_COMMANDS`, so a
#: `git ...` segment is already unclassified by command name (lode-wtk2).
_REDIRECT_TO_DOCS_RE = re.compile(r">>?\s*\S*docs/\S*\.md")
_SED_INPLACE_RE = re.compile(r"\bsed\b[^|;&\n]*(?:-[A-Za-z]*i\b|--in-place)")

#: A heredoc opener: ``<<WORD``, ``<<-WORD``, ``<<'WORD'``, ``<<"WORD"``.
#: ``<<<`` (a herestring, which has no body) is excluded by the lookahead.
_HEREDOC_OPEN_RE = re.compile(r"(?<!<)<<(?!<)(-?)\s*([\'\"]?)(\w+)\2")


_COMMENT = "\0"


def _mask_line(line: str, quote: str) -> tuple[str, str]:
    """``line`` with every character the shell would not treat as syntax
    blanked, plus the quote still open at its end (``quote`` is the one open
    at its start).

    Blanked: the inside of a quoted argument, a backslash-escaped character
    (a backslash escapes outside single quotes only) and, as ``_COMMENT``, a
    comment's text. A ``#`` opens a comment only at the start of a word, so
    ``$#`` and ``a#b`` do not. Quote delimiters are kept; the result has the
    line's length, so an index into it is an index into ``line``.
    """
    out = list(line)
    i = 0
    while i < len(line):
        char = line[i]
        if char == "\\" and quote != "'":
            out[i : i + 2] = " " * len(out[i : i + 2])
            i += 2
            continue
        if quote:
            if char == quote:
                quote = ""
            else:
                out[i] = " "
        elif char in "'\"":
            quote = char
        elif char == "#" and (i == 0 or line[i - 1] in _COMMENT_PRECEDERS):
            out[i:] = _COMMENT * (len(line) - i)
            break
        i += 1
    return "".join(out), quote


def _strip_heredoc_bodies(command: str) -> tuple[list[str], list[str]]:
    """The command's lines with every heredoc BODY removed -- the lines
    between a ``<<WORD`` opener and its terminating ``WORD`` line, the
    terminator included -- each paired with its :func:`_mask_line` mask. The
    opener line itself is kept, so :func:`_is_write_form` still sees the
    ``<<`` and disqualifies it.

    Heredoc body text is data, not commands: ``cat >> docs/stack.md <<'EOF'``
    followed by prose mentioning ``grep foo docs/design.md`` would otherwise be
    split into segments and one body line classified as a genuine direct read
    (lode-wtk2). Delimiters opened on one line are consumed in order, and a
    ``<<-`` body may indent its terminator. An opener inside a quoted
    argument (``python3 -c 'print(1 << 3)'``) opens nothing. A body is not
    scanned for quotes, so its apostrophes open none.

    If a quote is still open at the end, where it went wrong is unknown, so
    the masks are redone one physical line at a time. Each mask ends in the
    line's break: a newline, or a blank when a quote carries across it.
    """
    kept: list[str] = []
    masks: list[str] = []
    pending: list[tuple[bool, str]] = []
    quote = ""
    for line in command.split("\n"):
        if pending:
            dash, word = pending[0]
            if (line.strip() if dash else line.rstrip()) == word:
                pending.pop(0)
            continue
        mask, quote = _mask_line(line, quote)
        kept.append(line)
        masks.append(mask + (" " if quote else "\n"))
        pending.extend(
            (bool(m.group(1)), m.group(3))
            for m in _HEREDOC_OPEN_RE.finditer(line)
            if mask[m.start()] == "<"
        )
    if quote:
        masks = [_mask_line(line, "")[0] + "\n" for line in kept]
    return kept, masks


def _split_segments(command: str) -> list[str]:
    """Split a Bash command into independent segments -- heredoc bodies
    stripped first, then at every newline and in-line shell separator
    (:data:`_SEGMENT_PUNCTUATION_CHARS`) OUTSIDE quotes, with comment text
    dropped and segments with no non-whitespace content discarded.

    One scan decides what is quoted, so the heredoc, newline and separator
    splits cannot disagree about it (lode-d3snk).
    """
    lines, masks = _strip_heredoc_bodies(command)
    text = "\n".join(lines)
    mask = "".join(masks)
    segments: list[str] = []
    start = 0
    for i, char in enumerate(mask):
        if char == "\n" or char in _SEGMENT_PUNCTUATION_CHARS:
            piece = "".join(
                c for c, m in zip(text[start:i], mask[start:i]) if m != _COMMENT
            )
            if piece.strip():
                segments.append(piece.strip())
            start = i + 1
    return segments


def _segment_tokens(segment: str) -> list[str]:
    """The shell words of a single (already-split) segment."""
    try:
        return shlex.split(segment)
    except ValueError:
        # An unbalanced quote (shlex's only failure) leaves the words before
        # it readable, the command name among them. Approximate with
        # whitespace splitting rather than discarding the segment, which
        # would silently under-count the rows it should count (lode-wtk2).
        return segment.split()


def _segment_command_name(segment: str) -> str | None:
    """The one command name actually invoked by a single (already-split)
    segment, or ``None`` if it can't be determined.

    Tokenizes with :mod:`shlex` -- replacing the previous hand-rolled
    command-POSITION regex, which existed only to find a command name
    after a separator; once the line is pre-split into segments, each
    segment holds at most one simple command, and shlex tokenizes it
    correctly (respecting quoting) rather than approximating it with a
    character-class regex. A leading ``VAR=val`` assignment or an
    argument-forwarding wrapper (see :data:`_WRAPPER_COMMANDS`) is skipped
    so the token after it is taken as the command name.
    """
    for tok in _segment_tokens(segment):
        if _ASSIGNMENT_RE.match(tok):
            continue
        base = tok.rsplit("/", 1)[-1]
        if base in _WRAPPER_COMMANDS:
            continue
        if _COMMAND_NAME_RE.match(base):
            return base
        return None
    return None


def _is_write_form(segment: str) -> bool:
    """Whether this SEGMENT writes a docs file rather than reading it: a
    redirect into ``docs/*.md``, a heredoc opener, or an in-place ``sed``.

    A ``tee`` needs no clause of its own, for the same reason a version-control
    verb does not: it is not in :data:`_DIRECT_BASH_COMMANDS`, so its own
    segment is already unclassified by command name.
    """
    return bool(
        _REDIRECT_TO_DOCS_RE.search(segment)
        or "<<" in segment
        or _SED_INPLACE_RE.search(segment)
    )


_PYTHON_RE = re.compile(r"^python[\d.]*$")


def _is_index_segment(segment: str) -> bool:
    """Whether this SEGMENT runs the docs index: its command is the script
    itself, or its command is ``python*`` or ``uv`` and the script is the word
    a ``python*`` word runs (only options may sit between the two).

    A segment that merely names the script is not an invocation: ``sed``,
    ``grep`` or ``bd`` on it, a different file such as
    ``test_docs_index_query.py``, or the script passed as a FILE to what
    ``uv run`` or ``python`` really runs (``uv run ruff format <script>``,
    ``python edit.py <script>``). A form this rule does not know is missed,
    not counted: an interpreter option that takes a value (``python -X
    importtime <script>``), or ``uv run <script>`` with no ``python`` word
    (lode-yt7q2).
    """
    name = _segment_command_name(segment)
    if name == _INDEX_SCRIPT_MARKER:
        return True
    if name is None or not (name == "uv" or _PYTHON_RE.match(name)):
        return False
    runner = ""
    for tok in _segment_tokens(segment):
        base = tok.rsplit("/", 1)[-1]
        if base == _INDEX_SCRIPT_MARKER and _PYTHON_RE.match(runner):
            return True
        if not tok.startswith("-"):
            runner = base
    return False


def _classify_bash_segment(segment: str) -> str | None:
    """Classify one already-split Bash command segment: ``"direct"`` if it
    reads ``docs/*.md``, else ``None``."""
    if not _mentions_docs_md(segment):
        return None
    if _segment_command_name(segment) not in _DIRECT_BASH_COMMANDS:
        return None
    if _is_write_form(segment):
        return None
    return "direct"


def _iter_tool_uses(
    transcript_path: Path,
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield ``(entry, tool_use_block)`` pairs for every tool_use block in a
    transcript file. ``entry`` is the outer JSON object (carries
    ``timestamp``/``isSidechain``/``cwd``); ``tool_use_block`` is the inner
    dict with ``name``/``input``."""
    try:
        handle = transcript_path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            # A strict prefilter: any line carrying a tool_use block spells
            # the literal. Skipping the rest unparsed is the difference
            # between reading and json.loads-ing multi-MB transcripts whose
            # tool_use turns are a small minority.
            if not line or '"tool_use"' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    yield entry, item


def _mentions_docs_md(text: str) -> bool:
    return "docs/" in text and ".md" in text


def _classify(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Return ``"index"``, ``"direct"``, ``"ambiguous"``, or ``None`` (not
    relevant) for one tool_use block.

    ``ambiguous`` exists so an UNNARROWED ``Grep`` -- no path and no glob,
    which defaults to the whole repo and therefore *might* have read docs/ --
    is neither silently dropped nor counted in the headline index-vs-direct
    ratio it would inflate. It is reported on its own line instead.
    """
    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        names_index = _INDEX_SCRIPT_MARKER in command
        # No segment can name the script or a docs path the whole line
        # doesn't, so this is a safe superset test -- and it keeps the segment
        # split, the only expensive step here, off the overwhelming majority
        # of commands.
        if not names_index and not _mentions_docs_md(command):
            return None
        segments = _split_segments(command)
        if names_index and any(_is_index_segment(seg) for seg in segments):
            return "index"
        if any(_classify_bash_segment(seg) == "direct" for seg in segments):
            return "direct"
        return None
    elif tool_name == "Read":
        if _mentions_docs_md(str(tool_input.get("file_path", ""))):
            return "direct"
    elif tool_name == "Grep":
        target = str(tool_input.get("path", "")) + str(tool_input.get("glob", ""))
        if "docs" in target:
            return "direct"
        if not target:
            return "ambiguous"
    return None


def _day(timestamp: str) -> str:
    return timestamp[:10] if timestamp else "unknown"


#: The path segment that marks a producer/reviewer launch worktree. One
#: spelling, used both to tell a subagent's cwd from the main checkout's and
#: to normalize a worktree cwd back to its project root.
_LAUNCH_WORKTREE_MARKER = "/.claude/worktrees/"


def _is_subagent(entry: dict[str, Any]) -> bool:
    """Whether this tool_use came from a subagent rather than the main session.

    ``isSidechain`` is the direct signal, but it is not always populated (on
    Claude Code 2.1.270 it reads ``false`` on every entry of every transcript
    on the machine this was calibrated against, even though subagents
    demonstrably ran). The ``cwd`` fallback is the SAME main-checkout-vs-
    producer proxy this ticket's invocation log uses: a producer or reviewer
    runs in a ``.claude/worktrees/<hash>`` launch worktree, the main session
    does not. See docs/decisions.md's ``lode-dozi`` entry.
    """
    if entry.get("isSidechain"):
        return True
    return _LAUNCH_WORKTREE_MARKER in str(entry.get("cwd", ""))


def encode_project_dir_name(path: Path) -> str:
    """Encode a project path the way Claude Code names its transcript
    directory under ``~/.claude/projects`` -- every non-alphanumeric
    character becomes ``-`` (so ``/home/u/PROJECTS/lode`` becomes
    ``-home-u-PROJECTS-lode``, and a worktree's ``.claude`` becomes
    ``-claude``)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def project_scope_dirs(projects_dir: Path, cwd: Path) -> list[Path]:
    """The transcript directories belonging to the project containing ``cwd``.

    A session started inside ``.claude/worktrees/<name>`` gets its OWN
    transcript directory, named for the worktree path, so the scope is the
    project root's directory plus every worktree directory derived from it.
    ``cwd`` itself is normalized back to the project root first, so running
    this script from a producer's worktree still scopes to the whole project
    rather than to that one worktree.
    """
    root = str(cwd.resolve()).split(_LAUNCH_WORKTREE_MARKER)[0]
    encoded = encode_project_dir_name(Path(root))
    return [
        child
        for child in projects_dir.glob("*")
        if child.is_dir()
        and (child.name == encoded or child.name.startswith(encoded + "--"))
    ]


def scan(*projects_dirs: Path) -> list[dict[str, Any]]:
    """Scan every transcript under each of ``projects_dirs`` (at any depth)
    and return one row per classified tool call: ``{day, session,
    is_subagent, kind}``.

    A tool call is one ``tool_use`` id, counted once across the whole scan
    however many lines carry it. Claude Code records most assistant turns
    twice -- an ``assistant`` line and a timestamp-less, cwd-less
    ``api-request-blob`` line -- and the same subagent transcript can sit
    under more than one session directory, a copy carrying a call only on
    its ``api-request-blob`` line. The copy that carries a timestamp wins,
    and with it the right day and role (lode-yt7q2).
    """
    rows: dict[object, dict[str, Any]] = {}
    transcripts = sorted(p for d in projects_dirs for p in d.rglob("*.jsonl"))
    for transcript_path in transcripts:
        for entry, tool_use in _iter_tool_uses(transcript_path):
            kind = _classify(tool_use.get("name", ""), tool_use.get("input") or {})
            if kind is None:
                continue
            row = {
                "day": _day(str(entry.get("timestamp", ""))),
                "session": transcript_path.stem,
                "is_subagent": _is_subagent(entry),
                "kind": kind,
            }
            tool_id = tool_use.get("id")
            # A block with no id gets a key equal to nothing else, so it is
            # never merged with another.
            key = tool_id if isinstance(tool_id, str) and tool_id else object()
            held = rows.get(key)
            if held is None or (held["day"] == "unknown" and row["day"] != "unknown"):
                rows[key] = row
    return list(rows.values())


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate `scan()` rows into totals, per-day counts, and
    main-vs-subagent counts, for each of the two kinds."""
    totals: Counter = Counter()
    per_day: defaultdict[str, Counter] = defaultdict(Counter)
    per_role: defaultdict[str, Counter] = defaultdict(Counter)
    for r in rows:
        totals[r["kind"]] += 1
        per_day[r["day"]][r["kind"]] += 1
        per_role["subagent" if r["is_subagent"] else "main"][r["kind"]] += 1
    return {
        "totals": dict(totals),
        "per_day": {day: dict(c) for day, c in sorted(per_day.items())},
        "per_role": {role: dict(c) for role, c in per_role.items()},
    }


def _print_split(label: str, split: dict[str, dict[str, int]]) -> None:
    print()
    print(f"{label}:")
    for key, counts in split.items():
        print(
            f"    {key}: index={counts.get('index', 0)} "
            f"direct={counts.get('direct', 0)} "
            f"ambiguous={counts.get('ambiguous', 0)}"
        )


@app.command(
    help=(
        "Mine Claude Code session transcripts for the docs-index-vs-grep "
        "baseline: index invocations vs direct read-style access to "
        "docs/*.md, split by main session vs subagent, per day.\n\nRun this "
        "to reproduce the retrospective comparison the docs-index log alone "
        "cannot show, since a hand grep of docs/*.md never touches the "
        "index. Reads ~/.claude/projects; writes nothing there.\n\nScoped to "
        "the current project's transcripts by default; pass --all-projects "
        "to widen to every project on the machine."
    )
)
def report(
    projects_dir: Annotated[
        Path | None,
        typer.Option(
            "--projects-dir",
            help="Root directory of Claude Code session transcripts.",
        ),
    ] = None,
    all_projects: Annotated[
        bool,
        typer.Option(
            "--all-projects",
            help="Scan every project's transcripts, not just this project's.",
        ),
    ] = False,
) -> None:
    """Print the index-vs-direct-access baseline mined from transcripts."""
    resolved_projects_dir = (
        projects_dir
        if projects_dir is not None
        else Path.home() / ".claude" / "projects"
    )
    roots = (
        [resolved_projects_dir]
        if all_projects
        else project_scope_dirs(resolved_projects_dir, Path.cwd())
    )
    if not roots:
        print(
            "no transcripts for this project under "
            f"{resolved_projects_dir} -- pass --all-projects to widen"
        )
    rows = scan(*roots)
    result = summarize(rows)
    totals = result["totals"]
    index_count = totals.get("index", 0)
    direct_count = totals.get("direct", 0)
    print(f"index invocations: {index_count}")
    print(f"direct docs/*.md access: {direct_count}")
    if index_count + direct_count:
        print(f"index share: {index_count / (index_count + direct_count):.0%}")
    ambiguous_count = totals.get("ambiguous", 0)
    if ambiguous_count:
        print(f"unnarrowed Grep (may or may not have read docs/): {ambiguous_count}")
    _print_split("per day", result["per_day"])
    _print_split("main vs subagent", result["per_role"])


if __name__ == "__main__":
    app()
