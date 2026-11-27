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
  (``{"type": "text", "text": ...}``). The index-vs-direct counts only read
  ``tool_use`` blocks, which are consistently a dict with ``name``/``input``
  keys in every transcript sampled, so that inconsistency doesn't reach them.
  The pointer-follow rate does read an index call's ``tool_result``, in
  either shape (:func:`_result_text`).
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
import math
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
#: ``;``, ``|``/``||``, ``&``/``&&``, and subshell grouping ``(``/``)`` (an
#: unquoted newline is handled before this, by :func:`_logical_lines`).
#: ``_classify`` used to judge a whole line at once (lode-wtk2): a genuine
#: read piped into ``tee`` was excluded wholesale because the write check saw
#: ``tee`` anywhere on the line, and a line merely pairing a docs path with
#: an unrelated read still counted as direct because the docs-path check saw
#: ``docs/*.md`` anywhere on the line. Splitting first and classifying each
#: piece independently collapses both failure modes into one rule.
#:
#: Splitting is done by :mod:`shlex`, not by a regex, because a separator
#: only separates OUTSIDE quotes: a raw-character split cuts
#: ``grep -n "foo|bar" docs/design.md`` in half, leaving two fragments with
#: unbalanced quotes that nothing downstream can tokenize, so a genuine read
#: is dropped. That is worth a third of the direct rows on the calibration
#: corpus, all in the direction that inflates the index share this script
#: exists to measure. Redirects (``<``/``>``) are deliberately NOT separators:
#: :data:`_REDIRECT_TO_DOCS_RE` and the heredoc check need them to stay inside
#: the segment they qualify.
_SEGMENT_PUNCTUATION_CHARS = "();&|"

#: To :func:`_logical_lines` a ``#`` opens a comment only at the start of a
#: word: at the start of a line, or after whitespace or one of these
#: separators. It does not take ``$#``, ``${x#y}`` or ``a#b`` for a comment;
#: shlex in :func:`_split_line_segments` does, to the end of that line.
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


def _split_line_segments(line: str) -> list[str]:
    """Split ONE line on the shell separators listed at
    :data:`_SEGMENT_PUNCTUATION_CHARS`, honouring quoting.

    A line shlex cannot tokenize at all (an unbalanced quote) is returned
    whole, which degrades that one line to the pre-``lode-wtk2`` line-level
    behaviour rather than dropping it. A line here is what
    :func:`_split_segments` hands over, so it holds a newline wherever a
    quoted argument spans one.
    """
    lexer = shlex.shlex(line, posix=False, punctuation_chars=_SEGMENT_PUNCTUATION_CHARS)
    lexer.whitespace_split = True
    segments: list[str] = []
    current: list[str] = []
    try:
        for token in lexer:
            if token and all(char in _SEGMENT_PUNCTUATION_CHARS for char in token):
                if current:
                    segments.append(" ".join(current))
                current = []
            else:
                current.append(token)
    except ValueError:
        return [line]
    if current:
        segments.append(" ".join(current))
    return segments


def _strip_heredoc_bodies(command: str) -> list[str]:
    """The command's lines with every heredoc BODY removed -- the lines
    between a ``<<WORD`` opener and its terminating ``WORD`` line, the
    terminator included. The opener line itself is kept, so
    :func:`_is_write_form` still sees the ``<<`` and disqualifies it.

    Heredoc body text is data, not commands: ``cat >> docs/stack.md <<'EOF'``
    followed by prose mentioning ``grep foo docs/design.md`` would otherwise be
    split into segments and one body line classified as a genuine direct read
    (lode-wtk2). Delimiters opened on one line are consumed in order, and a
    ``<<-`` body may indent its terminator.
    """
    kept: list[str] = []
    pending: list[tuple[bool, str]] = []
    for line in command.splitlines():
        if pending:
            dash, word = pending[0]
            if (line.strip() if dash else line.rstrip()) == word:
                pending.pop(0)
            continue
        kept.append(line)
        pending.extend(
            (bool(m.group(1)), m.group(3)) for m in _HEREDOC_OPEN_RE.finditer(line)
        )
    return kept


