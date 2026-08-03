#!/bin/bash -e
#
# Validate every Mermaid diagram in docs/ with mermaid-cli in Docker.
#
# mmdc parses each ```mermaid block against the same mermaid.js parser GitHub
# renders with, so syntax errors are caught before they ship. Runs in Docker so
# no Node/Chromium toolchain is needed on the host. Pull the image first with
# scripts/update-images.sh (or let `docker run` fetch it on demand).
#
# Usage: scripts/validate-mermaid.sh

# AUDIT (lode-bss5, Finding D): the shebang's `-e` is kept here, unlike the
# other gate scripts, which all deliberately run WITHOUT it because their
# machine-fault-vs-content split lives in explicit exit-code inspection that
# -e would short-circuit. This script's inspection instead lives entirely in
# `if`/`else` arms (`if docker run ...; then ... else rc=$?; ... fi`), which
# bash exempts from -e by its own rules -- so -e cannot short-circuit the
# split the way it would in the siblings.
#
# But -e is NOT free here, and do not read the above as saying it is: a -e
# abort exits with the FAILING COMMAND's status, and most commands fail with
# 1 -- which in THIS script means "invalid mermaid". Every non-`if` command
# is therefore a route from a machine fault to a fabricated content verdict,
# the exact lode-9i2p inversion. Measured (bash 5.2, shebang honoured):
# `mktemp -d` failing under -e exits 1, and so do the REPO= assignment, the
# printf into $CFG/puppeteer.json, and both chmod calls below. All five are
# now routed to exit 2 -- four through gate_could_not_run, and REPO= through
# its own pre-library fallback, argued at that line rather than restated here
# (lode-bss5, lode-3xqb).
#
# Two more found reviewing that pass, because guarding a command is not the
# same as guarding the SHELL (lode-3xqb):
#   * the `rm -rf` in the EXIT trap -- which under -e rewrites the status of
#     every exit below it, so it could undo all five guards above. Fixed at
#     the trap itself; see the comment there.
#   * `basename` in the loop, replaced with parameter expansion.
# What deliberately stays unguarded: the plain `echo`s. A failing echo (stdout
# full or closed) does still abort with 1, but wrapping five of them buys a
# vanishing case at real cost to readability. Two consequences to hold on to
# if you edit this file: `-e` is now doing no work this script wants -- every
# command whose failure means anything is `||`-guarded or inside an `if`, and
# -e's only remaining effect is to convert leftovers into the WRONG code
# (lode-6znq weighs deleting it, the alternative this ticket declined); and
# any new command added below is unguarded by default, i.e. a new route.
#
# What WAS missing, unlike every other consumer, was any top-level
# `-u`/`pipefail`; added below for parity. Inert today: this script contains
# no pipeline at all, and references no variable meant to expand unset. Note
# `-u` firing also exits 1, i.e. onto the same route as above.
set -uo pipefail

IMAGE="minlag/mermaid-cli:latest"
# Guarded rather than left to -e (lode-3xqb): a failing `cd`/`pwd` here --
# this script's own checkout moved or was deleted out from under it after it
# started running -- would otherwise abort with -e's own exit 1, which in
# this script means "invalid mermaid", blaming a fabricated content verdict
# on a checkout/machine fault. This runs BEFORE gate-lib.sh is sourced below,
# so gate_could_not_run is not yet defined -- same chicken-and-egg reason the
# source guard immediately below hardcodes its own fallback instead of
# calling it.
#
# Honest about the cost, though: unlike the source guard -- which is the guard
# FOR the source and so genuinely cannot use the library it is checking for --
# this ordering is not forced. Nothing between here and the source line uses
# $REPO, and that line derives its own path from $0 independently, so the two
# blocks could be swapped and this could call the helper. Until they are, this
# fallback emits the banner and cause but NOT the GATE_ADVISORY trailer below
# -- half the contract, which is exactly what gate-lib.sh's header warns about
# and what its ordering sweep cannot see, since this is not a call site at all.
# Deferred to lode-dyq0 rather than done here: lode-ysr6 is rewriting that same
# source line, and reordering it before that lands is the one edit that could
# merge into a source line missing its advisory args.
REPO="$(cd "$(dirname "$0")/.." && pwd)" || {
  echo "GATE COULD NOT RUN: could not resolve the repo root from \"\$0\" ($0)" >&2
  echo "-- its parent directory is missing or inaccessible. This is a" >&2
  echo "machine/checkout fault, not a mermaid syntax error." >&2
  exit 2
}

