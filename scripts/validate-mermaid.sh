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
# SCOPE: this is a PRE-FLIGHT probe only. A docker-level failure *inside* the
# per-doc loop below (image missing with no network, engine dies mid-run) still
# surfaces as a per-doc FAIL, i.e. a broken tool still looks like broken content
# in that window. Narrowing that needs the loop to tell `docker run`'s own
# failures apart from mmdc's parse failures — deliberately out of scope here,
# tracked separately (see lode-9i2p's follow-up).
if ! docker info >/dev/null 2>&1; then
  if command -v docker >/dev/null 2>&1; then
    echo "GATE COULD NOT RUN: docker engine unreachable — a docker binary is on" >&2
    echo "PATH but cannot reach a running engine. Usual causes: Docker Desktop is" >&2
    echo "stopped (Resource Saver mode), WSL integration is off for this distro," >&2
    echo "or the docker socket denies permission. Diagnose with: docker info" >&2
  else
    echo "GATE COULD NOT RUN: no docker on PATH — mermaid validation runs the" >&2
    echo "parser in a container and needs Docker installed. See CLAUDE.md and" >&2
    echo "scripts/update-images.sh." >&2
  fi
  echo "This is a machine fault a human must fix, not a mermaid syntax error —" >&2
  echo "do not hand-verify diagrams or hand off in place of this gate." >&2
  exit 2
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