def _logical_lines(text: str) -> list[str]:
    """``text`` split at every newline OUTSIDE quotes, so a newline inside a
    multi-line quoted argument stays part of that argument and the closing
    quote is not misread as an opening one (lode-k813a). A backslash-newline
    continuation splits like any other unquoted newline. If a quote is still
    open at the end, where it went wrong is unknown, so every newline splits.

    Each piece is handed to shlex as its own line: shlex drops everything
    from a ``#`` to the end of its line, so a comment or a mid-word ``#``
    such as ``$#`` must not share a line with a later command. A comment's
    own text is skipped here, as a shell skips it: an apostrophe in a comment
    opens no quote.
    """
    lines: list[str] = []
    start = 0
    quote = ""
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and quote != "'" and text[i + 1 : i + 2] != "\n":
            i += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (i == start or text[i - 1] in _COMMENT_PRECEDERS):
            end = text.find("\n", i)
            i = len(text) if end == -1 else end
            continue
        elif char == "\n":
            lines.append(text[start:i])
            start = i + 1
        i += 1
    if quote:
        return text.split("\n")
    lines.append(text[start:])
    return lines


def _split_segments(command: str) -> list[str]:
    """Split a Bash command into independent segments -- heredoc bodies
    stripped first, then on unquoted newlines, then on the in-line shell
    separators -- dropping any segment with no non-whitespace content."""
    segments: list[str] = []
    for line in _logical_lines("\n".join(_strip_heredoc_bodies(command))):
        segments.extend(_split_line_segments(line))
    return [seg for seg in segments if seg.strip()]


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


