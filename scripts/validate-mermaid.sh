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

IMAGE="minlag/mermaid-cli:latest"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

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
# from scripts/gate-lib.sh (lode-090f) so this contract cannot accidentally
# drift out of sync with scripts/merge-precheck.sh / scripts/release-bump.sh —
# all three, plus .claude/agents/coding.md, code-reviewer.md and
# .claude/skills/land/SKILL.md, key on exactly this stderr shape.
# shellcheck source=gate-lib.sh
. "$(dirname "$0")/gate-lib.sh"

# This gate's own domain-specific advisory, appended after every call's cause
# lines (see gate-lib.sh's GATE_ADVISORY contract) — set once, so it does not
# need repeating at each of this file's three call sites below.
# shellcheck disable=SC2034  # read by gate_could_not_run() in the sourced gate-lib.sh
GATE_ADVISORY=(
  "This is a machine fault a human must fix, not a mermaid syntax error —"
  "do not hand-verify diagrams or hand off in place of this gate."
)

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
CFG="$(mktemp -d)"
trap 'rm -rf "$CFG"' EXIT
printf '{"executablePath":"/usr/bin/chromium-browser","args":["--no-sandbox","--disable-setuid-sandbox"]}' \
  > "$CFG/puppeteer.json"
# tempfile dirs are 0700; the container's non-root user must read the mount.
chmod 755 "$CFG"
chmod 644 "$CFG/puppeteer.json"

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
  grep -q '```mermaid' "$f" || continue
  found=1
  rel="docs/$(basename "$f")"
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