# `command -v docker` is a PROXY — it only proves some binary named `docker`
# is on PATH. When Docker Desktop's engine is stopped (e.g. Resource Saver
# mode, or WSL integration switched off for this distro), the Windows shim
# at .../Docker/resources/bin/docker still satisfies that check, then fails
# every container run with "The command docker could not be found in this
# WSL 2 distro" — indistinguishable, to a caller, from real per-doc syntax
# failures (lode-9i2p). Guard on the INVARIANT instead: can docker actually
# reach a running engine? `docker info` is the cheapest such probe. Exit 2
# (distinct from exit 1's "invalid mermaid") and print NO per-doc FAIL
# lines — an unusable engine is a machine fault, not content, and only a
# human can fix it.
#
# Both failure shapes exit 2, but they get DIFFERENT messages: naming one
# confident cause for every possible failure is the same bug this fix exists
# to kill, just relocated. `docker info` also fails when docker isn't
# installed at all, or when the socket denies permission — telling that user
# to "check Resource Saver mode" is exactly the plausible-but-false
# machine-level story that started this ticket.
#
# NOTE ON STRUCTURE: this probe is NOT what enforces the invariant — the
# per-doc exit-code check in the loop below is, and it subsumes this one (an
# engine that is down before the loop also makes `docker run` fail, which that
# check catches). This probe is a MESSAGE-QUALITY + FAIL-FAST layer on top: it
# can tell "no docker at all" apart from "binary present, engine unreachable",
# which the loop check — seeing only an integer — structurally cannot, and that
# distinction is the whole lode-9i2p lesson. It also aborts before the setup
# work below. Add new gate-could-not-run conditions to the LOOP check; add to
# this probe only to say something the exit code alone cannot.

# The ONE owner of the gate-could-not-run contract: the banner callers key on,
# the caller's cause lines, the standing instruction to a reader, and exit 2.
# Callers supply only the cause — the part that genuinely differs. Sourced
# from scripts/gate-lib.sh (lode-090f) so this contract cannot drift out of
# sync with the other gate scripts — they, plus .claude/agents/coding.md,
# code-reviewer.md and .claude/skills/land/SKILL.md, key on exactly this
# stderr shape.
# The source itself must fail CLOSED (lode-bss5) -- see gate-lib.sh's Usage
# section for the measurement and why the guard can't use the library it loads.
# This gate's own advisory trailer, bound at source time (lode-ysr6; see
# gate-lib.sh's GATE_ADVISORY contract for the mechanism and why it is not a
# separate assignment). tests/test_validate_mermaid_gate.py's
# _assert_gate_could_not_run pins the advisory TEXT below on an exit-2 path,
# which no static sweep can see -- route any new exit-2 test through it.
# shellcheck source=gate-lib.sh
if ! . "$(dirname "$0")/gate-lib.sh" \
     "This is a machine fault a human must fix, not a mermaid syntax error —" \
     "do not hand-verify diagrams or hand off in place of this gate."; then
  echo "GATE COULD NOT RUN: scripts/gate-lib.sh is missing or unreadable" >&2
  echo "next to $0 -- this is a machine/checkout fault, not a branch verdict." >&2
  exit 2
fi

if ! docker info >/dev/null 2>&1; then
  if command -v docker >/dev/null 2>&1; then
    gate_could_not_run \
      "docker engine unreachable — a docker binary is on" \
      "PATH but cannot reach a running engine. Usual causes: Docker Desktop is" \
      "stopped (Resource Saver mode), WSL integration is off for this distro," \
      "or the docker socket denies permission. Diagnose with: docker info"
  else
    gate_could_not_run \
      "no docker on PATH — mermaid validation runs the" \
      "parser in a container and needs Docker installed. See CLAUDE.md and" \
      "scripts/update-images.sh."
  fi
fi