def _iter_blocks(
    transcript_path: Path, *types: str
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield ``(entry, block)`` pairs for every content block of a transcript
    file whose ``type`` is one of ``types``, in file order. ``entry`` is the
    outer JSON object (carries ``timestamp``/``isSidechain``/``cwd``);
    ``block`` is the inner dict."""
    needles = [f'"{block_type}"' for block_type in types]
    try:
        handle = transcript_path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            # A strict prefilter: any line carrying a block of a type spells
            # that literal. Skipping the rest unparsed is the difference
            # between reading and json.loads-ing multi-MB transcripts whose
            # tool_use turns are a small minority -- so a caller that wants
            # only ``tool_use`` must not ask for ``tool_result`` as well.
            if not any(needle in line for needle in needles):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = entry.get("message") if isinstance(entry, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for item in content:
                if isinstance(item, dict) and item.get("type") in types:
                    yield entry, item


def _iter_tool_uses(
    transcript_path: Path,
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield ``(entry, tool_use_block)`` pairs for every tool_use block in a
    transcript file; the block is a dict with ``name``/``input``."""
    return _iter_blocks(transcript_path, "tool_use")


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


#: How many tool calls after an index call may carry the read that counts as
#: following it (lode-fg7w7.2). Calls are counted in transcript order, any
#: tool, the index's own repeat calls included. One window only, by ruling.
DEFAULT_FOLLOW_WINDOW = 5

#: One cited row of the index's output: ``<path>:<lo>-<hi>``, any marker or
#: title after it ignored. The path is that of the checkout that ran the
#: query, so :func:`_docs_relpath` normalizes it before comparing.
_CITED_RE = re.compile(r"^(\S+\.md):(\d+)-(\d+)", re.MULTILINE)

#: A bd id, as :func:`_is_id_query` looks for one. Any ``lode-`` word of 3-6
#: letters or digits matches, so a query naming ``lode-index`` counts as an
#: id query too; the over-match is accepted.
_BD_ID_RE = re.compile(r"\blode-[a-z0-9]{3,6}\b", re.IGNORECASE)

#: What the index prints when a query matches nothing.
_NO_RESULTS = "No results."

#: Which copy of an index call wins when more than one transcript holds it:
#: the one that shows the most. A copy can stop before the call's result or
#: before the read that follows it.
_STATUS_RANK = {
    "no-result": 0,
    "uncited": 1,
    "zero-hit": 1,
    "not-followed": 2,
    "followed": 3,
}

#: One command of a ``sed`` script: ``Np``, ``A,Bp`` or ``A,$p``.
_SED_RANGE_RE = re.compile(r"^(\d+)(?:,(\d+|\$))?p$")
_AWK_PAIR_RE = re.compile(r"NR\s*==\s*(\d+)\s*,\s*NR\s*==\s*(\d+)")
_AWK_CMP_RE = re.compile(r"NR\s*(>=|>|<=|<|==)\s*(\d+)")

_Range = tuple[int | float, int | float]


def _docs_relpath(path: str) -> str | None:
    """``path`` reduced to its ``docs/<name>.md`` tail, so a worktree path and
    a main-checkout path name the same file; ``None`` for anything else."""
    cut = path.rfind("docs/")
    if cut == -1 or not path.endswith(".md"):
        return None
    return path[cut:]


def _awk_ranges(program: str) -> list[_Range]:
    pair = _AWK_PAIR_RE.search(program)
    if pair:
        return [(int(pair.group(1)), int(pair.group(2)))]
    comparisons = _AWK_CMP_RE.findall(program)
    if not comparisons:
        return []
    lo = 1
    hi: int | float = math.inf
    for op, num in comparisons:
        n = int(num)
        if op in (">=", "=="):
            lo = max(lo, n)
        if op == ">":
            lo = max(lo, n + 1)
        if op in ("<=", "=="):
            hi = min(hi, n)
        if op == "<":
            hi = min(hi, n - 1)
    return [(lo, hi)]


def _head_count(args: list[str]) -> int:
    for i, tok in enumerate(args):
        if tok == "-n" and i + 1 < len(args) and args[i + 1].isdigit():
            return int(args[i + 1])
        if m := re.fullmatch(r"-n?(\d+)", tok):
            return int(m.group(1))
    return 10


def _segment_line_ranges(name: str | None, tokens: list[str]) -> list[_Range]:
    """The line ranges a ``cat``/``sed``/``head``/``tail``/``awk`` segment
    reads, or ``[]`` when its range cannot be read off the command. A ``grep``
    has no range and is never a follow."""
    if name == "cat":
        return [(1, math.inf)]
    if name == "sed":
        ranges: list[_Range] = []
        for tok in tokens:
            for part in tok.split(";"):
                m = _SED_RANGE_RE.match(part.strip())
                if m:
                    end = m.group(2)
                    lo = int(m.group(1))
                    ranges.append((lo, math.inf if end == "$" else int(end or lo)))
        return ranges
    if name == "head":
        return [(1, _head_count(tokens))]
    if name == "tail":
        for tok in tokens:
            if re.fullmatch(r"\+\d+", tok):
                return [(int(tok[1:]), math.inf)]
        return []
    if name == "awk":
        return [r for tok in tokens for r in _awk_ranges(tok)]
    return []


def _tool_reads(tool_name: str, tool_input: dict[str, Any]) -> list[tuple[str, _Range]]:
    """``(docs path, line range)`` for every read of ``docs/*.md`` one tool
    call makes: a ``Read`` by its offset and limit (absent = to the end), a
    ``cat``/``sed``/``head``/``tail``/``awk`` segment by the range it names."""
    if tool_name == "Read":
        rel = _docs_relpath(str(tool_input.get("file_path", "")))
        if not rel:
            return []
        offset = tool_input.get("offset")
        limit = tool_input.get("limit")
        lo = offset if isinstance(offset, int) and offset > 0 else 1
        hi = lo + limit - 1 if isinstance(limit, int) and limit > 0 else math.inf
        return [(rel, (lo, hi))]
    command = str(tool_input.get("command", "")) if tool_name == "Bash" else ""
    if not _mentions_docs_md(command):
        return []
    reads: list[tuple[str, _Range]] = []
    for seg in _split_segments(command):
        if _is_write_form(seg):
            continue
        tokens = _segment_tokens(seg)
        paths = [rel for tok in tokens if (rel := _docs_relpath(tok))]
        if not paths:
            continue
        ranges = _segment_line_ranges(_segment_command_name(seg), tokens)
        reads.extend((p, r) for p in paths for r in ranges)
    return reads


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def _overlaps(cited: tuple[str, int, int], read: tuple[str, _Range]) -> bool:
    path, lo, hi = cited
    rpath, (rlo, rhi) = read
    return path == rpath and lo <= rhi and rlo <= hi


def _is_id_query(command: str) -> bool:
    """Whether an index call's query names a bd id. Only the segments that
    run the index are searched, so an id elsewhere in the command (``bd show
    lode-abc1; <index> "words"``) does not make it an id query. A query that
    arrives through a shell variable (``for q in lode-abc1 ...; do echo "$q";
    <index> "$q"; done``) cannot be read off its segment, so the whole command
    is searched instead."""
    segments = [seg for seg in _split_segments(command) if _is_index_segment(seg)]
    if any("$" in seg for seg in segments):
        return bool(_BD_ID_RE.search(command))
    return any(_BD_ID_RE.search(seg) for seg in segments)


def _follow_status(
    result: str | None, later_calls: list[list[tuple[str, _Range]]]
) -> tuple[str, bool]:
    """``(status, whole_file_only)`` of one index call, from its recorded
    result and the reads of the tool calls in its window."""
    if result is None:
        return "no-result", False
    cited = [
        (rel, int(m.group(2)), int(m.group(3)))
        for m in _CITED_RE.finditer(result)
        if (rel := _docs_relpath(m.group(1)))
    ]
    if not cited:
        return ("zero-hit" if _NO_RESULTS in result else "uncited"), False
    hits = [
        read
        for reads in later_calls
        for read in reads
        if any(_overlaps(c, read) for c in cited)
    ]
    if not hits:
        return "not-followed", False
    return "followed", all(lo <= 1 and hi == math.inf for _, (lo, hi) in hits)


def scan_follow(
    *projects_dirs: Path, window: int = DEFAULT_FOLLOW_WINDOW
) -> list[dict[str, Any]]:
    """One row per docs-index call: ``{idless, status, whole_file_only}``.

    ``status`` is ``"followed"`` when one of the next ``window`` tool calls in
    the same transcript reads a range overlapping a cited ``path:lo-hi`` (cited
    ranges are parsed from the call's recorded result, never re-queried),
    ``"not-followed"`` when none does, ``"zero-hit"`` when the index answered
    ``No results.``, ``"uncited"`` when the result cites nothing for any other
    reason (the command failed, printed ``--help``, or had its output filtered
    away), ``"no-result"`` when the transcript holds no result for the call.
    An answer taken from the snippet alone is, by ruling, not a follow.
    ``whole_file_only`` marks a follow whose every overlapping read takes the
    whole file or an open-ended tail from line 1, which overlaps any range.

    A call held by more than one transcript is one row: :data:`_STATUS_RANK`.
    """
    rows: dict[str, dict[str, Any]] = {}
    transcripts = sorted(p for d in projects_dirs for p in d.rglob("*.jsonl"))
    for transcript_path in transcripts:
        calls: list[list[tuple[str, _Range]]] = []
        index_calls: dict[str, tuple[int, bool]] = {}
        results: dict[str, str] = {}
        seen: set[str] = set()
        for _, block in _iter_blocks(transcript_path, "tool_use", "tool_result"):
            if block["type"] == "tool_result":
                tid = block.get("tool_use_id")
                if isinstance(tid, str) and tid in index_calls:
                    results.setdefault(tid, _result_text(block.get("content")))
                continue
            tid = block.get("id")
            if not isinstance(tid, str) or not tid or tid in seen:
                continue
            seen.add(tid)
            name = block.get("name", "")
            tool_input = block.get("input") or {}
            if _classify(name, tool_input) == "index":
                command = str(tool_input.get("command", ""))
                index_calls[tid] = (len(calls), not _is_id_query(command))
            calls.append(_tool_reads(name, tool_input))
        for tid, (pos, idless) in index_calls.items():
            status, whole_file_only = _follow_status(
                results.get(tid), calls[pos + 1 : pos + 1 + window]
            )
            held = rows.get(tid)
            if held is None or _STATUS_RANK[status] > _STATUS_RANK[held["status"]]:
                rows[tid] = {
                    "idless": idless,
                    "status": status,
                    "whole_file_only": whole_file_only,
                }
    return list(rows.values())


def summarize_follow(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts and follow rates, overall and for id-less queries. The rate's
    denominator is the calls that cited something and have a recorded result."""

    def tally(subset: list[dict[str, Any]]) -> dict[str, Any]:
        counts = Counter(r["status"] for r in subset)
        cited = counts["followed"] + counts["not-followed"]
        return {
            "zero_hit": counts["zero-hit"],
            "uncited": counts["uncited"],
            "no_result": counts["no-result"],
            "cited": cited,
            "followed": counts["followed"],
            "whole_file_only": sum(1 for r in subset if r["whole_file_only"]),
            "rate": counts["followed"] / cited if cited else None,
        }

    return {
        "overall": tally(rows),
        "idless": tally([r for r in rows if r["idless"]]),
    }


def _print_follow(label: str, t: dict[str, Any]) -> None:
    rate = "n/a" if t["rate"] is None else f"{t['rate']:.0%}"
    print(
        f"    {label}: {t['followed']}/{t['cited']} followed = {rate} "
        f"(whole-file-only follows {t['whole_file_only']}, zero-hit {t['zero_hit']}, "
        f"result cites nothing {t['uncited']}, no result recorded {t['no_result']})"
    )


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
    window: Annotated[
        int,
        typer.Option(
            "--window",
            min=1,
            help="Tool calls after an index call that may follow it.",
        ),
    ] = DEFAULT_FOLLOW_WINDOW,
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
    follow = summarize_follow(scan_follow(*roots, window=window))
    print()
    print(f"pointer-follow rate (window: next {window} tool calls):")
    _print_follow("all queries", follow["overall"])
    _print_follow("id-less queries", follow["idless"])


if __name__ == "__main__":
    app()
