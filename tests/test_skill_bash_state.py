"""Gate: no fenced ```bash/```sh block in a `.claude/skills/*/SKILL.md` (lode-x495) or
a `.claude/agents/*.md` (lode-lv04) may reference a shell variable that isn't ALSO
assigned somewhere within that SAME block.

## The bug class

`lode-sfnb` fixed one instance of this in `.claude/skills/land/SKILL.md`: Section 3a
populated a `declare -A MSG` associative array that Section 3's merge loop read back two
fenced blocks later. An agent executing a skill runs each fenced block as its own,
separate Bash tool invocation -- shell state (variables, arrays, functions, traps,
`set -e`/`set -o pipefail`) never survives between them, which is a harness-level fact,
not a style preference (see the governing rule land/SKILL.md states at its own top,
and `docs/agents-workflow.md`'s sibling section this test enforces). `$MSG` silently
expanded to empty, and `git merge -m ''` failed with completely empty stdout AND
stderr. That fix also established the two sanctioned remedies: **re-derive** the value
fresh in every block that needs it (cheap, deterministic), or **persist it to a file**
that every later block reads back and asserts loaded.

## Why per-block, not file-global

The obvious-looking alternative reading -- "flag a variable only if NO block in the
whole file assigns it" -- is wrong, and demonstrably so: `$MSG` (the original bug) WAS
assigned somewhere in the file (Section 3a's own block), just not in the block that
used it. The correct check has to be **per use-site**: for every `$VAR`/`${VAR...}`
reference inside a block, is `VAR` ALSO assigned somewhere in that SAME block? If not,
running that block in isolation (the only way it is ever actually run) sees an unbound
variable. This also correctly does NOT flag a variable that is assigned earlier in the
SAME block as a later use within it (e.g. a `while read` loop's own loop variables) --
that is completely ordinary same-invocation shell state, not the bug.

## Precision -- what counts as an assignment

Verified against every real fenced block in `.claude/skills/*/SKILL.md` while writing
this gate (see git history / the lode-x495 hand-off for the full audit trail). Treated
as an assignment of `VAR`, anywhere in a block, at or after a statement boundary
(start of line; after `;`, `&`, `|`, `(`; after `if`/`while`/`until`/`then`/`do`/
`else`/`elif`, optionally negated with `!`):

- `VAR=value`, `VAR+=value` (never `==`/`!=`/`<=`/`>=` comparisons, which this
  regex does not match at all since it requires a bare `=` not doubled or paired
  with a comparison operator)
- `export VAR=`, `local VAR=`, `readonly VAR=`, `declare [-flags] VAR=`
- `declare -A VAR` / `declare -a VAR` (a bare declaration, no `=`)
- `read [-flags] VAR1 VAR2 ...` -- every whitespace-separated name is assigned,
  including the common `while IFS=$'\\t' read -r e TITLE; do` shape, where the
  trailing `; do` on the same physical line must NOT swallow `TITLE` as part of
  its own token
- `mapfile`/`readarray [-flags] VAR`
- `for VAR in ...` and C-style `for ((VAR=...`

A **comment is never scanned** for either an assignment or a use -- `#` outside any
quote, at the start of the line or preceded by whitespace (so `${VAR#pattern}`'s bare
`#` parameter-expansion operator, which has no preceding whitespace, is correctly left
alone). This matters: this codebase's skills carry heavy inline prose commentary that
routinely *quotes* a variable name while explaining history or a rejected design
(`# ...a hand-restated $LANDED is now structural` is prose about a fix, not code that
runs), and scanning comment text produced false positives during development of this
gate against the real files.

## Precision -- what counts as a use, and what is deliberately excluded

`$VAR` and `${VAR...}` (any `${VAR` prefix -- covers `${VAR:-default}`,
`${VAR#pattern}`, `${VAR/a/b}`, `${VAR[@]}`, `${#VAR}` is intentionally NOT matched
since `#VAR` inside `${#VAR}` is a length operator on `VAR`, not a second identifier;
the outer `VAR` is still caught by `_USE_BRACED`). Command substitution `$(...)` and
arithmetic `$((...))` are not treated as a use of a variable named `(` -- the regex
requires an identifier character immediately after `$`/`${`. Bash's own positional and
special parameters (`$0`-`$9`, `$@`, `$*`, `$#`, `$?`, `$!`, `$$`, `$-`, `$_`) are
excluded outright -- they are never "assigned" in the sense this gate checks. A short,
explicit list of environment variables this repo's skills legitimately expect to be set
by the calling shell/operator, never by the skill's own bash, is also excluded (see
`_KNOWN_ENV_VARS` below) -- each with its own one-line justification, the same bar as
the allowlist.

## The two false positives this precision was built to avoid

A cruder prototype run during `lode-sfnb`'s review flagged `TITLE` and `ROW` in
`/sweep` (`.claude/skills/sweep/SKILL.md` Section 2's `while IFS=$'\\t' read -r e
TITLE; do ... ROW=$(printf ...) ...`) -- both are legitimately assigned and used
within that SAME block, but a naive `^VAR=`-only scan (no `read` support, no
indentation tolerance) missed both. **This gate's own test suite pins both as
non-findings** (`test_read_assigns_every_named_variable`,
`test_indented_assignment_still_counts`) precisely because a parser that
over-flags real, correct code is worse than one that under-flags: an
over-flagging gate gets its findings suppressed or its allowlist bloated with
non-bugs, which is how this exact rot restarts (per the ticket that added this
gate, lode-x495).

## Scope: EVERY skill file AND every agent file, with a small, per-variable allowlist

lode-x495 explicitly permitted either scoping this gate to `land/SKILL.md` only and
widening later, or shipping repo-wide with an allowlist. This gate ships **repo-wide**
(`.claude/skills/*/SKILL.md`) with an allowlist, because the bug class is not
land-local (per the ticket's own title) and `/sweep` and `/release` already carried
real, confirmed instances that a land-only gate would leave silently uncovered.

`lode-lv04` widened the same gate to `.claude/agents/*.md` -- markdown instruction
files whose fenced bash an agent executes exactly the same way, block by block, under
the same harness rule; the bug class is not skills-specific either. At the time of
widening the shipped parser reported ZERO violations across all three agent files
(`coding.md` 31 fenced bash blocks, `code-reviewer.md` 11, `land-review.md` 3), so
widening needed no allowlist entry and no fix to any agent file, only a change to
which paths are globbed. That the widened gate actually *catches* an agent-file
regression is pinned permanently rather than checked once by hand, by
`test_sabotaged_agent_file_is_caught_by_find_violations`. Both roots share **one**
`ALLOWLIST`, keyed by a path relative to `.claude/` (not to either root) precisely so
a key can never be ambiguous about which side of the tree it names -- e.g.
`skills/land/SKILL.md` vs. a hypothetical `agents/land.md`.

**There is deliberately no whole-file escape hatch** -- not even for `land/SKILL.md`,
which was initially skipped file-wide. The reasoning for that decision and against it
lives in `docs/agents-workflow.md`'s section above ("There is no whole-file escape
hatch, deliberately"); `test_every_skill_and_agent_file_is_covered` pins the outcome.
Measured while making the call, and recorded here because it is evidence rather than
rationale: across `trunk`, this branch, and all five in-flight sibling branches
touching the file, the parser reports exactly `$ACCEPTED` and `$CONFLICTS` and nothing
else -- no false positive, and no sibling introduces a new instance.

That file already went through one thorough remediation (`lode-sfnb`: `$MSG` converted
to a per-id file under `$MSG_DIR`, `$LANDED` built up incrementally -- each successful
merge appends to `$STATE_DIR/landed` in the SAME block that merges it -- and read back
with an assert-on-load by a later block). The one small, purely-notational fix this
ticket makes in it (`$id`/`$B` -> `<id>`/`<B>` in the Section 3a "HELD" note template,
matching the file's own established `<...>` convention already used two sections later)
needs no allowlist entry because it stops being a `$`-reference at all.

`challenge/SKILL.md` carries no fenced ```bash/```sh blocks at all. `code/SKILL.md`
carries **nine** and `epic-audit/SKILL.md` six, all clean. Not one of `code/SKILL.md`'s
nine fences puts its backticks at column 0: eight are indented under a list item, four
open inside a markdown blockquote, and three are BOTH -- the per-fence breakdown is
recorded once, next to the parser, in `tests/conftest.py::bash_fence_blocks`. A scanner
anchored at column 0 -- `line.startswith("```")`, the shape `tests/test_land_lock.py`
used before lode-ovgs fixed it -- therefore sees zero blocks in that file and would
report it as carrying no bash at all. `_bash_blocks` (imported from
`tests/conftest.py::bash_fence_blocks`, lode-ovgs unified what were three near-identical
private copies into one shared helper) normalizes each line before testing, so it sees
all nine -- this docstring previously recorded the column-0 answer (zero) as fact, then
(after lode-ovgs, before lode-wroz) a stale count of five.

The blockquote half of that was a SECOND blind spot, independent of the indentation one
and closed later (lode-wroz): a bare `>` survives `.strip()`, so
`stripped.startswith("```")` never matched a `> ```bash` fence even once the indentation
fix had landed -- and the marker has to come off the CONTENT lines too, or a same-block
assign-then-use pair still reads as unassigned (mechanism stated once, at
`_BLOCKQUOTE_MARKER`; pinned by `test_blockquoted_fence_content_lines_are_also_unmarked`
below). Filed as a sibling of lode-ovgs rather than folded into it: lode-ovgs's scope was
the indented-fence variant and the three-copies-to-one unification, and the blockquote
fix was measured free -- the six SKILL.md files go 51 -> 55 blocks and this gate's full
corpus, which also takes in `.claude/agents/*.md`, goes 96 -> 100, with zero new
violations either way -- only once that unification had already landed. Fixing it in
`tests/conftest.py`'s shared helper, once, rather than adding a fourth private copy of
the same workaround `tests/test_bd_list_limit_gate.py` already carries on its own input.
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import bash_fence_blocks as _bash_blocks

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_DIR = REPO_ROOT / ".claude"
SKILLS_DIR = CLAUDE_DIR / "skills"
AGENTS_DIR = CLAUDE_DIR / "agents"

# Bash's own positional/special parameters -- never "assigned" by any skill's own code,
# so a use of one is never a finding.
_SPECIAL_VARS = {"?", "!", "$", "#", "@", "*", "-", "_"} | {str(i) for i in range(10)}

# Environment variables this repo's skills legitimately rely on being set OUTSIDE any
# fenced block -- by the calling shell, the operator, or (for the two LAND_* knobs) an
# explicit override convention documented in docs/agents-workflow.md, which is where
# landing-loop dev-tooling knobs live rather than docs/configuration.md. Each entry
# needs a reason, same bar as the per-(file, var) allowlist below.
_KNOWN_ENV_VARS = {
    "LAND_LOCK_STALE_SECONDS",  # scripts/land-lock.sh's operator-settable staleness
    # override; land/SKILL.md and docs/agents-workflow.md document it, this repo's
    # skills never assign it.
    "TMPDIR",  # standard POSIX env var; sweep/SKILL.md reads it only via `${TMPDIR:-/tmp}`
    # to place its own cross-block scratch state, and never assigns it.
    "LAND_WORKTREE_DIRONLY_MIN_AGE_SECONDS",  # (lode-yrtu) operator-settable age floor
    # for Section 4's clean-not-merged worktree-agent-* dir-only reclaim; read only via
    # `${LAND_WORKTREE_DIRONLY_MIN_AGE_SECONDS:-21600}`, documented in
    # docs/agents-workflow.md, never assigned by any skill's own bash.
}

# (path relative to CLAUDE_DIR, variable name) -> reason a human can audit. Relative
# to `.claude/`, not to either SKILLS_DIR or AGENTS_DIR (lode-lv04 widened this gate to
# both roots), so a key can never be ambiguous about which side of the tree it names --
# e.g. "skills/land/SKILL.md" vs. a hypothetical "agents/land.md". An entry with no
# reason is exactly how this bug class was allowed to rot in the first place
# (lode-x495) -- never add one without a specific, checkable justification.
#
# Scope of an entry is FILE-WIDE, not block-scoped: allowlisting ($ACCEPTED, land) also
# excuses a NEW block that references $ACCEPTED cross-block (verified by sabotage). That
# is the deliberate trade -- a (file, block_index, var) key would be more precise but
# would break every time anyone inserted a block earlier in the file, failing on
# unrelated edits until someone "fixed" it by widening the entry. Keep entries rare and
# keep the names specific; a generic name here is much costlier than a specific one.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("skills/release/SKILL.md", "PROPOSED"): (
        "The human-confirmed version string from Section 3's confirmation dialogue. "
        "Never computed by any bash in this file -- Section 2's scripts/release-bump.sh "
        "only classifies breaking/feat/fix/none, the actual X.Y.Z arithmetic (or an "
        "explicit override) is applied by the agent's own reasoning and confirmed in "
        "conversation, not in a shell. There is nothing upstream to re-derive or "
        "persist from; the agent supplies the literal confirmed version at Section 4's "
        "invocation site, the same way it fills in a `<...>` template placeholder."
    ),
    ("skills/land/SKILL.md", "ACCEPTED"): (
        "Section 3a's ordered, land-review-verdict-derived accepted set -- the same "
        "shape as release/SKILL.md's $PROPOSED above: computed by the agent's own "
        "reasoning across Sections 2c (dispatched land-review verdicts) and 3a "
        "(stacked-branch ordering), never by any single deterministic bash command in "
        "the file, so there is nothing upstream in this file's own bash to re-derive or "
        "persist it FROM. Note the block that uses it immediately persists it onward "
        "($STATE_DIR/accepted), which every later block reads back -- so the cross-block "
        "hop this gate exists to catch is already closed downstream; only the initial "
        "hand-off from the agent's reasoning into bash remains. Removing this entry "
        "needs a genuine mechanical source for the set: lode-p1r3."
    ),
    ("skills/land/SKILL.md", "CONFLICTS"): (
        "The 'Needs rebase -- kick back' block interpolates the conflicting paths that "
        "Section 2b's merge-precheck (or Section 3's merge loop) captured -- a real, "
        "confirmed instance of this bug class, already tracked and fixed by lode-rfon "
        "($STATE_DIR/conflicts/<id>). Allowlisted rather than fixed here only because "
        "that fix belongs to lode-rfon's branch and this one does not merge trunk in; "
        "verified against origin/land/lode-rfon, whose land/SKILL.md no longer trips "
        "this. The entry goes inert the moment lode-rfon lands and should then be "
        "deleted (lode-p1r3)."
    ),
}


def _strip_comment(line: str) -> str:
    """Truncate at the first unquoted `#` that starts a real comment (line start, or
    preceded by whitespace) -- never a `${VAR#pattern}` parameter-expansion operator,
    which has no preceding whitespace. Quote tracking is intentionally simple (no
    backslash-escape handling) -- sufficient for this corpus, verified against every
    real block while writing this gate."""
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif (
            ch == "#"
            and not in_single
            and not in_double
            and (i == 0 or line[i - 1] in " \t")
        ):
            return line[:i]
    return line


# ---- USE extraction --------------------------------------------------------------
# $VAR or ${VAR...}. Never $(...)  (command substitution) or $((...)) as a use of a
# variable literally named "(" -- both regexes require an identifier character
# immediately after the $/${, so neither matches those.
_USE_SIMPLE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_USE_BRACED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")


def _used_vars(block: str) -> set[str]:
    found: set[str] = set()
    for raw_line in block.splitlines():
        line = _strip_comment(raw_line)
        found.update(_USE_SIMPLE.findall(line))
        found.update(_USE_BRACED.findall(line))
    return found - _SPECIAL_VARS


# ---- ASSIGNMENT extraction --------------------------------------------------------
# A statement boundary: start of line, after a separator, or after a keyword that
# introduces a new command. Shared by both assignment regexes below -- they were
# written with two hand-copied alternations, and the copy had silently lost
# `else|elif|if|while|until`, so `else declare -a Q` read as unassigned while the
# equivalent `else X=1` did not (lode-x495 review; pinned by
# test_declare_after_else_is_an_assignment).
_STMT_BOUNDARY = r"(?:^|[;&|(]|\b(?:then|do|else|elif|if|while|until)\b)\s*"

# `(?:!\s*)?` for a negated command, e.g. `if ! DEPS=$(...); then`.
# `(?!=)` so a `==` comparison is never read as an assignment.
_ASSIGN_STMT = re.compile(
    _STMT_BOUNDARY + r"(?:!\s*)?"
    r"(?:export\s+|local\s+|readonly\s+|declare\s+(?:-[A-Za-z]+\s+)*)?"
    r"([A-Za-z_][A-Za-z0-9_]*)\+?=(?!=)"
)
# A bare `declare -A VAR` / `local -a VAR` (no `=`) -- still a real assignment/declaration.
_ASSIGN_DECLARE_NOEQ = re.compile(
    _STMT_BOUNDARY
    + r"(?:local|declare|readonly)\s+(?:-[A-Za-z]+\s+)+([A-Za-z_][A-Za-z0-9_]*)\b(?!=)"
)
_ASSIGN_MAPFILE = re.compile(
    r"\b(?:mapfile|readarray)\s+(?:-[A-Za-z]+\s+)*([A-Za-z_][A-Za-z0-9_]*)\b"
)
_ASSIGN_FOR = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
_ASSIGN_FOR_C = re.compile(r"\bfor\s+\(\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
_ASSIGN_READ = re.compile(r"\bread\s+(?:-[A-Za-z]+\s+)*(.+)$")
_STATEMENT_TERMINATOR = re.compile(r"[;&|<>]")


def _assigned_vars(block: str) -> set[str]:
    found: set[str] = set()
    for raw_line in block.splitlines():
        line = _strip_comment(raw_line)
        found.update(_ASSIGN_STMT.findall(line))
        found.update(_ASSIGN_DECLARE_NOEQ.findall(line))
        found.update(_ASSIGN_MAPFILE.findall(line))
        found.update(_ASSIGN_FOR.findall(line))
        found.update(_ASSIGN_FOR_C.findall(line))
        m = _ASSIGN_READ.search(line)
        if m:
            # `while IFS=$'\t' read -r e TITLE; do` -- stop at the first statement
            # terminator so `; do` on the same physical line doesn't get tokenized
            # as though it named a variable.
            rest = _STATEMENT_TERMINATOR.split(m.group(1), maxsplit=1)[0]
            for tok in rest.split():
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tok):
                    found.add(tok)
    return found


def find_violations(path: Path) -> list[tuple[int, str]]:
    """(block index, variable name) for every USE in a block that is not also
    ASSIGNED somewhere in that same block, excluding special/known-env vars.
    Order is block index ascending, then variable name -- deterministic for a
    stable, readable failure message."""
    text = path.read_text(encoding="utf-8")
    violations: list[tuple[int, str]] = []
    for i, block in enumerate(_bash_blocks(text)):
        used = _used_vars(block) - _KNOWN_ENV_VARS
        assigned = _assigned_vars(block)
        for var in sorted(used - assigned):
            violations.append((i, var))
    return violations


def _source_files() -> list[Path]:
    """Every file this gate parses: each skill's SKILL.md, plus every agent
    definition under `.claude/agents/*.md` (lode-lv04) -- both execute fenced bash
    the same way, block by block, under the same harness rule. `ALLOWLIST` keys are
    relative to `CLAUDE_DIR`, computed the same way at each call site, so a key can
    never be ambiguous about which of the two roots it names."""
    return sorted(SKILLS_DIR.glob("*/SKILL.md")) + sorted(AGENTS_DIR.glob("*.md"))


# =====================================================================================
# Unit tests -- the parser's own precision, against synthetic snippets. Every case here
# is a pattern that either genuinely occurs in .claude/skills/*/SKILL.md today, or is
# named explicitly in lode-x495 as a false-positive risk to guard against.
# =====================================================================================


def _violations_in_block(block_text: str) -> set[str]:
    used = _used_vars(block_text) - _KNOWN_ENV_VARS
    assigned = _assigned_vars(block_text)
    return used - assigned


def test_simple_assignment_then_use_is_clean() -> None:
    assert _violations_in_block('FOO="bar"\necho "$FOO"\n') == set()


def test_use_with_no_assignment_anywhere_in_block_is_flagged() -> None:
    assert _violations_in_block('echo "$FOO"\n') == {"FOO"}


def test_cross_block_reference_is_flagged_the_land_regression_shape() -> None:
    """The exact lode-sfnb shape: assigned in one block, used in a DIFFERENT one.
    Each call below simulates a SEPARATE Bash tool invocation (a separate block) --
    `find_violations` is what actually enforces this over a real file; here we just
    confirm the per-block primitive sees block B's use as unassigned, regardless of
    what block A did (a different `_violations_in_block` call, therefore no shared
    Python state either -- mirrors "no shared shell state")."""
    block_a = (
        'declare -A MSG\nMSG[lode-abc]="Merge land/lode-abc: summary (lode-abc)"\n'
    )
    block_b = 'git merge -m "${MSG[lode-abc]}"\n'
    assert _violations_in_block(block_a) == set()
    assert _violations_in_block(block_b) == {"MSG"}


def test_read_assigns_every_named_variable() -> None:
    """Regression pin for the /sweep TITLE false positive (lode-x495's own audit): a
    cruder prototype had no `read` support at all, so `TITLE` in
    `while IFS=$'\\t' read -r e TITLE; do ... "$TITLE" ... done` read as unassigned."""
    block = "while IFS=$'\\t' read -r e TITLE; do\n  echo \"$e $TITLE\"\ndone\n"
    assert _violations_in_block(block) == set()


def test_indented_assignment_still_counts() -> None:
    """Regression pin for the /sweep ROW false positive: a cruder prototype anchored
    `^VAR=` with no leading-whitespace tolerance, so an assignment indented inside a
    loop body (completely ordinary shell) read as unassigned."""
    block = '  ROW=$(printf \'%s\' "hi")\n  echo "$ROW"\n'
    assert _violations_in_block(block) == set()


def test_for_loop_variable_is_assigned() -> None:
    block = 'for id in $ACCEPTED; do\n  echo "$id"\ndone\n'
    # $ACCEPTED itself is unassigned in THIS block (expected -- it's the point of
    # this test's fixture, not what's under test); $id must not also be flagged.
    assert _violations_in_block(block) == {"ACCEPTED"}


def test_command_substitution_is_not_a_use_of_a_variable_named_open_paren() -> None:
    assert _violations_in_block('X=$(echo hi)\necho "$X"\n') == set()


def test_arithmetic_expansion_does_not_false_positive_on_the_paren() -> None:
    block = "N=3\necho $((N + 1))\n"
    assert _violations_in_block(block) == set()


def test_default_expansion_is_a_use_not_an_assignment() -> None:
    """`${VAR:-default}` READS var (with a fallback); it must never be mistaken for
    an assignment of VAR. LAND_LOCK_STALE_SECONDS is excluded via _KNOWN_ENV_VARS
    (an operator override), so use a plain unknown name here instead."""
    assert _violations_in_block('echo "${TIMEOUT:-30}"\n') == {"TIMEOUT"}


def test_known_env_var_is_never_flagged() -> None:
    assert _violations_in_block('echo "${LAND_LOCK_STALE_SECONDS:-1800}"\n') == set()


def test_tmpdir_default_expansion_is_never_flagged() -> None:
    """Found during this gate's own development against the real files: sweep/SKILL.md
    reads `${TMPDIR:-/tmp}` to place its own cross-block scratch state -- a standard
    POSIX env var, never assigned by any skill's own bash."""
    assert (
        _violations_in_block('SWEEP_TMP="${TMPDIR:-/tmp}/lode-sweep-state"\n') == set()
    )


def test_special_parameters_are_never_flagged() -> None:
    block = 'echo "$?" "$1" "$@" "$#" "$$" "$!" "$-" "$_" "$0"\n'
    assert _violations_in_block(block) == set()


def test_comment_only_reference_is_not_a_use() -> None:
    """Regression pin for the land/SKILL.md false positives this gate's development
    found (lode-x495): heavy inline prose routinely quotes a variable name while
    describing history or a rejected design -- that must never count as a real use.
    Indented, because a real comment inside a loop body is."""
    block = 'true\n  #   grep -vxF "$dropped" "$STATE_DIR/accepted" > tmp\n'
    assert _violations_in_block(block) == set()


def test_parameter_expansion_hash_is_not_a_comment_start() -> None:
    """`${VAR#pattern}` / `${#ARR[@]}` -- the `#` is a parameter-expansion operator,
    not a comment, because nothing whitespace precedes it.

    The fixture is deliberately UNQUOTED. Two earlier pins for this rule wrote the
    `#` inside `"..."`, which made both vacuous: with the rule mutated to "any `#`
    starts a comment" the truncated line left no unassigned use either way, so
    neither test could fail (verified by mutation -- dropping the whitespace rule
    AND the quote tracking together killed zero tests). Here, truncating at the `#`
    would also swallow the `&& C=1` that follows, so the mutant reports `C` as
    unassigned and the test fails.
    """
    block = 'A=${B#x} && C=1\necho "$A $B $C"\n'
    assert _violations_in_block(block) == {"B"}


def test_if_assignment_with_negation_counts() -> None:
    """`if ! DEPS=$(cmd); then` -- land/SKILL.md's real Section-3-bounce shape.
    A prototype without `if`/negation support in its statement-boundary regex
    would miss this and flag DEPS as unassigned."""
    block = 'if ! DEPS=$(cmd); then\n  echo "$DEPS"\nfi\n'
    assert _violations_in_block(block) == set()


def test_equality_comparison_is_not_an_assignment() -> None:
    """`==` is a comparison, not a write, and must never register X as ASSIGNED --
    otherwise a real missing assignment sitting next to a comparison is masked.

    `((X==1))` and not `[ "$X" == "$Y" ]`: in the bracketed form the char before
    `==` is a quote, so no identifier abuts the `=` and `_ASSIGN_STMT` cannot match
    with OR without its `(?!=)` guard -- the earlier pin here was vacuous (verified:
    deleting `(?!=)` killed zero tests). The arithmetic form has the identifier
    directly against the `==`, so it is what actually exercises the guard.
    """
    block = 'echo "$X"\nif ((X==1)); then echo hi; fi\n'
    assert _violations_in_block(block) == {"X"}


def test_declare_dash_a_without_initializer_is_an_assignment() -> None:
    block = 'declare -A MSG\nMSG[foo]=bar\necho "${MSG[foo]}"\n'
    assert _violations_in_block(block) == set()


def test_declare_after_else_is_an_assignment() -> None:
    """`_ASSIGN_DECLARE_NOEQ` used to carry its own hand-copied, narrower boundary
    alternation (`^|[;&|(]|then|do`), so a bare `declare` after `else`/`if`/`while`
    read as unassigned while the equivalent `else X=1` did not. Both regexes now
    share `_STMT_BOUNDARY`; this fails if they are split again."""
    assert (
        _violations_in_block('if x; then y; else declare -a Q; fi\necho "${Q[@]}"\n')
        == set()
    )


def test_mapfile_and_readarray_assign_their_target() -> None:
    """No block in the corpus uses these today, so nothing else would notice if
    `_ASSIGN_MAPFILE` were deleted as dead -- it is not dead, it is unexercised."""
    assert _violations_in_block('mapfile -t ARR < f\necho "${ARR[0]}"\n') == set()
    assert _violations_in_block('readarray LINES < f\necho "$LINES"\n') == set()


def test_c_style_for_with_spaces_around_equals() -> None:
    """`for ((i = 0; ...))` is legal bash and is caught ONLY by `_ASSIGN_FOR_C` --
    `_ASSIGN_STMT` needs the `=` to abut the identifier. (Conversely `for ((i+=2))`
    is caught only by `_ASSIGN_STMT`.) They look redundant and are not."""
    assert (
        _violations_in_block("for (( i = 0; i<3; i++ )); do echo $i; done\n") == set()
    )


def test_export_and_local_prefixed_assignment() -> None:
    block = 'export FOO="bar"\nlocal BAZ="qux"\necho "$FOO $BAZ"\n'
    assert _violations_in_block(block) == set()


def test_non_bash_fence_is_never_scanned() -> None:
    """A plain (unlabeled) fence or a ```text fence is prose/template, never
    executed -- e.g. release/SKILL.md's confirmation template uses `<PROPOSED>`-style
    placeholders inside a plain fence, which must never be parsed as bash at all."""
    markdown = '```\necho "$UNASSIGNED"\n```\n'
    assert _bash_blocks(markdown) == []


def test_sh_fence_is_scanned_the_same_as_bash() -> None:
    markdown = '```sh\necho "$UNASSIGNED"\n```\n'
    blocks = _bash_blocks(markdown)
    assert len(blocks) == 1
    assert _violations_in_block(blocks[0]) == {"UNASSIGNED"}


def test_indented_fence_is_still_a_fence_the_lode_ovgs_shape() -> None:
    """A fence nested under a list item is indented, and a scanner anchored at column 0
    (`line.startswith("```")` -- what `tests/test_land_lock.py` did before `lode-ovgs`
    fixed it) is blind to it. Eight of `code/SKILL.md`'s nine bash blocks open with an
    indented fence (the ninth is blockquoted instead -- see the test below), so this is
    not hypothetical: a column-0 scanner reports that file as carrying no bash at all.
    `_bash_blocks` (the shared `bash_fence_blocks` helper, imported from
    `tests/conftest.py`) must strip first."""
    markdown = '1. Step one:\n\n   ```bash\n   echo "$UNASSIGNED"\n   ```\n'
    blocks = _bash_blocks(markdown)
    assert len(blocks) == 1
    assert _violations_in_block(blocks[0]) == {"UNASSIGNED"}


def test_blockquoted_fence_is_still_a_fence_the_lode_wroz_shape() -> None:
    """A fence nested inside a markdown blockquote (`> ```bash`) is a SECOND, independent
    blind spot from the indented one above: `>` survives `.strip()`, so
    `stripped.startswith("```")` never matched it even after lode-ovgs's fix. This is not
    hypothetical either -- four of `code/SKILL.md`'s nine bash blocks open this way
    (~lines 65, 292, 324, 367). Line 65 is this test's exact shape -- blockquoted at
    column 0, no indentation -- so it is the one fence in the corpus that the indented
    test above cannot reach even in principle. `_bash_blocks` must strip the blockquote
    marker from the fence delimiters to open/close the block at all."""
    markdown = '> Step one:\n>\n> ```bash\n> echo "$UNASSIGNED"\n> ```\n'
    blocks = _bash_blocks(markdown)
    assert len(blocks) == 1
    assert _violations_in_block(blocks[0]) == {"UNASSIGNED"}


def test_blockquoted_fence_content_lines_are_also_unmarked() -> None:
    """Stripping the blockquote marker from the fence DELIMITERS is not enough on its
    own: every CONTENT line inside a blockquoted fence carries the same literal `> `
    prefix in the raw source (real code/SKILL.md shape: `> REPO_ROOT=...` on one line,
    `> ... "$REPO_ROOT" ...` on the next). If that prefix survived into the extracted
    block text, `REPO_ROOT=...`'s own `^`-anchored assignment regex would never match
    (the line would start with `> `, not the identifier), so a same-block
    assign-then-use pair would falsely report the variable as used-but-unassigned --
    exactly the false positive this test would catch if only the fence markers, and not
    the content, were unmarked."""
    markdown = '> ```bash\n> FOO=1\n> echo "$FOO"\n> ```\n'
    blocks = _bash_blocks(markdown)
    assert len(blocks) == 1
    assert _violations_in_block(blocks[0]) == set()


def test_two_separate_blocks_are_returned_separately() -> None:
    markdown = '```bash\nFOO=1\n```\nprose in between\n```bash\necho "$FOO"\n```\n'
    blocks = _bash_blocks(markdown)
    assert len(blocks) == 2
    assert _violations_in_block(blocks[0]) == set()
    assert _violations_in_block(blocks[1]) == {"FOO"}


# =====================================================================================
# The gate itself, against the real, shipped skill AND agent files.
# =====================================================================================


def test_allowlist_entries_all_have_a_reason() -> None:
    for key, reason in ALLOWLIST.items():
        assert reason.strip(), f"allowlist entry {key} has an empty reason"


def test_every_skill_and_agent_file_is_covered() -> None:
    """No file-level escape hatch exists, deliberately (lode-x495 review). A whole-file
    skip would leave NEW cross-block variables in that file unguarded too, not just the
    known ones -- and the file it was first reached for, `land/SKILL.md`, is the sole
    writer of `trunk`. Per-variable allowlisting keeps every other block in the file
    covered. This pins that: every skill AND every agent file carrying bash blocks is
    actually parsed (lode-lv04 added the `.claude/agents/*.md` half)."""
    scanned = [
        str(p.relative_to(CLAUDE_DIR))
        for p in _source_files()
        if _bash_blocks(p.read_text(encoding="utf-8"))
    ]
    assert "skills/land/SKILL.md" in scanned, scanned
    assert "agents/coding.md" in scanned, scanned
    # code/SKILL.md earns its own entry: it is the ONE real file whose every
    # fence opens off column 0 -- five plainly indented, three indented AND
    # blockquoted, one blockquoted only (lode-wroz) -- so it is the only one of
    # the three whose entry here goes red if `bash_fence_blocks` ever regresses
    # to a column-0 `line.startswith("```")` scanner (lode-ovgs). land/SKILL.md and
    # coding.md both keep 20 and 25 blocks under that regression -- still
    # non-empty, so they would sit here looking fine while the parser was
    # broken, and this coverage pin would pass vacuously. Verified by mutation.
    assert "skills/code/SKILL.md" in scanned, scanned


def test_code_skill_blockquoted_blocks_are_visible_and_clean() -> None:
    """Sabotage-verified against the REAL file, not a synthetic snippet (lode-wroz's
    acceptance criteria). `code/SKILL.md` carries nine bash blocks; four open with a
    blockquoted fence (~lines 65, 292, 324, 367) and were invisible to `_bash_blocks`
    before this fix. Both regressions were re-measured against this exact file rather
    than estimated: drop the blockquote strip from `bash_fence_blocks` and
    `len(blocks) == 9` fails at **5**; regress it further to the pre-lode-ovgs column-0
    `line.startswith("```")` scanner and it fails at **0**.

    The count alone is not enough -- a delimiters-only strip also parses to 9, measured.
    The load-bearing assertion is the last one: block 0 (~lines 65-68) is the real
    `$REPO_ROOT` assign-then-use pair the synthetic
    `test_blockquoted_fence_content_lines_are_also_unmarked` above covers in miniature,
    and it reports `{"REPO_ROOT"}` rather than `set()` under that partial fix."""
    text = (SKILLS_DIR / "code" / "SKILL.md").read_text(encoding="utf-8")
    blocks = _bash_blocks(text)
    assert len(blocks) == 9, blocks
    assert "REPO_ROOT" in blocks[0], blocks[0]
    assert _violations_in_block(blocks[0]) == set(), blocks[0]


def test_no_cross_block_shell_state_outside_the_allowlist() -> None:
    """The actual gate. EVERY `.claude/skills/*/SKILL.md` and every
    `.claude/agents/*.md` is parsed (lode-lv04); any (block, variable) violation not
    covered by `ALLOWLIST` fails this test with enough detail to find and fix it."""
    failures: list[str] = []
    for source_md in _source_files():
        rel = str(source_md.relative_to(CLAUDE_DIR))
        for block_index, var in find_violations(source_md):
            if (rel, var) in ALLOWLIST:
                continue
            failures.append(
                f"{rel} block {block_index}: ${var} is used but not assigned in the "
                f"same fenced block. Fenced ```bash blocks run as SEPARATE Bash tool "
                f"invocations -- shell state does not survive between them (lode-sfnb; "
                f"see docs/agents-workflow.md's 'Guard against cross-block shell "
                f"state...' section). Either re-derive ${var} inside this block, "
                f"persist it to a file an earlier block wrote and this one reads back "
                f"(with an assert-on-load), or add ('{rel}', '{var}') to ALLOWLIST in "
                f"this file with a specific reason."
            )
    assert not failures, "\n".join(failures)


def test_sabotaged_agent_file_is_caught_by_find_violations(tmp_path: Path) -> None:
    """lode-lv04's sabotage verification, kept as a permanent regression pin rather
    than a one-off manual check: a cross-block variable injected into a REAL agent
    file's fenced bash (the exact `.claude/agents/coding.md` regression shape -- a
    variable set in one ```bash block and read in a separate later one) must be
    caught by `find_violations`, the same primitive
    `test_no_cross_block_shell_state_outside_the_allowlist` runs over every skill and
    agent file.

    Routed through `find_violations` on a COPY under `tmp_path`; the real file on
    disk is never written. Calling that primitive rather than re-implementing its
    loop is what makes this a pin at all: an inlined copy of the loop body still
    passes with `find_violations` itself mutated to the file-global reading this
    module's "Why per-block, not file-global" section rejects, since under that
    reading `SABOTAGE_VAR` IS assigned somewhere in the file. Verified by mutation
    during lode-lv04's review, where the inlined form was what shipped.

    Note this also depends on `.claude/agents/*.md` being globbed at all, which is
    `test_every_skill_and_agent_file_is_covered`'s job, not this test's -- reverting
    the widening fails there, not here.
    """
    sabotaged = tmp_path / "coding.md"
    sabotaged.write_text(
        (AGENTS_DIR / "coding.md").read_text(encoding="utf-8")
        + '\n```bash\nSABOTAGE_VAR=1\n```\n\n```bash\necho "$SABOTAGE_VAR"\n```\n',
        encoding="utf-8",
    )
    violations = find_violations(sabotaged)
    assert "SABOTAGE_VAR" in {v for _, v in violations}, violations