# The image ships chromium at /usr/bin/chromium-browser, but puppeteer hunts for
# its own download; point it at the bundled binary. --no-sandbox is required
# because chromium cannot sandbox inside the container.
# Guarded rather than left to -e: under -e a failing `mktemp -d` aborts with
# mktemp's own exit 1, which is this script's CONTENT verdict ("invalid
# mermaid") -- a machine fault blamed on a doc. Route it to exit 2 instead,
# the same way scripts/merge-precheck.sh guards its own mktemp. (mktemp prints
# its own error; suppress it so the diagnostic below is the authoritative one.)
CFG="$(mktemp -d 2>/dev/null)" || gate_could_not_run \
  "could not create a temporary directory (mktemp failed)" \
  "Usual causes: TMPDIR points at a nonexistent, full, or read-only" \
  "filesystem. Diagnose with: mktemp -d"
# `|| :` is load-bearing, not defensive noise (lode-3xqb, found in review).
# MEASURED (bash 5.2): under -e, a command that fails inside an EXIT trap
# makes the shell exit with THAT command's status, clobbering the status the
# script had already chosen. So an unguarded `rm -rf` here silently rewrites
# every exit below -- including a guard's correct 2 and a clean run's 0 --
# into rm's own 1, this script's CONTENT verdict. Reproduced end-to-end
# against the real script: $CFG read-only fires the printf guard's exit 2,
# then the trap's failing rm turns it into "invalid mermaid" -- the lode-9i2p
# inversion re-entering through the one command no guard below can reach.
# Cleanup failure is not a verdict about anything, so swallow the status;
# rm's own stderr still names it.
trap 'rm -rf "$CFG" || :' EXIT
# Guarded rather than left to -e (lode-3xqb): a write failure here (disk
# full, or $CFG's filesystem gone read-only after mktemp created it) would
# otherwise abort with -e's own exit 1 -- this script's CONTENT verdict.
printf '{"executablePath":"/usr/bin/chromium-browser","args":["--no-sandbox","--disable-setuid-sandbox"]}' \
  > "$CFG/puppeteer.json" || gate_could_not_run \
  "could not write $CFG/puppeteer.json" \
  "Usual causes: the filesystem backing \$TMPDIR is full or went" \
  "read-only after mktemp created \$CFG. Diagnose with: df -h $CFG"
# tempfile dirs are 0700; the container's non-root user must read the mount.
# Both chmods guarded for the same reason as the printf above.
chmod 755 "$CFG" || gate_could_not_run \
  "could not chmod $CFG to 755" \
  "the container's non-root user needs read+traverse access to the" \
  "mounted config dir. Diagnose with: ls -ld $CFG"
chmod 644 "$CFG/puppeteer.json" || gate_could_not_run \
  "could not chmod $CFG/puppeteer.json to 644" \
  "the container's non-root user needs read access to the mounted" \
  "config file. Diagnose with: ls -l $CFG/puppeteer.json"

# THIS is where the invariant "a broken tool is never mistakable for broken
# content" is enforced. The partition is drawn around mmdc's ONE content
# verdict, not around docker's failure codes: exit 1 means "mmdc ran, parsed,
# and rejected the diagram" — the only exit allowed to print FAIL — and EVERY
# other nonzero exit means the gate could not run.
#
# Inverting it that way is the point. It fails SAFE (an exit code nobody
# anticipated escalates to a human rather than silently blaming an innocent
# doc), and it needs no list of docker's codes kept in sync as docker evolves.
# An allowlist of docker's 125-127 was this fix's first pass and was WRONG: the
# engine dying mid-run — the very flake that opened the ticket — kills a RUNNING
# container, which exits 128+SIGKILL = 137, not a pre-start code, so the
# allowlist printed FAIL for exactly the case it was written to catch.
#
# Measured 2026-07-14 against a live engine, not assumed (this fix's earlier
# pass — lode-9i2p — was itself a plausible structural theory that measurement
# dissolved):
#
#   CONTENT (exit 1 — the only verdict that may print FAIL):
#     a genuine mermaid syntax error, through mmdc              -> 1
#     mmdc handed a missing input file                          -> 1
#   TOOL (everything else — gate could not run, exit 2):
#     docker run <image absent, pull denied>                    -> 125
#     docker run <bad docker CLI flag>                          -> 125
#     docker run --entrypoint <not executable>                  -> 126
#     docker run --entrypoint <nonexistent cmd>                 -> 127
#     engine kills the container (docker kill / Resource Saver) -> 137
#     container OOM-killed (chromium is memory-hungry)          -> 137
#
# Docker does not *reserve* 125-127 either — a contained command exiting 125
# itself has that code passed through verbatim (also measured). Under this
# partition that ambiguity is harmless: it lands on the tool side, where a human
# looks, instead of being pinned on a doc.
fail=0
found=0
for f in "$REPO"/docs/*.md; do
  # Parameter expansion, not `basename` (lode-3xqb): identical result for a
  # glob match, and it removes the last unguarded external command under -e
  # -- a missing basename would abort the loop with basename's own status
  # rather than this script's contract. One fewer fork per doc, besides.
  # Computed before the grep check below so a machine-fault message can name
  # the file it happened on.
  rel="docs/${f##*/}"
  # grep exits 1 for "no match" -- a CONTENT answer: this doc genuinely has
  # no mermaid block, so skip it exactly as before. Anything else (2: file
  # unreadable, an I/O error, or -- if $REPO/docs vanished mid-run -- the
  # unmatched glob passed through literally) is a MACHINE fault, not a
  # content answer, and must not be treated the same way (lode-yoc3): a bare
  # `|| continue` here cannot tell the two apart, so an unreadable doc was
  # silently skipped as though it simply had no diagram -- and if every doc
  # errored this way, `found` stayed 0 and the gate printed a clean "nothing
  # to validate" PASS on a completely broken run. This is the mirror of the
  # `docker run` partition below: only ONE exit code is a real content
  # answer, everything else escalates, and the grep sits inside an `if` arm
  # for the same -e reason that block does (argued once, in AUDIT above).
  #
  # That vanished-$REPO/docs route reaches grep ONLY because this script
  # leaves `nullglob` unset, which is what passes the unmatched glob through
  # as a literal filename for grep to fail on. Do not set it here: with
  # `nullglob` the loop body would never run at all, `found` would stay 0,
  # and the gate would go straight back to a clean "nothing to validate" exit
  # 0 on a missing docs tree -- this exact bug, restored, with every test
  # below still green (they run against the real docs/, which always matches).
  #
  # `!` is deliberately NOT used here: `! cmd`'s $? is cmd's status LOGICALLY
  # NEGATED (0<->1), not cmd's own status, so `rc=$?` after `if ! grep ...;
  # then` would have captured the wrong number entirely -- measured while
  # writing this fix's own tests.
  if grep -q '```mermaid' "$f"; then
    found=1
  else
    rc=$?
    if [ "$rc" -ne 1 ]; then
      gate_could_not_run \
        "grep failed scanning $rel for a mermaid block (exit $rc) --" \
        "grep's exit 1 means \"no match\" (a content answer: no diagram in" \
        "this doc), so anything else is a machine fault, not content -- the" \
        "file may be unreadable, hit an I/O error, or \$REPO/docs may have" \
        "vanished mid-run. Diagnose with: grep -q -- '\`\`\`mermaid' $rel"
    fi
    continue
  fi
  if docker run --rm -v "$REPO:/data:ro" -v "$CFG:/cfg:ro" -w /data "$IMAGE" \
       -p /cfg/puppeteer.json -i "$rel" -o /tmp/out.md --quiet; then
    echo "OK    $rel"
  else
    rc=$?
    if [ "$rc" -ne 1 ]; then
      gate_could_not_run \
        "docker run failed with exit $rc while validating" \
        "$rel. mmdc reports invalid mermaid as exit 1 and nothing else, so this" \
        "is a tool failure, not a syntax error — the diagram was never judged." \
        "Usual causes: the image is missing and the network is unreachable (125)," \
        "or the engine killed the container mid-run — Docker Desktop stopping" \
        "(Resource Saver mode), or an out-of-memory kill (137). Diagnose with:" \
        "docker info"
    fi
    echo "FAIL  $rel"
    fail=1
  fi
done

if [ "$found" -eq 0 ]; then
  echo "no mermaid diagrams found in docs/ — nothing to validate"
  exit 0
fi

if [ "$fail" -ne 0 ]; then
  echo "mermaid validation failed" >&2
  exit 1
fi
echo "all mermaid diagrams valid"
