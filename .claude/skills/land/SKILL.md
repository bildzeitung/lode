---
name: land
description: Drain the ready-for-land queue — the SINGLE owner of every write to `trunk`. Per pass: cheap-precheck each `ready-for-land` branch (drift + does it still merge onto `trunk` — a conflict is kicked back `needs-rebase`, no review spent); semantic-review the survivors (via the `land-review` agent) → accept | bounce | escalate; batch-merge the accepted set `--no-ff` into `trunk`, re-gate once, isolate the culprit on red; then push `trunk`, `bd close` the landed tickets, flag any epic whose last child this pass closed with `epic-ready-to-audit` (for `/epic-audit`), `bd dolt push`, and GC the merged `land/<id>` branches and the local builder worktrees. Bounces open a new linked ticket carrying the findings; escalations leave the branch for a human and land nothing. Run self-paced as `/loop 5m /land` on ONE machine; a local lockfile guard skips a tick that would overlap a still-running land. Producers (`/code`) never land their own work — this skill does. Examples — "/land", "/loop 5m /land", "drain the ready-for-land queue", "land the reviewed branches".
---

# land

I am lode's **lander** — the **single, sole owner of every write to `trunk`**. Producers (`/code` →
`coding`) build reviewed, green branches, push them to `origin/land/<id>`, and mark their ticket
`ready-for-land`; they **never** merge, close, or push `trunk`. I am the other half of that contract:
I drain the `ready-for-land` queue, and **nothing reaches `trunk` except through me.** The whole
design lives in
[`docs/agents-workflow.md` — the landing loop](../../../docs/agents-workflow.md#the-landing-loop--build-review-land)
(read it; "The lander" and "Mechanics (decided)" are the source of truth) and the decided mechanics
in [`docs/decisions.md`](../../../docs/decisions.md). Where this skill and `CLAUDE.md` disagree,
**`CLAUDE.md` wins** — surface the drift instead of diverging.

I run on the **main checkout, on `trunk`** — I am the *one* agent allowed to. (Producers are the
inverse: they may never touch `trunk`. I never touch a producer's worktree.) I am typically invoked
self-paced as **`/loop 5m /land`** so I drain the queue while you work, with no daemon to manage.

**Run me from an Opus session.** I am a skill, not a subagent — I have no model of my own and inherit
the model of the session that runs the `/loop 5m /land`. My semantic review and combined re-gate are
exactly where Opus judgment earns its keep, so don't `/fast` the lander (the `coding` builder is the
one that runs cheaper on Sonnet; the `code-reviewer` and I stay on Opus).

## The merge decision belongs to the agent that didn't write the code

My **first task per branch is a semantic review I do not perform myself** — I dispatch the
[`land-review`](../../agents/land-review.md) agent (the build-side twin of `challenge`). The independence is
the point: the producer already ran the *technical* review (its own reasoned correctness pass —
`/code-review` is unreachable from any model context, lode-axyq — plus the tool-backed `/simplify` =
bugs & cleanup) on its own branch with gates green; I add the *semantic* gate — *should this land?* — from
the outside. I do **not** re-run the technical review and I assume the branch is green until my
re-gate says otherwise.

## Governing rule: no fenced block may depend on shell state from another (lode-sfnb)

**I run each fenced `bash` block below as its own, separate Bash tool invocation. Nothing carries
over between them** — not variables, not arrays, not function definitions, not `trap`s, not `set -e`
/ `set -o pipefail`, not background jobs. Anything one block needs from an earlier one is either
**re-derived** (cheap, deterministic — e.g. `$(git rev-parse --git-dir)`) or **persisted to a
file** under `$STATE_DIR` (`.git/land-state/`, which survives `git reset --hard` because that only
touches the index and working tree). Logic shared by two call sites lives in `scripts/`, never in a
bash function defined in one block and called from another.

This is not a style preference — it is the defect this skill has already shipped once. Section 3a
used to populate a `declare -A MSG` associative array that Section 3's merge loop read back; by the
time the loop ran, `MSG` was empty, and `git merge -m ''` failed with *completely empty stdout and
stderr* (OBSERVED, 2026-07-26, landing lode-ns3r/lode-1q2i/lode-sys4). **Every such failure is
silent by default**, and this is the one skill that writes `trunk` — so any block that loads state
must also *assert it loaded* and abort loudly if it did not. A loop that iterates zero times and
exits 0 is indistinguishable from a clean pass that had nothing to do.

**This rule, and the mechanical gate that now backstops it repo-wide, are recorded once, in
[`docs/agents-workflow.md`](../../../docs/agents-workflow.md#guard-against-cross-block-shell-state-in-skill-markdown-lode-sfnb--lode-x495)
— that is the source of truth, not this restatement.** `lode-x495` found the same bug class in
`/sweep` and `/release` (both since fixed) and shipped `tests/test_skill_bash_state.py` to catch a
regression to this file or any other skill's markdown. **This file is covered by that gate**, so a
newly-introduced cross-block variable here fails `nox -s tests`. One name is allowlisted rather than
fixed — `$ACCEPTED` (Section 3a; derived by my own reasoning over the land-review verdicts, so there
is nothing upstream in this file's bash to re-derive it from). Everything else in this file is gated
mechanically.

---

## 0. Single-lander lock — acquire FIRST, every tick

Being the **single** lander is what serializes landing. v1 guarantees that with **(a)** a local
"skip if already running" lockfile guard and **(b)** the convention that the `/land` loop runs on
**one machine**. (The distributed `refs/locks/land` ref for true concurrent multi-machine landing is
a deferred upgrade, recorded in `docs/decisions.md` — **not** v1.)

**This lock is real state that must span the whole pass, across every fenced block below — exactly
the shape the governing rule above warns cannot survive a `trap` or a `$$` (lode-aps3).** It used to
be managed inline here and was therefore **inert**: the release fired the instant this block's own
shell exited, before Section 1 ran, and the stale-lock reclaim judged liveness from a PID that is
*always* already dead by the time a later block reads it. Both halves are now in
`scripts/land-lock.sh`, which replaces them with a wall-clock staleness token — the full reasoning,
and the mechanism's two known limits, are in that script's header;
[docs/agents-workflow.md](../../../docs/agents-workflow.md#mechanics-decided)'s single-lander-lock
bullet is the design home. `tests/test_land_lock.py` pins both the script's behaviour and these call
sites, so this section cannot quietly go back to an inline lock.

**The token is now a heartbeat, not a one-shot stamp (lode-m87j).** [Section
2a](#2a-re-validate-that-beads-and-git-havent-drifted) re-stamps it once per ticket in the vet loop
(right before that ticket's `land-review` dispatch) and `scripts/land-merge-one.sh` re-stamps it on
every call, covering both Section 3 merge loops — so a pass no longer risks having its *own* lock
reclaimed mid-merge just for running long. Two more boundary call sites (lode-v4sv) close the two
originally-uncovered stretches that *grew* with queue size: one right before Section 1a (below,
covering Section 1's networked calls before it and isolating Section 1a's O(n²) work after it), and
one at the top of Section 4's main block (covering the per-ticket `bd close`/`epic-completion-check.sh`
work, the networked `bd-dolt-push.sh`, the branch deletes, and the worktree-GC sweep). **That did not
shorten the window below, and the four call sites still do not cover literally every line of the
pass**: Section 3's single combined re-gate (~60s measured, and — unlike the two gaps just closed —
does not grow with queue size) still runs unheartbeated. `scripts/land-lock.sh`'s header has the full,
current accounting and explains why the default stays at 1800s; re-deriving the number itself is
lode-cp4o (closed 2026-08-07 — the number stays 1800s permanently).

**What I need to know to run the pass:** the lock is released explicitly at exactly two sites below —
the empty-queue exit in [Section 1](#1-setup-the-pass--dolt-authoritative-fetch-origin) and the end
of a full pass in [Section 4](#4-land-the-survivors). **Every other way a pass stops leaves the lock
held until it ages out** after `LAND_LOCK_STALE_SECONDS` (default 1800s/30min) — genuine machine
faults (an exit-2 stop, an isolation-replay baseline red, a crash) age out this way, and that is
correct: a TTL that asks nothing of any exit site cannot be silently broken by a future "stop the
pass" that forgets to release, which is the same reasoning the pass-start `git reset --hard` uses in
Section 1. Adding release calls per exit site was deliberately rejected on that basis, and still is.

**A pass in which every branch was bounced, escalated, held, or kicked back `needs-rebase` is NOT
one of those exit sites (lode-0jan).** [Section 3](#3-batch-merge-the-accepted-set-re-gate-once-isolate-on-red)'s
empty-`accepted` guard used to abort identically whether `$STATE_DIR/accepted` was **missing** (3a's
precompute never ran — a real silent-failure shape, still aborted loudly, unchanged) or merely
**empty** (every branch already left the set for a legitimate reason). The empty case is not a
failure: the loop that reads `$ACCEPTED` correctly iterates zero times, the re-gate that follows is
*skipped* (`trunk` is unchanged, so there is nothing this pass introduced to gate — see that
section's own note; it is skipped rather than merely harmless, since running it would cost a full
suite for no new content), and the pass flows straight through to [Section
4](#4-land-the-survivors) — which already handles an empty `$LANDED` correctly by construction (its
own guard there makes the same missing-vs-empty distinction). This covers the `needs-rebase`-only
case too, by the same route: a branch kicked back mid-loop is dropped from `$STATE_DIR/accepted`
before the re-gate runs, so an all-`needs-rebase` pass converges on the same empty-but-present file
and the same fall-through. This is the **narrow** fix: one specific outcome (an empty-but-present
accepted set) stops being treated as an exit at all and instead flows into the pass's existing single
end-of-pass path — it is not a scattering of release calls across every exit site, and the rejection
above still stands for every genuine abort.

Before doing anything else, take the local lock. If another `/land` is still running on this machine
(a long pass overrunning a `/loop` tick), **skip this tick cleanly and exit 0** — do not queue, do
not run in parallel:

```bash
STATE_DIR="$(git rev-parse --git-dir)/land-state"    # re-derive -- fresh Bash invocation (lode-sfnb)
mkdir -p "$STATE_DIR"
# On a non-zero acquire, this skip line goes to STDERR and points AT the
# diagnostic land-lock.sh has already printed there itself -- whose wording
# distinguishes a transient "another /land appears to still be running" from a
# permanent MACHINE FAULT (flock missing, rev-parse failure, an unwritable lock
# dir), the distinction a reader of `/loop 5m /land`'s output needs and that a
# generic line alone invites them to skim past (lode-119w; docs/agents-workflow.md).
# Same stream, so it is ordered immediately after it. Deliberately NOT `2>&1`
# into $ACQUIRE_OUT: land-lock.sh's token contract is its STDOUT, and on the
# SUCCESS path that variable is the input to the token parse below, whose
# failure aborts the pass -- widening it buys nothing there and risks that parse.
#
# lode-oup2: stderr is ALSO captured to a scratch file (never `2>&1` into
# $ACQUIRE_OUT itself, same reasoning as above) so the failure branch below can
# inspect it for land-lock.sh's own escalation marker without a second
# `acquire` call (which would double-count its consecutive-fault counter).
# Nothing changes on the success path -- `acquire` never writes stderr there.
#
# That capture file is deliberately NOT under $STATE_DIR, for the same reason
# land-lock.sh keeps its fault counter out of $GIT_COMMON_DIR: an unwritable git
# dir IS the headline fault being escalated, and a redirect into the git dir
# fails BEFORE `acquire` runs at all -- no counter bump, no ESCALATE marker, and
# lode-119w's diagnostic gone too. Both halves live under ${TMPDIR:-/tmp} so
# both survive that fault.
ACQUIRE_ERR_FILE="${TMPDIR:-/tmp}/lode-land-lock-acquire-stderr"
ACQUIRE_OUT="$(scripts/land-lock.sh acquire 2>"$ACQUIRE_ERR_FILE")" || {
  ACQUIRE_ERR="$(cat "$ACQUIRE_ERR_FILE" 2>/dev/null || true)"
  echo "$ACQUIRE_ERR" >&2
  echo "land: could not acquire the lock this tick -- skipping. Read land-lock.sh's" \
    "own diagnostic immediately above: a MACHINE FAULT there is PERMANENT on this" \
    "machine and blocks landing until a human fixes it -- not an overrunning tick." >&2
  # lode-oup2: land-lock.sh only DETECTS a persistent fault (its own
  # consecutive-MACHINE-FAULT counter past LAND_LOCK_FAULT_ESCALATE_THRESHOLD)
  # and marks it with the distinctly-prefixed "land-lock: ESCALATE" stderr
  # line -- it stays bd-free by design, same as the rest of that script. THIS
  # is the one place that actually reaches a human: open a `human`-labeled
  # ticket -- lode's escalation mechanism, which `/sweep` already surfaces (no
  # new mechanism). Keyed by a fixed title and FILED ONCE per fault episode: a
  # fault that persists for days is thousands of ticks, so refreshing the ticket
  # per tick would grow its notes without bound and commit to Dolt every 5
  # minutes for information a human already has. The ticket EXISTING is the
  # signal; closing it re-arms filing, so a recurrence opens a fresh one.
  if grep -q 'land-lock: ESCALATE' "$ACQUIRE_ERR_FILE" 2>/dev/null; then
    # `--limit 0` (never a bare `bd list`, see docs/agents-workflow.md's
    # canonical "bd list defaults to a small page" rationale), and NO
    # `--status open` -- `bd list` already excludes closed issues, while pinning
    # `open` would miss this very ticket once a human moves it to
    # in_progress/blocked and then duplicate it every tick.
    #
    # The title comparison goes through jq's `env.` builtin rather than `--arg`
    # (no call site in this file uses `--arg`, and a `$name` binding inside the
    # jq PROGRAM string falsely trips tests/test_skill_bash_state.py's
    # cross-block-shell-state scanner, which has no notion of jq's quoting) --
    # `export` is what makes the value visible to the jq child process.
    export ESCALATION_TITLE="land-lock: persistent MACHINE FAULT is blocking /land on this machine"
    EXISTING_ESCALATION="$(bd list --label human --limit 0 --json \
      | jq -r '(. // [])[] | select(.title == env.ESCALATION_TITLE) | .id' | head -1)"
    if [ -z "$EXISTING_ESCALATION" ]; then
      bd create --type=decision --label=human \
        --title="$ESCALATION_TITLE" \
        --description="scripts/land-lock.sh acquire has hit a persistent MACHINE FAULT
under /loop 5m /land on this machine -- past its escalation threshold
(LAND_LOCK_FAULT_ESCALATE_THRESHOLD, default 3 consecutive ticks). This is not a
routine overrunning pass; it will not self-heal, and every tick keeps skipping
until a human fixes the underlying cause named in the diagnostic below.

This ticket is filed ONCE per fault episode and is not refreshed per tick -- the
live diagnostic is in the /land loop's own output. Do NOT run
\`scripts/land-lock.sh acquire\` by hand to check: on a machine that has since
been fixed it would take the lock out from under the loop. Close this ticket
once the machine is fixed; a recurrence after that opens a fresh one.

Diagnostic at the time of filing:
$ACQUIRE_ERR"
      bd dolt push
    fi
  fi
  exit 0
}
echo "$ACQUIRE_OUT"
# Persist THIS pass's own acquire token to disk (lode-q9pm), for every later
# heartbeat/release call site to re-read -- a file, not a variable, because no
# shell state survives between this file's separate Bash invocations (the
# governing rule above). Full threading mechanism: docs/agents-workflow.md's
# canonical paragraph (search that file for "Threading mechanism.").
# It lives OUTSIDE $STATE_DIR, beside .git/land.lock, because Section 1's
# per-pass scratch wipe (lode-wjw4) would otherwise delete it before any
# consumer read it -- it is lock state, not per-pass scratch (lode-l7mj).
# Reasoning: the same canonical paragraph.
# Loud-fail if the pattern doesn't match rather than silently persisting an
# empty token -- see scripts/land-lock.sh's own "acquired (token ...)"/
# "acquired via reclaim (token ...)" stdout contract.
printf '%s\n' "$ACQUIRE_OUT" \
  | grep -oE 'token [0-9a-f]+' | cut -d' ' -f2 > "$(git rev-parse --git-dir)/land-lock-token"
[ -s "$(git rev-parse --git-dir)/land-lock-token" ] || {
  echo "land: could not parse this pass's own token out of: $ACQUIRE_OUT" >&2
  # RELEASE BEFORE BAILING. We hold the lock as of two lines ago, and this is the
  # only exit path in the whole skill that aborts while holding it -- without this,
  # a bail here wedges landing for the FULL staleness window (~6 skipped /loop 5m
  # ticks) for what is a parse bug, not a running pass. The explicit
  # --land-lock-blind sentinel on purpose: we could not parse our own token, and
  # nothing else can have taken the lock in the microseconds since `acquire`
  # succeeded, so skipping the ownership comparison (the pre-lode-q9pm blind form)
  # is exactly right here -- land-lock.sh's own [own-token] argument is REQUIRED
  # as of lode-yuwt, so this is the one sanctioned opt-out, not an omission.
  scripts/land-lock.sh release --land-lock-blind   # land-lock-blind-ok: the one sanctioned opt-out, see above
  exit 1
}
```

**Convention:** run the `/land` loop on **one machine only** — the local lock does not cross
machines.

---

## 1. Setup the pass — Dolt-authoritative, fetch origin

I am the heaviest bd **writer** in the system (many closes, plus bounce-ticket creates, interleaved
with git merges and pushes), so I follow the bd-sync discipline strictly (see
[bd-sync discipline](#bd-sync-discipline-non-negotiable) below). At the start of each pass:

**Refuse to start unless I am actually in the main checkout (lode-pcee) — asserted once, up front,
as a precondition of the whole block rather than as a `-C` bolted onto individual commands.**
`--show-toplevel` resolves relative to
**cwd**, so `-C "$(git rev-parse --show-toplevel)"` — the form this block used to run the `checkout
-f trunk` through — is a no-op wherever it matters: in the main checkout it just re-states the cwd
you're already in, and in a worktree it resolves to *that worktree's own root*, not the main
checkout, so it can never redirect a command away from wherever you actually are. It reads as a
safety guard and is not one — and the destructive `reset --hard` two lines later carried no `-C` at
all, so from a worktree it would have hard-reset *that worktree's* branch, discarding any
uncommitted work there with nothing in `reflog` to recover it. `--git-common-dir` is the mechanism
that actually distinguishes the two: every worktree of a repo (main checkout included) shares one
common `.git` directory, and only the **main checkout's own toplevel** is that directory's parent —
a linked worktree's toplevel never is. The check is
[`scripts/assert-main-checkout.sh`](../../../scripts/assert-main-checkout.sh) — extracted rather than
inlined so it is shellcheck'd and unit-tested against real worktree fixtures the same way
`scripts/isolation-guard.sh` and `scripts/recycled-worktree-guard.sh` are (see its own header for the
full mechanism and exit-code contract).

**The guard is the FIRST LINE OF THE SAME fenced block as the commands it protects — never its own
block, and this is the whole point.** Per the [governing rule](#governing-rule-no-fenced-block-may-depend-on-shell-state-from-another-lode-sfnb)
above, every fenced block is a *separate* Bash invocation, so a guard in its own block can only
`exit` **that** block's shell — whether the destructive block then runs is left to my judgment
reading prose. That is exactly the strength of assurance lode-pcee exists to delete. Sharing one
block makes `||` do the work instead: `git reset --hard` is **unreachable** unless the assertion
passed, enforced by the shell, with no agent decision in between. Nothing here depends on state from
another block, so the rule is satisfied — the guard is re-run from scratch, not carried forward:

```bash
scripts/assert-main-checkout.sh || exit 1   # STOP THE PASS -- everything below assumes this passed
bd dolt pull            # Dolt is authoritative; pull the latest claim/label/close state over refs/dolt/data
git checkout -f trunk   # I land ON trunk, in the main checkout (just asserted above)
  # `-f` so this cannot FAIL (lode-k9ef) — not to clean anything; the reset below does that by itself.
git fetch origin        # I need origin/trunk and every origin/land/<id> fresh
STATE_DIR="$(git rev-parse --git-dir)/land-state"
rm -rf "$STATE_DIR"   # per-pass scratch the reset below cannot clear (lode-wjw4) -- see below
git log --oneline origin/trunk..trunk   # expected EMPTY; non-empty = residue, printed before it goes
  # Residue here is BY CONSTRUCTION merge commits (a pass that died between Section 3's merges and
  # Section 4's push), so this print must be faithful about merges -- the reset below destroys them.
git reset --hard origin/trunk   # pass-start reset, NOT `pull --rebase` (lode-k9ef) -- see below
```

**On a non-zero exit the pass stops there** — the script's own stderr already names cwd, the main
checkout, and why (exit 1 = genuinely the wrong directory; exit 2 = a machine fault rather than a
location verdict — `git rev-parse` could not answer at all, e.g. cwd is outside any repository, or
the layout is one the derivation does not support such as a bare repo or a submodule; both stop the
pass the same way, and neither is ever a reason to retry from here). Every command after it runs
unqualified — no `-C` on any of them — because the assertion is what guarantees cwd already *is* the
main checkout, which a `-C` derived from cwd itself never could.

**Why a hard reset, not `pull --rebase` (lode-k9ef).** I am the only **agent** that writes `trunk`, so
at pass start local `trunk` should already be bit-for-bit `origin/trunk`. The only way it legitimately
differs is a **previous** pass that died between
[Section 3](#3-batch-merge-the-accepted-set-re-gate-once-isolate-on-red)'s `--no-ff` merges and
[Section 4](#4-land-the-survivors)'s push — `validate-mermaid.sh` or `nox -s lock_currency` exit 2
(2b's precheck cannot: it runs before anything merges), or an ungraceful crash / `SIGTERM` / kill.
Those merges were never gated green **on their own** and never reached origin: residue, not work.

**This line is the *only* place that residue is cleared — every exit site below points here rather
than restoring `trunk` itself.** That generality costs timing: the residue is discarded at the *next*
pass's start, not at the instant of the stop, so it survives between passes. `/land` doesn't care (it
re-derives everything from `origin`), but it is not nothing — see the reader caveat in the write-up
below. What it buys is that a bare crash or kill self-heals too, which a per-exit-site restore cannot:
a killed pass runs no exit-site code at all. Content reds (exit **1**) are untouched — they still
isolate and bounce exactly as before.

Two things the reset does **not** do that `pull --rebase` did, both deliberate: it does not replay
local-only commits forward, and it does **not** refuse on a dirty tree or index. The second cuts both
ways. A staged `.beads/issues.jsonl` in the main checkout is real and observed — it is why the
[Section 3](#3-batch-merge-the-accepted-set-re-gate-once-isolate-on-red) merge loop unstages it every
pass — and that index state aborts `pull --rebase` outright while `reset --hard` absorbs it. **What
stages it is not established**; do not read this line as settling that (see the lode-bns3
reconciliation in Section 3). The same permissiveness lets the reset destroy genuinely uncommitted
work, which no reflog recovers — hence the `git log origin/trunk..trunk` line above, printed while the
residue still exists. Discarded *commits* stay in `git reflog`; discarded *uncommitted* work does not.
Do not "simplify" this back to `pull --rebase`.

**The `checkout -f` is load-bearing, and not for cleanup** — `reset --hard` clears an unmerged index
and `MERGE_HEAD` by itself. It is load-bearing because `reset --hard` moves whatever ref HEAD is on:
were HEAD ever detached, it would leave `trunk` untouched, and the checkout is what guarantees the
reset lands on the branch. `-f` is there purely so that checkout cannot *fail* — a pass killed
mid-`git merge` leaves an unmerged index, and a bare `checkout` then fails rc=1 ("you need to resolve
your current index first") *even when already on `trunk`*, stopping the pass at its second command in
exactly the crash case this reset exists to heal.

Full write-up, including the writer this does **not** cover:
[docs/agents-workflow.md — Mechanics (decided)](../../../docs/agents-workflow.md#mechanics-decided).

**The same block also wipes `$STATE_DIR` (lode-wjw4).** `.git/land-state/` is per-pass scratch the
reset cannot clear, and a leftover from a crashed prior pass is the same *residue* category as a
leftover merge commit; Section 0's lock is already held by this point, so this is its altitude. The
line only ever **removes** — each writer still `mkdir -p`s the subdirectory it needs (`$CONFLICTS_DIR`
in 2b, `$MSG_DIR`/`$CONFLICTS_DIR` in 3a) — so Section 1 never has to enumerate a subdirectory a later
section invents, and, running ahead of every writer, nothing has to be verbally ordered around it.
Its position *inside* the block is load-bearing too, and is pinned rather than remembered:
`tests/test_land_conflicts_state.py` fails if the wipe leaves this block, or if `git reset --hard`
stops being the block's **last** command — no block here runs under `set -e`, so that last command's
status is the only machine-readable signal the block gives, and `rm -rf` reports success even on a
path that does not exist.

**This reset is also load-bearing for a downstream consumer, and both halves are now pinned.**
`scripts/recycled-worktree-guard.sh` resets recycled agent worktrees onto `origin/trunk`, on the
premise that `/land` only ever advances that ref with already-gated content — a property of this
skill's step order, not of any lock. The guard is not the only consumer: every launch worktree
branches from `origin/trunk` (`.claude/settings.json`'s `worktree.baseRef: "fresh"`), so a reorder's
blast radius is every fresh agent worktree, not just this guard's reset path. This reset's own
placement is pinned by the test named above; the *other* half — [Section
4](#4-land-the-survivors)'s push waiting on [Section
3](#3-batch-merge-the-accepted-set-re-gate-once-isolate-on-red)'s re-gate — is now pinned too, by
`tests/test_land_conflicts_state.py::test_section_3_regate_precedes_section_4_push_origin_trunk`
(lode-youi, re-costing lode-rlz8's declined option (b)). It is a **document-order** assertion, not an
execution-order guarantee — see its docstring, which owns that caveat. Full dependency in the guard's
header (lode-rlz8).

Then read the queue — every ticket carrying the **`ready-for-land`** label (it stays `in_progress`;
the label, not a status, is the queue):

```bash
bd list --label ready-for-land --status in_progress --limit 0 --json
```

**`--limit 0` is load-bearing, not noise** — canonical reason + measurements, and why this is
hardening rather than a live fix, in [`/sweep`](../sweep/SKILL.md) (`lode-hwbm`). The stake here: a
truncated read wouldn't lose a branch outright (an unprocessed item just waits for the next pass), but
it would silently under-report and under-land a large backlog, every pass.

If the queue is empty, there is nothing to land: release the lock and stop —

```bash
# Normal completion -- release now rather than waiting out the staleness window for no reason.
MY_TOKEN="$(cat "$(git rev-parse --git-dir)/land-lock-token" 2>/dev/null || true)"
[ -n "$MY_TOKEN" ] || echo "land: WARNING -- no own-token available; land-lock ownership check is" \
  "DISABLED for this call (lode-67nk) -- land-lock.sh REFUSES it outright (exit 2, lode-yuwt)" \
  "rather than releasing blind, so the lock stays held until the staleness window reclaims it" >&2
scripts/land-lock.sh release "$MY_TOKEN"
exit 0
```

— otherwise process the batch.

**Heartbeat once more here, closing gap (a) (lode-v4sv).** Section 0's acquire → this point already
covers Section 1's two networked calls (`bd dolt pull`, `git fetch origin`); without a call here,
[Section 1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)'s `O(n^2)`
`git merge-base` work below — which grows with the size of the `ready-for-land` queue — ran completely
unheartbeated, all the way out to the first per-ticket heartbeat in
[Section 2a](#2a-re-validate-that-beads-and-git-havent-drifted). This single call does not itself
bound 1a's growth (it is one interval, not a per-pair heartbeat) — it isolates 1a as the sole
remaining contributor to this stretch, rather than 1a plus Section 1's networked calls combined. Same
best-effort contract as every other heartbeat call site: failure here is logged but never stops the
pass.

```bash
MY_TOKEN="$(cat "$(git rev-parse --git-dir)/land-lock-token" 2>/dev/null || true)"
[ -n "$MY_TOKEN" ] || echo "land: WARNING -- no own-token available; land-lock ownership check is" \
  "DISABLED for this call (lode-67nk) -- land-lock.sh REFUSES it outright (exit 2, lode-yuwt)" \
  "rather than re-stamping blind, so this heartbeat simply does not fire (|| true below)" >&2
scripts/land-lock.sh heartbeat "$MY_TOKEN" || true
```

---

## 1a. Compute the stacked-branch graph — once per pass, from git, never from bd

A producer sometimes must build one `land/<id>` branch **on top of** another still-unlanded
`land/<base>` branch — merging it in — because its ticket only makes sense once the base's code
exists (OBSERVED: lode-6qh / lode-96t — lode-96t was the error-handling fix *for* a command lode-6qh
introduced that was not yet on `trunk`; its branch merged `land/lode-6qh` to have something to fix).
Full contract: [docs/agents-workflow.md — Stacked land
branches](../../../docs/agents-workflow.md#stacked-land-branches-lode-02v). Nothing about a stacked
branch's *content* announces this — I detect it purely from **git history**. `coding.md` records a
`builds_on` bd field as a breadcrumb when it builds this way, but that is redundancy and intent
only — I never trust it as the mechanism; a producer that forgets to write it, or writes it wrong,
must not silently break this section.

**Detection — shared history off `trunk`, NOT tip-ancestry.** Two land branches cut independently from
`trunk` have nothing but `trunk` in common. So the relation to test is: **does any of their merge-bases
lie off `trunk`?** If one does, the pair **shares non-trunk history** — and that shared commit is a
base's tip *at the moment a dependent merged it*.

**Shared history is necessary, not sufficient — the direction test is what decides.** An off-trunk
merge-base means one of *two* things, and only the first is a stack:

- **A stack** — one of the pair merged the other. The direction test below finds the shared commit on
  the base's first-parent spine but not the dependent's, and emits the edge.
- **Siblings** — *two dependents that each merged the same third base* also share that base's commits,
  so they too have an off-trunk merge-base with **each other**, while neither is stacked on the other.
  Here the shared commit is off *both* spines (each reached it through a merge), the direction test
  matches neither ordering, and **no edge is emitted — which is the correct answer.** Each sibling is
  still correctly detected as stacked on the *base* by its own pair. This is a normal, supported shape
  (two producers may stack on one base); a related pair with no direction between them is not, by
  itself, a symptom of anything wrong.

```bash
git for-each-ref --format='%(refname:short)' 'refs/remotes/origin/land/*'
# for every ORDERED pair (X, Y) among the listed refs:
# ENUMERATE ALL merge-bases — a pair can have more than one (see below) — and keep only the
# off-trunk ones. A base branch that later takes a needs-rebase trunk-merge pickup (lode-cln)
# AFTER a dependent has already merged it acquires a SECOND merge-base: the dependent's own
# trunk cut point, which IS an ancestor of trunk. The single-result `git merge-base` picks one
# of the two ARBITRARILY, and when it happens to return the on-trunk one, the pair reads as
# unrelated and the stack goes undetected. `--all` sees every candidate; discarding the
# on-trunk ones and keeping any survivor is what makes this immune to that flow.
OFF_TRUNK=""
for mb in $(git merge-base --all "origin/land/<X>" "origin/land/<Y>"); do
  git merge-base --is-ancestor "$mb" origin/trunk || OFF_TRUNK="$OFF_TRUNK $mb"
done
[ -z "$OFF_TRUNK" ] && continue   # every merge-base is on trunk → unrelated
# At least one off-trunk merge-base → X and Y share non-trunk history. That is EITHER a stack OR two
# siblings on a common base; the direction test below is what tells them apart. Emitting no edge here
# is a normal outcome (siblings), not a failure.
# DIRECTION: the BASE is the one whose own first-parent spine contains an off-trunk MB — the
# dependent reached that commit through a merge (second parent), so it is not on its spine.
for mb in $OFF_TRUNK; do
  git rev-list --first-parent origin/trunk..origin/land/<X> | grep -qx "$mb" \
    && ! git rev-list --first-parent origin/trunk..origin/land/<Y> | grep -qx "$mb" \
    && echo "<Y> is stacked on <X>" && break
done
```

**Do NOT reduce this to `git merge-base --is-ancestor origin/land/<X> origin/land/<Y>`** (i.e. "is X's
current tip contained in Y"). That tests the base's **tip**, and a base's tip *moves after a dependent
merges it* — by an ordinary, entirely legitimate fast-forward: the code-reviewer pushes review fixes
onto `land/<base>` ([`code-reviewer.md`](../../agents/code-reviewer.md)), and a `needs-rebase` pickup
merges `trunk` into it (lode-cln). Both leave the dependent holding the base's *older* commits, so the
base's current tip is no longer an ancestor of the dependent and **the whole stack goes invisible** —
silently, with no error. That is not a corner case; it is the *normal* flow, because a producer stacks
on a base precisely while that base is still unlanded and therefore still moving. A detector that
misses the stack is worse than no detector, because the rest of this section trusts it and goes right
back to stranding dependents. But note the merge-base test above is immune to a fast-forward on either
side **only when every merge-base is considered, not one** — a single-result `git merge-base` walks
straight back into this same silent miss by a different route, for the reason spelled out in the
`--all` comment in the snippet above. Immunity comes from `--all` + the off-trunk filter, not from
using a merge-base per se.

Build this **once**, right here, as an in-memory map for the rest of the pass — never persisted, never
trusted from a prior pass (a branch can be bounced, dropped, or landed between passes, changing what's
live). Two shapes of the same relation get used below:

- **Full relation** (every base of Y, direct *or* transitive) — used by [Bounce](#bounce--clear-failure)
  and the [exit-(b)/(c) resolution paths](#resolving-a-land-escalated-branch) to ask "does deleting X
  strand a live descendant?" A transitively-stacked branch inherits X's content just as much as a
  directly-stacked one, so the strand check must not reduce to direct edges only.
- **Direct edges only** (X is Y's *nearest* base: no other base `B'` of Y has X as one of *its* bases —
  computed from the relation itself, never from tips, which move) — needed by
  [2c](#2c-run-the-semantic-gate) to pick the one base `land-review` diffs a stacked branch against:
  handing it a *transitive* base would reintroduce hole 2, because the diff would then carry the
  intermediate branch's work as if it were this branch's. [Section
  3a](#3a-order-the-accepted-set--base-before-dependent-hold-an-orphaned-dependent) uses the same view
  to order the merge set, though the topological sort would in fact run correctly off the full relation
  too (its extra transitive edges are already-implied constraints, not new ones) — the nearest-base
  pick is the reason this view has to exist.

**Known gap — documented, not claimed airtight.** The merge-base test survives any *append* to either
branch, but not a **rewrite**: if a base's history were force-pushed after a dependent merged it, the
shared commit is gone from the base's history entirely, the merge-base falls back to `trunk`, and the
pair reads as unrelated — the dependent still carries the base's old, now-orphaned commits. Nothing in
the current architecture force-pushes a `land/<id>` branch (every push on these branches is an ordinary
fast-forward), so this is a defense against a *future* change or a manual force-push, not a
live trigger today. There is no fully general fix short of every dependent re-checking after every base
push, which this ticket deliberately does not build (documented-YAGNI: this has happened once). **If a
`land/<id>` branch is ever force-pushed, the stacked-branch graph for that pass is not trustworthy** —
that is the honest limit of this mechanism, and it is stated here rather than papered over.

**A second known gap, same honest register: branched-from-base, not merged-base.** The direction test
assumes the dependent *merged* the base, so the shared commit sits on the base's first-parent spine
but not the dependent's. A producer that instead **branches directly off `land/<base>`** (rather than
branching from `trunk` and merging the base in, as `coding.md` instructs) puts that shared commit on
*both* branches' first-parent spines — the direction test finds it on both sides, matches neither
half of the `&&` condition, and emits no edge at all. Detection (the off-trunk-merge-base test) still
correctly flags the pair as related; only the *direction* is silently lost. `coding.md`'s sanctioned
build flow (branch from `trunk`, merge the base in) never produces this shape, but a producer HAS 
deviated from it — OBSERVED 2026-08-07, `land/lode-35nu.9` branched directly off `land/lode-kuc7`,
detected as related and silently given no direction — so unlike the force-push gap above this is a
LIVE trigger, not merely a defense against a future or off-process deviation, not something this
ticket builds a general fix for.

**And note it is not distinguishable from a sibling pair by signature alone**: both show up as "related
(off-trunk merge-base) but no edge." That is exactly why this stays a *documented gap* rather than
something to detect and warn on — a warning keyed on that signature would fire on every perfectly
normal sibling pair. If a stack is ever suspected but no edge appears, the thing to check by hand is
whether the dependent's first-parent spine reaches `trunk` at all, or dead-ends in the base.

---

## 2. Vet each branch — cheap prechecks first, then the semantic review

For **each** `ready-for-land` ticket, in this order. Steps 2a and 2b are **cheap gates I run before
spending Opus on the semantic review** — a branch that has drifted or no longer merges cleanly is
disqualified on mechanics alone, so I don't burn a `land-review` verifying contents that are about to
be thrown back.

### 2a. Re-validate that beads and git haven't drifted

The landing context is **minimal by design** — `land_head` + a one-line `land_summary` in bd
metadata, read via `bd show <id> --json` (the branch name is *derived*, `land/<id>`, never stored).
`land_head`/`land_summary` is the one field-name convention across the whole loop: `code-reviewer`
writes it when swapping a ticket to `ready-for-land`, and a rebase pickup (`coding.md`) refreshes it
on every re-push. The SHA exists only to **detect drift**: a push onto the branch *after* the ticket
was marked ready.

**First action of every iteration of this loop: heartbeat the single-lander lock (lode-m87j).** This
is the one call site (not per-section) that keeps the lock's staleness token measuring idle time
rather than this pass's total duration — it fires once per ticket, right before that ticket's
`land-review` Opus dispatch in 2c, so the gap the TTL has to outlast is one dispatch, not the sum
across the whole queue. `scripts/land-lock.sh`'s own header has the full reasoning; failure here is
logged but never stops the pass (this is lock bookkeeping, not the vet itself):

```bash
MY_TOKEN="$(cat "$(git rev-parse --git-dir)/land-lock-token" 2>/dev/null || true)"
[ -n "$MY_TOKEN" ] || echo "land: WARNING -- no own-token available; land-lock ownership check is" \
  "DISABLED for this call (lode-67nk) -- land-lock.sh REFUSES it outright (exit 2, lode-yuwt)" \
  "rather than re-stamping blind, so this iteration simply does not heartbeat (|| true below)" >&2
scripts/land-lock.sh heartbeat "$MY_TOKEN" || true
BD_JSON="$(bd show <id> --json)"     # read metadata.land_head and metadata.land_summary
LAND_HEAD="$(jq -r '.[0].metadata.land_head // empty' <<<"$BD_JSON")"
# Shape-check land_head BEFORE comparing it to anything (lode-xdg3, prose below).
# Exit 1 = malformed/missing metadata; exit 2 = this call is broken, fix the
# invocation and report nothing about the field. `|| exit $?` is load-bearing:
# there is no `set -e` here, so without it the block would run the drift
# comparison below on a value the check just rejected -- the exact thing the
# check exists to prevent -- while preserving the 1-vs-2 exit distinction.
scripts/validate-sha40.sh land_head "$LAND_HEAD" || exit $?
git ls-remote origin "refs/heads/land/<id>"   # branch must still exist on origin...
# ...and origin/land/<id>'s tip SHA must equal $LAND_HEAD
```

**Why the shape check, before the comparison (lode-xdg3).** A `bd update --set-metadata
land_head=...` call has no schema — a truncated or hand-retyped value (one hex digit short, say)
writes just as cleanly as a real one, and a malformed value never equals a real branch tip either,
so without this check it reads as ordinary drift and the branch is thrown back on that basis — for
this section, a **bounce** (the disposition the next paragraph gives drift), which supersedes the
ticket and drops the branch: a self-inflicted rebuild of work that was already correct (the
reproduction: a rebase pickup wrote a 39-character `land_head`, one digit short of the real tip).
`scripts/validate-sha40.sh` is the shared predicate, also used by `code-reviewer.md`'s own
`review_head` check, so both read sites can't drift on what "well-formed" means. It is called in the
same fenced block that reads the value because shell state does not survive between blocks
(lode-sfnb); `tests/test_validate_sha40_call_sites.py` pins that both read sites keep calling it.

**Deliberate asymmetry — do not "harmonize" it.** This check is **exact-match**, not the
ancestor-check `code-reviewer.md` uses for `review_head` (lode-9b5n / lode-xdg3 composition). The two
read sites share the same shape-check predicate but answer different questions: `/land` lands
**without** re-reviewing, so a forward push of never-reviewed commits onto `land/<id>` after
`ready-for-land` genuinely is drift here; `code-reviewer` reviews `trunk...HEAD` wholesale regardless
of what `review_head` names, so a forward push is harmless there. Collapsing this to one shared
comparison would either miss real drift here or falsely flag every exit (d) re-entry there — see
`docs/agents-workflow.md` and `docs/decisions.md` for the full reasoning.

A **missing branch** or a **SHA mismatch** is drift — treat it exactly like a review **bounce**
(below): I will not land a branch I can't verify is the reviewed one. A **malformed `land_head`**
(the check above failed) is a **distinct** outcome — neither drift nor a real mismatch, since there
is no well-formed value to compare in the first place — and it is an
**[escalate](#escalate--genuine-decision)**, never a bounce and never an in-pass repair
(DECISION, human, `lode-xdg3`). Concretely: **keep `origin/land/<id>`** (no delete, no supersede, no
rebuild ticket), **land nothing from it this pass**, label the ticket `land-escalated`, and say
explicitly in the escalation findings — "malformed `land_head` metadata, not drift" — what the human
owes: **re-derive** the value mechanically per
[`docs/conventions.md`](../../../docs/conventions.md)'s "Derive identifiers, never retype them" fiat
(`git rev-parse` / `git ls-remote` — never retyped, `lode-fpmi`), re-write the field, and re-enter the
ticket per the [re-entry table](#re-entry-per-escalating-source--re-enter-at-the-gate-that-escalated)
(this gate escalated and the resolution needs no branch edit, so it re-enters at `ready-for-land`).

**Why escalate rather than bounce or repair.** A corrupt hand-off record means **no drift evidence
exists at all** — and whether the branch is nonetheless the reviewed one is a *human* judgement,
which is exactly what escalation is for. Bouncing would `bd supersede` the ticket, open a rebuild
ticket and **delete** `land/<id>` — destroying the very branch whose field the remedy above asks a
human to re-write, rebuilding a reviewed, correct branch over one mistyped hex digit. Repairing it
in-pass (re-deriving from `git ls-remote` and continuing) is worse still: `land_head` records **what
the reviewer saw**, so re-deriving it yields the *current* tip and the subsequent comparison becomes
tip == tip — vacuously true. That does not fix the drift check, it deletes it while leaving it green,
and it is the only disposition that can let genuinely unreviewed drift reach `trunk`.

### 2b. Cheap conflict precheck — does it still merge onto `trunk`?

A branch that forked long ago is **not** stale-in-a-way-that-matters as long as it still merges
clean: `git merge --no-ff` integrates non-linear history fine, and the combined re-gate in
[Section 3](#3-batch-merge-the-accepted-set-re-gate-once-isolate-on-red) re-runs the tests on the
*merged* `trunk`. So a stale-but-clean branch needs no rebase and no special handling. What *does*
disqualify a branch is a **textual conflict** with current `trunk` — and discovering that only at
merge time (Section 3) means I've already paid for a full `land-review` on contents the rebase will
change. So I test it cheaply, up front, with a no-checkout trial merge against current `trunk`
(the pass already brought local `trunk` current with `origin/trunk` in Section 1, and I have not
merged anything yet this pass, so `origin/trunk` is the right base for every branch here), via
[`scripts/merge-precheck.sh`](../../../scripts/merge-precheck.sh) (extracted from an inline snippet
per lode-mh9g — two live defects found landing lode-l38d.6 are fixed there, with fixture-backed
tests in `tests/test_merge_precheck.py`; see the script's own header for the full writeup):

```bash
# $STATE_DIR is wiped once per pass, in Section 1 (lode-wjw4); re-derived here, fresh invocation.
STATE_DIR="$(git rev-parse --git-dir)/land-state"
CONFLICTS_DIR="$STATE_DIR/conflicts"
mkdir -p "$CONFLICTS_DIR"

# A command substitution inside an `if` condition is exempt from `set -e` —
# unlike a bare `VAR=$(cmd)` assignment, which would abort the shell on a
# non-zero exit before `rc=$?` is ever reached. Same idiom the snippet this
# replaced used, for the same reason.
if CONFLICTS=$(scripts/merge-precheck.sh origin/trunk "origin/land/<id>"); then
  rc=0
else
  rc=$?
fi

# $CONFLICTS does NOT survive to the "Needs rebase -- kick back" block below -- that block is a
# SEPARATE Bash invocation and this shell variable dies with this one (lode-rfon, the same defect
# class as lode-sfnb's $MSG/$ACCEPTED/$LANDED). Persist it to disk now, at the only point this
# block actually holds it; the kick-back block reads it back from the file, never from $CONFLICTS.
#
# An `if`, NOT `[ "$rc" = 1 ] && printf ...`. As this block's LAST command that AND-list makes the
# whole invocation exit 1 whenever rc is 0 -- so the COMMON clean path would report failure and a
# real conflict would report success, INVERTING the only signal this block gives the agent at all
# (it prints nothing on any path). That is exactly the "non-zero exit, completely empty stdout AND
# stderr" shape lode-sfnb documents in 3a below as the silent failure this file exists to remove.
# Testing `= 1` and not `!= 0` is also load-bearing: a machine fault (rc=2) leaves $CONFLICTS empty,
# and must NOT leave a file behind for a later kick-back to read as a conflict record.
if [ "$rc" = 1 ]; then
  printf '%s\n' "$CONFLICTS" > "$CONFLICTS_DIR/<id>"
fi
```

- **`rc=0`** → clean — proceed to the semantic gate (2c). (Prints nothing.)
- **`rc=1`** → textual conflict. `$CONFLICTS` (now also persisted to `$STATE_DIR/conflicts/<id>`,
  since the file — not the shell variable — is what the kick-back block actually reads) holds
  exactly the conflicting path(s), one per line — no tree OID, no chatter. → needs-rebase kick-back
  (see "Needs rebase — kick back"): skip `land-review`, leave the merge set. **Do that kick-back now,
  for this branch, while still in Section 2** — not batched up for later:
  [3a](#3a-order-the-accepted-set--base-before-dependent-hold-an-orphaned-dependent) already computes
  `$ACCEPTED` from outcomes that include "kicked back `needs-rebase`", so a branch reaching 3a
  un-kicked-back is out of order on its own terms.
- **`rc=2`** → **MACHINE FAULT, not a branch conflict** (git < 2.38, an unreadable/unknown ref, or
  `merge-tree` itself failing) — same [gate exit-code
  contract](../../../docs/agents-workflow.md#gate-exit-code-contract-012-lode-jhry) every other gate
  here honours. I do **not** kick this branch back `needs-rebase`. A machine fault blaming an
  innocent branch is exactly the defect this extraction closed (defect 2, in the script's header).
  Instead I **stop the pass** and surface the script's own stderr diagnostic verbatim as a human
  decision — it names the cause and the remedy, and only a human can fix the machine. This is the one
  behaviour change from the inline snippet this replaced; do not "simplify" it back into a kick-back.

A conflict (`rc=1`) is **neither a bounce nor an escalate** — the branch's *content* may be perfectly
fine, it simply can't replay onto where `trunk` now is. I handle it per
[Needs rebase — kick back](#needs-rebase--kick-back): remove `ready-for-land`, add `needs-rebase`,
**keep the branch and the build worktree**, and move on — **without dispatching `land-review`**. The
branch leaves this pass's merge set and the producer rebases it (this is where the noise went).

### 2c. Run the semantic gate

**Dispatch `subagent_type: "land-review"` (its own dedicated agent, lode-c6ir) via the Agent tool —
no `isolation` argument at the call site.** `land-review`'s own agent definition
(`.claude/agents/land-review.md`) carries `isolation: worktree` in its frontmatter, so the
requirement travels with the *role*: any dispatch of `subagent_type: "land-review"` lands isolated
whether or not the call site remembers to ask for it. That frontmatter is the **sole** enforcement
point for this dispatch — empirically confirmed by a dedicated probe (lode-p2vi, 2026-07-20); the
probe design and its control are recorded in [`docs/decisions.md`](../../../docs/decisions.md).
I run on **trunk, in the main checkout** (see above) — the same working tree Section 3 merges into. A
`land-review` dispatch that was *not* isolated would run *in that same tree*, and nothing stops a
reviewer, mid-inspection, from leaving files staged
or modified there (OBSERVED, 2026-07-19: three `land-review` agents dispatched with no isolation all
ran in the main checkout; one left `lode-2zj0`'s full diff staged, and the next branch's `git merge
--no-ff` aborted with "would be overwritten by merge" — with `git ls-files -u` empty, so it hit
neither the merge step's jsonl-restore retry path (now `scripts/land-merge-one.sh`, lode-sfnb) nor its
real-conflict path, and the failure silently
read as an unretried conflict rather than what it was). Frontmatter `isolation: worktree` launches the
reviewer already cwd'd inside its own `.claude/worktrees/agent-<hash>`, branched from `origin/trunk`
HEAD (`worktree.baseRef: "fresh"`, lode-jzbz) — the same *kind* of disposable launch worktree `code/SKILL.md` mandates for the `coding` and
`code-reviewer` dispatches. Those two now carry the same `isolation: worktree` frontmatter key
(`lode-ojsr`), and frontmatter is now the **sole** enforcement point for them too — measured
sufficient for both by dedicated top-level probes (`lode-09td`), so their call-site option was
dropped just as this one was. See [docs/decisions.md](../../../docs/decisions.md) (search
"lode-09td").
From there it `git fetch`es `origin/land/<id>` (and `origin/land/<base-id>` if
stacked) and diffs entirely by ref — it never needs to check anything
out — so under isolation any tree mutation it performs (accidental or not) lands in that disposable
worktree, never in the one Section 3 is about to merge into.

**That launch worktree is not reliably reclaimed "by construction" — it needed dedicated code, and
now has it (lode-qv5t).** `land-review` never commits (its own "What I don't do" — no merge, no push,
no `bd` writes), and the existing backstop sweep in [Section 4](#4-land-the-survivors) reclaims any
unlocked, clean worktree under `.claude/worktrees/` whose HEAD is an ancestor of `trunk` — but "never
commits" only proves the worktree's HEAD never *diverges further* once `land-review` starts; it says
nothing about where that HEAD was when the agent was dispatched. lode-nt98 established the harness's
`isolation: "worktree"` hand-off does not reliably start a dispatched agent at `origin/trunk` HEAD — it has
handed a builder and a `code-reviewer` a **recycled** worktree still checked out on a *previous*
ticket's build branch. `land-review` gets the identical dispatch mechanism, so a recycled worktree
handed to it starts with `HEAD` already **not** an ancestor of `trunk`, fails the sweep's ancestor
predicate, and leaks past every pass indefinitely — `land-review` never touching it doesn't fix that,
since the contamination predates its own first action. Its **correctness** is untouched either way (it
only ever fetches and diffs by ref, so a recycled worktree's foreign commits are simply never read —
that half of lode-nt98's exposure was, and remains, nil for this agent); this is purely a worktree-leak
defect, distinct from and not to be conflated with the correctness question. The fix:
`land-review.md`'s own frontmatter role now carries the same recycled-worktree guard `coding.md` and
`code-reviewer.md` carry (`git merge-base --is-ancestor HEAD origin/trunk`, never bare local `trunk`
— lode-isl3 — asserted before any fetch/diff work; a failure rescues the rewound ref and resets onto
`origin/trunk` HEAD) — see [`land-review.md`](../../agents/land-review.md) and
[docs/agents-workflow.md — Recycled-worktree guard](../../../docs/agents-workflow.md#recycled-worktree-guard-lode-nt98).
Once that guard has run, the worktree's HEAD **is** an ancestor of `trunk`, whether it started that
way or was just reset there — so the sweep's ancestry predicate reclaims it same as before; nothing
about Section 4 itself needed to change. That survives the guard's own detection blind spot (the
check cannot recognize a worktree recycled onto a `land/<other-id>` that has *since landed*, since its
`HEAD` is by then genuinely an ancestor of `origin/trunk`) intact, since what the guard fails to notice
already satisfies that predicate. **lode-3v1p** closed the sweep's *other* arm too: in the blind-spot
case, the remediation's `git clean -fd` used to run only inside the failed-check branch, so it never
fired there, and the recycled worktree's untracked leftovers survived to trip the
[lode-9hgu dirty-tree guard](#4-land-the-survivors) below, leaking the worktree anyway. `git clean
-fd` now runs **unconditionally**, right after the ancestor check, at all three guard sites
(`coding.md`, `code-reviewer.md`, `land-review.md`) — still scoped to `.claude/worktrees/` only by the
same `case` precondition. Nothing in Section 4 needed to change for this either; the fix lives
entirely at the dispatch-time guard, same layer as the rest of this fix. Full reasoning:
[docs/decisions.md](../../../docs/decisions.md) (search "lode-3v1p").

Normally that is the end of this very pass: Section 4 is
reached even when the accepted set is **empty** (nothing between 2c and 4 exits early on that
account — the merge loop simply iterates zero times), so the sweep is not conditional on anything
having landed. The exception is a pass that **aborts** after 2c has already spun up scratch
worktrees — the 2b precheck machine-fault stop, the `validate-mermaid.sh` exit-2 stop, and the
bounce path's `blocks-dependents.sh` `exit 1`. Those leak the scratch worktrees past the pass, and
the next pass that reaches Section 4 reclaims them (they are clean, unlocked, and ancestors of
`trunk`, so they still qualify) — bounded and self-healing, not a hazard, but don't read the
same-pass reclaim as unconditional. Full rationale:
[docs/agents-workflow.md — Isolating land-review dispatches](../../../docs/agents-workflow.md#isolating-land-review-dispatches-lode-g387).

Pass the ticket ID and its `land/<id>` branch. **If
[1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)'s direct-edge map
found this ticket stacked on exactly one live base**, also pass that base (`land/<base-id>`) —
land-review diffs against it instead of `trunk` (its own
[docs/agents-workflow.md#stacked-land-branches-lode-02v](../../../docs/agents-workflow.md#stacked-land-branches-lode-02v)-linked
handling). If it found *no* live base, or (the rare nested/multi-base case) more than one direct
base, hand land-review nothing extra and let it default to a `trunk` diff — but in the multi-base
case, note in the dispatch which other live land branches this one contains, so land-review doesn't
misread their content as scope creep even without a clean single base to diff against.

land-review reads both sides (ticket acceptance/design vs. the actual diff against the right base),
judges on acceptance / scope / design+invariants / approach, and returns exactly one verdict with
findings:

- **accept** → add the ticket to the **merge set** for this pass.
- **bounce** (a clear, confident failure) → handle per [Bounce](#bounce--clear-failure) below: open a
  new ticket carrying the findings, supersede the original, **drop the branch** — unless doing so
  would strand a live descendant (the bounce section's own descendant check), in which case it
  escalates instead. The ticket leaves the merge set either way.
- **escalate** (a genuine decision only a human can make) → handle per
  [Escalate](#escalate--genuine-decision) below: land **nothing** for it, **keep the branch**, label
  it, surface the question. It never enters the merge set.

Collect verdicts for the whole queue before merging — I want the full accepted set so I can
**batch**-merge.

---

## 3. Batch-merge the accepted set, re-gate once, isolate on red

Two branches each green *in isolation* can break when **combined** (a clean git merge with broken
behaviour). So I merge the whole accepted set, then re-gate the combined `trunk` **once**.

**Every re-gate in this section — the combined re-gate below and the isolation-replay loop's own
`nox -s tests` / `nox -s lock_currency` — runs in the FOREGROUND, in the same turn, and its result is
read from its own real exit status, never from a downstream command's.** No `run_in_background`, no
`Monitor`, no ending a turn on a pending gate — the same lode-95o rule the producer agents already carry
(`.claude/agents/coding.md`, `.claude/agents/code-reviewer.md`); `nox -s tests` fits well under
`Bash`'s 600000ms timeout cap. And never pipe a gate through `tail`/`head`/`grep` and read the
*pipeline's* exit status as the gate's own: a shell pipeline's exit status is its **last** element's,
not the gate's, so a killed or hung gate can surface as "completed, exit 0" while its own output ends
mid-run. **OBSERVED (lode-b8sr):** `nox -s tests 2>&1 | tail -30` exceeded the Bash timeout, was
moved to the background, hung, and was killed with `SIGTERM` — the harness reported the run as
"completed (exit code 0)" because that 0 was `tail`'s exit status, even though the captured output
itself ended in `nox > Session tests failed`. If a gate's output must be trimmed, capture its real
status explicitly first — `set -o pipefail`, `${PIPESTATUS[0]}`, or `cmd > file; status=$?; tail -30
file` — and gate on that captured status, never on whatever ran last in the pipe.

**This is specifically the lander's problem, more than it is anyone else's.** A producer or reviewer
that misreads its own gate hands a bad branch on to the *next* gate in the chain — the code-reviewer,
then `land-review`, then this re-gate — so there's still a chance downstream to catch it. I am the
**last** gate: nothing re-checks what I certify here. A misread green at this step pushes unverified
content straight onto `trunk`, which is the one thing `/land` exists to prevent.

**Consistent with lode-9i2p's rule elsewhere in this file (the `validate-mermaid.sh` exit-2
distinction): a gate that was killed or never completed is neither green nor a content red.** It must
not land anything (the false-"green" case above) and it must not bounce anything either — nothing
here failed on its *content*, the run simply never finished. Treat it like the same kind of machine
fault: stop, re-run the gate cleanly with its real exit status captured, and only then decide
green/red. (`land-review.md` carries no equivalent rule — it explicitly does not re-run gates at all,
so there is no gate there to misread; see the note in section 4 ("What I don't do") of that file.)

### 3a. Order the accepted set — base before dependent; hold an orphaned dependent

An unordered merge set is unsafe for a stacked branch: merging a dependent *before* its base drags
the base's unreviewed content onto `trunk` under the wrong ticket's name, and a dependent whose base
never made it into this pass's accepted set (bounced, escalated, kicked back `needs-rebase`, or
simply not yet at `ready-for-land`) must not land at all this pass — its base isn't on `trunk` yet.

Using [1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)'s **direct
edges**, restricted to `$ACCEPTED`:

- For each accepted `id` stacked on a direct live base `B`:
  - **`B` is also in `$ACCEPTED`** → `id` must merge *after* `B`. A plain topological sort (Kahn's
    algorithm — repeatedly take an accepted id whose bases are all already ordered) handles any depth
    of stacking from these direct edges alone; nothing deeper is needed.
  - **`B` is not in `$ACCEPTED`** → **hold** `id`: pull it out of this pass's merge set entirely (it
    does not merge, conflict-isolate, or bounce this pass), and leave a note so it's visible next
    pass:

    ```bash
    bd update <id> --append-notes "HELD (/land, stacked-branch ordering): land/<id> is stacked on
    land/<B>, which is not landing this pass (<B>'s own outcome: <bounced|escalated|needs-rebase|not
    yet ready-for-land>). Re-evaluated automatically once <B> lands or its own outcome resolves -- no
    action needed unless <B> itself needs a human decision."
    ```

    (No `bd dolt push` needed here in isolation — this note rides along with the pass's other
    publishes.) `id` stays `ready-for-land`; it simply re-enters `land-review` next pass, by which
    point either `B` has landed (so `id`'s own trunk-diff now naturally excludes `B`'s content — see
    [docs/agents-workflow.md](../../../docs/agents-workflow.md#stacked-land-branches-lode-02v)) or the
    hold note explains why it's still waiting.

Once ordered (and any orphaned dependents pulled out), `$ACCEPTED` below refers to this **ordered,
possibly-reduced** set, not the raw land-review output.

**The invariant that outlives this step: a base that leaves the merge set takes its dependents with
it.** Ordering the set up-front is not sufficient, because a base can still drop *out* of it later in
Section 3 — by a real merge conflict (kicked back `needs-rebase`, the loop `continue`s) or by turning
the gate red during isolation (bounced). In both cases the loop would carry straight on to a dependent
that is still in `$ACCEPTED`, merge it, and land the departed base's un-landed, just-rejected content
onto `trunk` under the *dependent's* ticket name — hole 3 again, reached through the back door. So
whenever a branch leaves this pass's merge set **for any reason** — held above, conflict-kicked, or
bounced during isolation — **drop every dependent of it from `$ACCEPTED` too** (the full relation from
[1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd), so transitive
dependents go as well), and leave each one the same HELD note. They are not conflicted and not
rejected; they simply have no foundation this pass, and they re-enter `land-review` next pass exactly
as a held dependent does.

**Pre-compute every merge message before the first merge — no `bd` call inside the merge loop, and
persisted to a FILE, never a bash variable (lode-sfnb).** The `<summary>` in each commit message comes
from `bd show <id> --json` (`metadata.land_summary` / title).
**Reconciled (lode-bns3), and note what is and is not established.** The previous wording here —
"*any* `bd` read regenerates the passive export and leaves it **staged**" — is **not** what happens.
Measured three times now, independently (lode-h1vn's review, lode-bns3's build, lode-bns3's review),
each inside a live agent worktree: a bare `bd show` / `bd ready` (reads) **and** a real `bd update`
(a write) each leave `git status --porcelain` **empty**. bd writes go to Dolt; the tracked
`.beads/issues.jsonl` is regenerated and staged by the **pre-commit hook at commit time**, not at
`bd`-call time. So the per-iteration `bd show` this section hoists out of the loop is **not** what
re-dirties the tree, and no claim here should say it is.

**What has NOT been established is the positive cause.** [bd-sync
discipline](#bd-sync-discipline-non-negotiable) below names `bd dolt pull` as the suspected trigger —
but read it closely: it states that as an explicit *defensive assumption* ("on the assumption it may
be staged even when `git diff` says otherwise"), not as a measurement, and a direct attempt to
reproduce it during lode-bns3's review did **not** stage anything. Do not upgrade that hedge into a
settled fact — swapping one confidently-wrong cause for another is the failure this reconciliation
exists to end, and a wrong causal story about a destructive path is worse than an admitted gap.

**The restore below stays regardless, and its justification does not depend on knowing the cause.**
The staged-jsonl failure is real and observed (a merge refusing with "Your local changes … would be
overwritten"); the export is **by invariant never work** (`import.auto: false`, lode-6ra); so
restoring it unconditionally is free, correct whatever the trigger turns out to be, and precisely the
right move *because* the trigger is unestablished. Hoisting `bd show` out of the loop stays worth
doing on its own merits — one read pass beats N subprocess calls, and it costs nothing to avoid
depending on future `bd` versions behaving as measured today. (The Section 4 GC loop takes none of
this on faith either way — as of lode-bns3 it *excludes* the export from its cleanliness judgment
outright, so it never has to assume anything about what does or doesn't dirty it. It excludes rather
than restores because it only needs to **judge**, whereas this section must actually **clean** the
index or the merge below refuses to run — the same invariant, two different jobs.)

**lode-sfnb: the `declare -A MSG` associative array this block used to populate was cross-block shell
state, and it silently broke.** An agent executing this skill runs each fenced code block as its own,
separate Bash tool invocation — the harness does not carry variables, arrays, or function definitions
between them. `MSG` used to be read by a `merge_one()` function defined in a *later*, separate fenced
block (Section 3's merge loop, ~40 lines of prose below, past the jsonl-restore snippet). By the time
that loop ran in its own invocation, `MSG` no longer existed — `${MSG[$id]}` silently expanded to the
empty string, and `git merge -m ''` either produced an empty-message merge or failed outright with no
output at all. **OBSERVED** landing the 2026-07-26 lode-ns3r/lode-1q2i/lode-sys4 pass: `declare -A MSG`
was re-declared over a variable a prior `source` had already created as an INDEXED array, which bash
refuses to convert (non-zero exit, completely empty stdout/stderr) — and every `MSG[lode-xxx]` lookup
had also silently collapsed to index `0` before that.

**The fix: write each message to a file under `.git/`, and do the actual merge from a script, not an
inline bash function (see [Section 3](#3-batch-merge-the-accepted-set-re-gate-once-isolate-on-red)
below).** A file on disk and a script under `scripts/` are both available identically to *every* Bash
invocation — there is nothing to redeclare, so this can't be silently re-split by a future editor
moving code around the way the bash array was. `git reset --hard` (Section 3's isolation-replay path)
only resets the index and working tree, never anything under `.git/`, so these files survive that
reset intact — which is exactly why `STATE_DIR` lives there and not in the working tree:

```bash
STATE_DIR="$(git rev-parse --git-dir)/land-state"    # under .git/ -- survives a later `git reset
MSG_DIR="$STATE_DIR/msg"                                 # --hard` (that only resets the index+worktree)
CONFLICTS_DIR="$STATE_DIR/conflicts"                     # same mechanism, holding a Section-3
                                                         # conflict's paths for the kick-back block
                                                         # below to read (lode-rfon)
mkdir -p "$MSG_DIR" "$CONFLICTS_DIR"   # $STATE_DIR is wiped once per pass, in Section 1 (lode-wjw4)

# Capture the accepted set to a file HERE, at the one moment I actually hold it (2c's land-review
# verdicts, in the order 3a just established). Every later block RE-READS this file instead of having
# the ids restated by hand: bd ids are opaque identifiers, and docs/conventions.md's "Derive
# identifiers, never retype them" fiat rules out hand-transcribing them -- doubly so here, where the
# ORDER is load-bearing (base before dependent) and a silent slip merges a dependent before its base.
printf '%s\n' $ACCEPTED > "$STATE_DIR/accepted"
: > "$STATE_DIR/landed"    # appended to by the merge loops below; Section 4 reads it back

for id in $(cat "$STATE_DIR/accepted"); do
  SUMMARY=$(bd show "$id" --json | jq -r '.[0].metadata.land_summary // .[0].title')
  printf '%s' "Merge land/$id: $SUMMARY ($id)" > "$MSG_DIR/$id"
done
```

**Re-derive `STATE_DIR`/`MSG_DIR` at the top of every later block that needs them** — Section 3's merge
loop and its isolation-replay copy below both do this. Deriving `$(git rev-parse --git-dir)` fresh
each time is cheap and deterministic, not "state assumed to survive"; what actually persists across
blocks is the **files** on disk, never the shell variables naming their location.

**`$ACCEPTED` and `$LANDED` are files for the same reason** (`$STATE_DIR/accepted`,
`$STATE_DIR/landed`) rather than values restated at each site. `$ACCEPTED` genuinely cannot be
*re-derived* after the fact — it encodes `land-review`'s per-branch judgment, which is not queryable
from git or bd — but **"cannot be re-derived" is not "cannot be persisted"**: the block above captures
it at the one moment I do hold it, in the loop that was already iterating it, exactly as it captures
each merge message. `$LANDED` is better still — the merge loops **append** to it as each branch
actually merges, so it is derived mechanically from what happened rather than recalled.

**Every block below that loads one of these files asserts that it LOADED** — i.e. that
`scripts/land-state-load.sh` itself exited 0 — per the governing rule above. Since lode-dc4n every
**cross-block** load goes through that one script, and the two policies it offers (default = missing
fatal / empty OK; `--require-nonempty` = both fatal) are the *only* two: a new load site picks one by
argument rather than by hand-rolling a fifth `cat` spelling. The `cat "$STATE_DIR/accepted"` in the
block **above** is deliberately left alone and is not a counter-example — it re-reads the file that
same block wrote two lines up, so there is no cross-block hand-off to assert and nothing a load
failure there could mean other than "the write immediately above failed," which its own `printf`
already reports. `for id in $ACCEPTED` over an empty value iterates
**zero** times and exits 0: it merges nothing, closes nothing, and is indistinguishable *by its
behaviour* from a clean pass with an empty queue. What the assertion separates is not that shape from
a real merge, but its two **causes**: a file that was never written (3a never ran — the silent
failure, aborted loudly) from a file that was written empty (every branch legitimately left the set —
allowed through, lode-0jan). Since lode-0jan the guards test only the former; do **not** re-add an
`[ -n "$ACCEPTED" ]`/`[ -z "$ACCEPTED" ]` emptiness test to the first-pass merge loop on the strength
of this paragraph — that conflates the two causes again, which is the whole defect lode-0jan fixed
(pinned by `tests/test_land_conflicts_state.py::test_empty_accepted_falls_through_missing_accepted_still_aborts`).

Before merging anything, unstage the passive jsonl export — unconditionally, without needing to know
what staged it (see the reconciliation above: it is *not* the reads above, and the `bd dolt pull`
suspicion is unverified). `.beads/issues.jsonl` staged means its index blob differs from
`HEAD` while the worktree matches the index — `git diff` / `git diff --quiet` read **clean** in that
state (they compare worktree to index, not index to `HEAD`), so the drift is invisible right up until
`git merge --no-ff` refuses with "Your local changes to the following files would be overwritten by
merge." A bare `git checkout --` does **not** fix this: it only overwrites the worktree, leaving the
staged index entry in place, so a naive retry loops. `git restore --staged --worktree` resets both
index and worktree back to `HEAD` in one shot:

```bash
git restore --staged --worktree .beads/issues.jsonl 2>/dev/null || true   # unstage the passive export;
  # a STAGED jsonl aborts 'git merge' even though 'git diff' reads clean (staged != unstaged) — never
  # let the passive export block or enter a merge (import.auto: false; see bd-sync discipline below)
```

**A failed `git merge` is not automatically a textual conflict.** With the `bd` calls now out of the
loop this should not recur, but if the jsonl gets re-staged mid-loop by anything else, the failure
looks identical to a conflict and must not be classified as one on sight. Classify on the actual
failure: `would be overwritten by merge` in stderr *with* an **empty** `git ls-files -u` (no unmerged
index entries) is the passive-export trap, not a conflict — restore and retry the same merge once.
Only a genuinely unmerged index (`git ls-files -u` non-empty) is a real textual conflict.

**This retry-and-classify logic is now [`scripts/land-merge-one.sh`](../../../scripts/land-merge-one.sh)
(lode-sfnb), not an inline `merge_one()` bash function.** The function used to be defined in this same
fenced block and then called again, unmodified, from the isolation-replay loop in the "Red" branch
below — but that "Red" branch only runs after a `git reset --hard` and a re-gate, each their own
separate Bash invocation, so `merge_one` (and `MSG`, see 3a above) had already vanished by the time
that second call site needed it. A script has no such problem — it exists on disk identically for
both call sites, with no function to redeclare. It reads its message from the `MSG_DIR` files 3a
wrote (never a bash variable), and communicates a real conflict's paths back over **stdout** (capture
with `CONFLICTS=$(...)`) instead of a global `$CONFLICTS` bash variable — see the script's own header
for its full 0/1/2 exit-code contract (0 = merged, 1 = real conflict, 2 = machine fault / missing
message — the same convention `scripts/merge-precheck.sh` and `scripts/validate-mermaid.sh` use,
lode-9i2p).

That script runs a bare `git merge --no-ff` against **cwd**, with no ref or path pinning the target —
but as of **lode-1nty**, [`land-merge-one.sh`](../../../scripts/land-merge-one.sh) asserts its own
main-checkout identity internally, as its first real action, before attempting anything. This block
therefore no longer needs its own `assert-main-checkout.sh` call: the guard now protects this call
site (and the isolation-replay call site below) by construction, not by a caller remembering to fence
it. This reverses lode-pxyt's original choice to guard this fence directly — see
[docs/agents-workflow.md's main-checkout section](../../../docs/agents-workflow.md#mechanics-decided)
for the decision and its reasoning.

```bash
STATE_DIR="$(git rev-parse --git-dir)/land-state"   # re-derive here -- this is a fresh Bash
MSG_DIR="$STATE_DIR/msg"                                # invocation; nothing from 3a's block persists
CONFLICTS_DIR="$STATE_DIR/conflicts"                    # except the FILES 3a wrote under $STATE_DIR
MY_TOKEN="$(cat "$(git rev-parse --git-dir)/land-lock-token" 2>/dev/null || true)"   # lode-q9pm
[ -n "$MY_TOKEN" ] || echo "land: WARNING -- no own-token available; land-lock ownership check is" \
  "DISABLED for this call (lode-67nk)" >&2

# Load 3a's accepted set from disk, and REFUSE to continue if the FILE never got written: that is
# 3a's precompute not having run at all, the silent-failure shape lode-sfnb's governing rule (top)
# exists to catch. An EMPTY file is a different, legitimate outcome (lode-0jan) -- every branch was
# already bounced, escalated, held, or kicked back needs-rebase before this loop started -- and is
# NOT refused: $ACCEPTED being empty makes the loop below iterate zero times, the re-gate after it
# SKIPPED (nothing merged, so there is nothing new to gate -- see that section's own note), and the
# pass falls through to Section 4 exactly as a real merge would.
#
# scripts/land-state-load.sh (lode-dc4n) makes this the "missing fatal, empty OK" policy explicit
# -- one of the two policies every $STATE_DIR load in this skill now shares, instead of a fourth
# hand-rolled spelling. Its own exit status IS the missing-vs-empty discriminator: a missing file, an
# unreadable one, and a directory in its place all fail the read and print a diagnostic to this
# call's stderr, while a present-but-empty OR whitespace-only file reads clean and prints nothing --
# unquoted word-splitting in the `for` below then iterates zero times over either.
ACCEPTED=$(scripts/land-state-load.sh "$STATE_DIR/accepted" -- \
  "3a's precompute did not run. Landing nothing.") || exit 1

for id in $ACCEPTED; do
  # Same idiom as Section 2b's merge-precheck.sh call, for the same reason: a command substitution
  # inside an `if` condition is exempt from `set -e` (unlike a bare `VAR=$(cmd)` assignment, which
  # would abort the shell on a non-zero exit before `rc=$?` is ever reached), and `$?` in the `else`
  # arm is the SCRIPT's real exit status. Do NOT rewrite this as `if ! CMD; then rc=$?`: there `$?`
  # is the *negation's* status, which inside that arm is always 0 -- so a machine-fault 2 would read
  # as a clean merge and the pass would carry on as though the branch had landed.
  if CONFLICTS=$(scripts/land-merge-one.sh "$id" "$MSG_DIR" "$MY_TOKEN"); then
    rc=0
  else
    rc=$?
  fi
  case "$rc" in
    0) echo "$id" >> "$STATE_DIR/landed" ;;   # merged cleanly -- record it and keep going
    2)
      # MACHINE FAULT (missing/empty message file, or an unexpected git failure) -- per lode-9i2p's
      # rule, never read this as a conflict or a bounce. Stop the pass and surface the script's own
      # stderr (already printed to this call's stderr) as a human decision.
      exit 1
      ;;
    *)
      # rc=1: real textual conflict with a branch already merged this pass -- both passed the 2b
      # precheck against origin/trunk but conflict with *each other*. Needs-rebase kick-back (see
      # below, with $CONFLICTS), NOT a land. It never reaches the `0)` arm, so it is never appended
      # to $STATE_DIR/landed and Section 4 cannot close or GC it -- what used to rely on the agent
      # remembering to exclude it from a hand-restated $LANDED is now structural.
      #
      # Persist the conflicting paths now, while this loop actually holds them: the kick-back block
      # that writes the bd note is a SEPARATE Bash invocation and cannot see this loop's $CONFLICTS
      # once it exits (lode-rfon).
      printf '%s\n' "$CONFLICTS" > "$CONFLICTS_DIR/$id"
      #
      # 3a INVARIANT: this branch just LEFT the merge set -- so drop it AND its dependents (1a's full
      # relation, transitively; scripts/blocks-dependents.sh derives the `blocks` edges) and leave
      # each dependent the HELD note. WRITE THAT REDUCTION TO $STATE_DIR/accepted, not just to this
      # shell's $ACCEPTED: the isolation-replay loop below re-reads the FILE, and would otherwise
      # re-merge a branch this pass already kicked back, or merge a dependent whose base is no longer
      # landing -- putting this branch's un-landed content on trunk under the dependent's name.
      # For each dropped id:
      #   grep -vxF "$dropped" "$STATE_DIR/accepted" > "$STATE_DIR/accepted.tmp" || true
      #   mv "$STATE_DIR/accepted.tmp" "$STATE_DIR/accepted"
      # (`|| true` because grep exits 1 when it filters out the last remaining line, and an empty
      # accepted set is a legitimate outcome here -- not a reason to leave the file unchanged.)
      continue
      ;;
  esac
done
```

Re-gate the combined result (this is a Python-gated repo where code changed; a **docs-only** merge
set has no Python gate — skip nox, run `scripts/validate-mermaid.sh` only if a merged diff touched a
`docs/` diagram).

**If the loop above merged NOTHING — `$STATE_DIR/landed` is empty, the all-bounced /
all-`needs-rebase` pass lode-0jan lets through — skip this re-gate entirely and go straight to
[Section 4](#4-land-the-survivors).** Local `trunk` is byte-identical to the `origin/trunk` Section 1
fetched, whose content is by construction already gated (that is the premise every fresh agent
worktree branches from), so there is nothing this pass introduced to certify. This is not a
correctness nicety but a cost one: without it every all-bounced tick pays a full `nox -s tests` run
to re-certify content `trunk` already carries, and any red it found could only be pre-existing
breakage this pass neither caused nor could attribute to a branch. Nothing downstream needs
special-casing: Section 4's reformat commit is already behind its own `git status --short` emptiness
check, which a skipped `nox -t fix` leaves clean.

```bash
. ./venv/bin/activate
nox -t fix && nox -s tests && nox -s lock_currency     # if nox -t fix reformats merged code, commit that as part of the merge result
```

**`nox -s lock_currency` (lode-sys4) catches a stale `requirements.lock` here — locally, before the
public CI badge does.** A branch that bumped a `pyproject.toml` dependency without regenerating the
lock (or whose merge with another accepted branch this pass changed the resolved graph) fails this
with **exit 1**, the same way a red `nox -s tests` would; treat *that* identically — **Red** below
covers it, and the isolation-replay loop re-runs it per branch (see its own `nox -s lock_currency`
call) to find the culprit.

**`nox -s lock_currency` and `validate-mermaid.sh` exit 2 are NOT red gates — they are machine
faults, and isolating on either bounces an innocent branch.** Full contract (what 0/1/2 mean, and
why): [docs/agents-workflow.md — Gate exit-code
contract](../../../docs/agents-workflow.md#gate-exit-code-contract-012-lode-jhry). On either exit 2 I
do **not** isolate, do **not** bounce, and do **not** land: I stop the pass and surface the script's
own message verbatim as a human decision. `lock_currency` is **last** in the `&&` chain above for
exactly this reason — an `&&` chain reports its last-run command's status, so putting anything after
it would mask the 2. Keep it there.

**Neither exit-2 stop in this section restores local `trunk`** — deliberately; that is
[Section 1](#1-setup-the-pass--dolt-authoritative-fetch-origin)'s job, and the reasoning lives there
(lode-k9ef).

- **Green** → proceed to [Land the survivors](#4-land-the-survivors).
- **Red** → **isolate**. The combined merge is bad but I don't yet know which branch. Reset `trunk`
  back to `origin/trunk` and replay the accepted set **one at a time** (in 3a's order), re-gating after
  each; keep every branch that stays green, and **bounce** the first that turns the gate red (→ new
  ticket, drop branch), then continue with the rest — **but if the branch I bounce here is a base, its
  dependents leave the set with it** ([3a's
  invariant](#3a-order-the-accepted-set--base-before-dependent-hold-an-orphaned-dependent)): don't
  replay them, hold them. Replaying a dependent whose base just failed the gate merges the failing
  content back in under a different ticket's name. (The bounce's own [descendant
  check](#bounce--clear-failure) fires here too — a live dependent means this bounce **escalates**
  rather than deleting the branch.)

  This is a fresh Bash invocation — `STATE_DIR`/`MSG_DIR` are re-derived and `$ACCEPTED` re-read from
  disk exactly as the first-pass merge loop above does (lode-sfnb): re-deriving the path is cheap, and
  the **files** under `$STATE_DIR` (not the shell variables naming them) are what actually survived
  `git reset --hard` below, since that only resets the index and working tree, never anything under
  `.git/`.

  Being a fresh invocation is also why this block needs its **own**
  [`assert-main-checkout.sh`](../../../scripts/assert-main-checkout.sh) call as its first line
  (lode-gczf) — Section 1's cannot reach it. The
  [rule and its reasoning live in Section 1](#1-setup-the-pass--dolt-authoritative-fetch-origin) and
  apply here unchanged: keep the guard first, and keep this block's own destructive commands below it
  **in this same fence** — the replay loop's two `git reset --hard HEAD~1` calls are protected only by
  sharing it, since neither is ref/path-addressed. (The `git merge` inside `land-merge-one.sh` is, as
  of **lode-1nty**, ALSO self-guarded internally — see Section 3's first-pass loop above — so this
  fence's guard is now redundant defense-in-depth for that one call, not its sole protection; it stays
  first here regardless, since it is still the sole protection for the two resets.) Splitting this
  block is what would silently un-guard the resets.

  ```bash
  scripts/assert-main-checkout.sh || exit 1   # STOP THE PASS -- everything below assumes this passed
  git reset --hard origin/trunk
  STATE_DIR="$(git rev-parse --git-dir)/land-state"   # re-derive -- see above; 3a's files under
  MSG_DIR="$STATE_DIR/msg"                                 # $STATE_DIR are untouched by the reset
  CONFLICTS_DIR="$STATE_DIR/conflicts"
  MY_TOKEN="$(cat "$(git rev-parse --git-dir)/land-lock-token" 2>/dev/null || true)"   # lode-q9pm
  [ -n "$MY_TOKEN" ] || echo "land: WARNING -- no own-token available; land-lock ownership check" \
    "is DISABLED for this call (lode-67nk)" >&2
  # DELIBERATELY ASYMMETRIC with the first-pass loop above, which lode-0jan taught to let an EMPTY
  # accepted set through: here an empty one is still refused (--require-nonempty). This block only
  # runs on a RED combined re-gate, and a nothing-merged pass now skips that re-gate entirely (see its
  # note above), so an empty set should be unreachable here -- which is exactly why it stays fatal
  # rather than being relaxed for symmetry. If it ever does arrive, `trunk` is byte-identical to
  # `origin/trunk`, so the red is attributable to no branch in this pass: nothing to isolate, nothing
  # to bounce, and a loud stop is the only honest outcome. Do not "finish the job" by deleting this
  # guard -- the two blocks are answering different questions; scripts/land-state-load.sh (lode-dc4n)
  # is what makes that difference a single visible flag instead of two divergent hand-rolled loads.
  ACCEPTED=$(scripts/land-state-load.sh "$STATE_DIR/accepted" --require-nonempty -- \
    "isolation-replay path -- nothing to attribute this red to. Landing nothing.") || exit 1
  : > "$STATE_DIR/landed"    # the reset above discarded every merge the first-pass loop recorded --
                              # start the replay's record from empty so Section 4 closes only what
                              # THIS loop actually keeps merged

  # BASELINE before attributing anything (lode-sys4, extended to cover `nox -s tests` by
  # lode-kq4v). THE RULE, stated generally so a gate added here later inherits it instead of
  # earning its own paragraph: NO gate this loop attributes is a pure function of the tree, so
  # baseline EVERY one of them on bare `origin/trunk` before entering the attribution loop.
  # Otherwise the loop blames — and deletes — whichever innocent branch happened to be merged
  # first. This whole block is on the red/isolate path only, so a green pass never pays for it.
  #
  # `nox -s tests` used to be exempt, on the premise that it "asks a question about the tree
  # alone". That premise licensed a real incident (lode-kq4v, OBSERVED landing a real pass): an
  # ambient `FORCE_COLOR=3` in the LANDING SESSION's own shell — not set anywhere in this repo —
  # fixed rich's colour decision at IMPORT (lode-xgaa's mechanism) and reddened 6
  # `tests/test_cli.py` tests on a bare, unmodified `origin/trunk` with NOTHING merged. Trusting
  # it would have bounced the first branch in `$ACCEPTED`: `bd supersede` closes its ticket, a
  # rebuild ticket carries a FABRICATED "turned the gate red" finding, and `git push origin
  # --delete land/<id>` destroys the reviewed branch — for a variable this repo does not set.
  # `nox -s lock_currency` fails the same test for its own reason: it asks whether the committed
  # lock is a fixed point of the tree PLUS this machine's ambient uv PLUS today's PyPI, so it too
  # can be red with no branch involved at all (a uv release that changes the emitted format, an
  # upstream yank, a lock that went stale on trunk itself).
  nox -s tests
  #   exit 0 → attributable from here on for THIS gate: any later `nox -s tests` red IS caused by
  #            a merged branch. Continue.
  #   nonzero → the suite is red before any branch merged — not attributable to anything in
  #            $ACCEPTED. Stop the pass, land nothing, surface as a human decision — and check the
  #            landing shell's own environment for an ambient `FORCE_COLOR` / `NO_COLOR` /
  #            `TTY_COMPATIBLE` / `TTY_INTERACTIVE` first (lode-kq4v; `tests/conftest.py` now
  #            scrubs these for every pytest invocation it collects for, so a baseline red here
  #            more likely means a genuine regression on `trunk` itself — still not attributable
  #            to any branch in this pass, but worth a closer look before assuming "just env").
  nox -s lock_currency
  #   exit 0 → attributable from here on: any later red IS caused by a merged branch. Continue.
  #   exit 1 → trunk's own lock is stale, before any branch merged. Not attributable to anything in
  #            $ACCEPTED: stop the pass, land nothing, surface as a human decision.
  #   exit 2 → machine fault (see above): stop the pass, land nothing, surface it verbatim.

  # $ACCEPTED was loaded from $STATE_DIR/accepted above -- already reduced by any needs-rebase
  # kick-back the first-pass loop wrote back to that file, so the replay never re-merges one.
  for id in $ACCEPTED; do
    # Identical idiom and identical shape to the first-pass loop above -- see its comment for why
    # `if ! CMD; then rc=$?` is wrong here (that `$?` is the negation's, always 0 in that arm, so a
    # machine-fault 2 would read as a clean merge). Keep the two loops the same shape.
    if CONFLICTS=$(scripts/land-merge-one.sh "$id" "$MSG_DIR" "$MY_TOKEN"); then
      rc=0
    else
      rc=$?
    fi
    case "$rc" in
      0) : ;;   # merged -- now gate it below before recording it as a survivor
      2)
        # MACHINE FAULT — never a branch verdict (lode-9i2p). Stop the pass here; do not bounce, do
        # not isolate further, do not land anything from this pass.
        exit 1
        ;;
      *)
        # rc=1: real textual conflict against an earlier survivor merged this pass: needs-rebase
        # kick-back (see below, with $CONFLICTS), not a bounce — its content wasn't judged bad, it
        # just needs to replay onto the new trunk. Continue with the rest.
        #
        # Persist the conflicting paths, identically to the first-pass loop above and for the same
        # reason -- see its comment (lode-rfon).
        printf '%s\n' "$CONFLICTS" > "$CONFLICTS_DIR/$id"
        continue
        ;;
    esac
    if ! nox -t fix || ! nox -s tests; then
      git reset --hard HEAD~1   # back the culprit out
      # → bounce <id> (Section "Bounce"); it does NOT land this pass
      continue
    fi
    nox -s lock_currency
    case $? in
      0) echo "$id" >> "$STATE_DIR/landed" ;;   # survivor — keep it merged and record it
      2) break ;;                          # machine fault mid-loop, NOT this branch: stop the pass,
                                           # land nothing (skip section 4). Never bounce on a 2.
      *) git reset --hard HEAD~1 ;;    # back the culprit out → bounce <id> (Section "Bounce")
    esac
  done
  ```

  **Every "stop the pass" exit above (the baseline's exit 1/2, and the loop's own break on 2) leaves
  local `trunk` exactly as it sits** — at bare `origin/trunk` for the baseline exits, or carrying
  whichever prefix of `$ACCEPTED` had merged when the loop broke. I restore none of them; that is
  [Section 1](#1-setup-the-pass--dolt-authoritative-fetch-origin)'s job (lode-k9ef).

  Read `$?` from the gate itself — `nox -s lock_currency` yields nox's own 0/1/2. The same lode-b8sr rule as ever applies with
  extra force here: never pipe this gate into `tail`/`grep` and read the *pipeline's* status, which
  would silently flatten a 2 into whatever ran last.

  The survivors stay merged on local `trunk`; the culprit is bounced like any other failure. A branch
  `scripts/land-merge-one.sh` reports as a real conflict (exit 1) here — one that passed the 2b
  precheck against `origin/trunk` but can't cleanly combine with an *earlier survivor* merged this
  pass — is handled as a [needs-rebase kick-back](#needs-rebase--kick-back), not a bounce.

---

## 4. Land the survivors

Only now — combined `trunk` is green — do I write the world. Order matters (see
[bd-sync discipline](#bd-sync-discipline-non-negotiable)): push `trunk` first, then close, then
publish bd state, then GC branches **and the local builder worktrees**.

**Do not hoist this push above
[Section 3](#3-batch-merge-the-accepted-set-re-gate-once-isolate-on-red)'s re-gate.**
`tests/test_land_conflicts_state.py::test_section_3_regate_precedes_section_4_push_origin_trunk` now
catches a document edit that reorders them — see [Section
1](#1-setup-the-pass--dolt-authoritative-fetch-origin)'s note for who relies on the ordering and what
the pin does and does not guarantee (lode-rlz8, lode-youi). The push-before-close order above is a
separate bd-sync concern.

First, check whether the re-gate's `nox -t fix` (above) actually changed anything:

```bash
git status --short
```

- **Empty** → `nox -t fix` touched nothing. Skip the commit entirely — there's nothing to commit.
- **Non-empty** → stage **only** the explicitly-named reformatted source paths shown by that
  `git status`. Never `-A`, and never rely on a `':!.beads'` pathspec exclude to keep the passive
  jsonl export out (`-A` once swept in an unrelated pre-existing untracked directory under a
  misleading `style:` message, hitting landing `lode-0wj.1`); the exclude cannot help anyway — beads' own
  pre-commit hook (`.beads/hooks/pre-commit`) re-exports and re-stages `.beads/issues.jsonl` on
  *every* commit regardless of what was `git add`-ed (see CLAUDE.md's workflow gotchas), so the
  commit itself must skip hooks too:

  **This commit names no ref or path at all — the one `git` write in this section that doesn't.**
  Every other one below is ref- or path-addressed and therefore cwd-independent (`git push origin
  trunk`, `git push origin --delete land/<id>`, `git worktree unlock/remove --force/prune`, `git
  branch -D` — each names its own target; the `bd` calls are cwd-independent too, but for an
  unrelated reason: `bd` resolves the repo's canonical `.beads` rather than cwd's). This one commits
  directly to whatever branch cwd's `HEAD` happens to be on, and run from the wrong directory that is
  not a loud failure: it silently commits the reformat to that directory's branch, and the
  `git push origin trunk` below then pushes local `trunk` *without* it — green all the way through
  (lode-pxyt). Fresh Bash invocation, so it needs its own
  [`assert-main-checkout.sh`](../../../scripts/assert-main-checkout.sh) call as this fence's first
  line ([rule and reasoning in Section 1](#1-setup-the-pass--dolt-authoritative-fetch-origin)):

  ```bash
  scripts/assert-main-checkout.sh || exit 1                          # STOP -- this commit is not ref-addressed at all; see above (lode-pxyt)
  git add <path> <path> ...                                          # explicit reformatted source paths only, e.g. git add src/foo.py src/bar.py
  git commit --no-verify -q -m "style: nox -t fix on merged trunk"   # --no-verify: skip the beads pre-commit hook so it can't re-stage .beads/issues.jsonl
  git show --stat HEAD                                               # confirm only the intended paths rode along — no jsonl, nothing else
  ```

```bash
git push origin trunk
git status                 # MUST show trunk up to date with origin

# Heartbeat once more here, closing gap (c) (lode-v4sv) -- positioned so every per-ticket `bd close`,
# `epic-completion-check.sh`, the networked `scripts/bd-dolt-push.sh`, every per-ticket branch delete,
# and the worktree-GC sweep below all sit strictly BETWEEN this call and the pass-end `release` near
# the end of this same block, rather than after the LAST heartbeat Section 3's own merge helper (see
# above) fired during its loop. That old gap was the worst of the three CAVEAT 1 named: the ordinary
# GREEN path, growing with the number of tickets landed, during the exact stretch `trunk` is being
# written. Same caveat as gap (a)'s own new call site: one interval, not a per-ticket heartbeat, so it
# does not itself bound the per-ticket loops' own growth -- it just isolates this stretch as the one
# remaining unheartbeated span, cleanly separated from Section 3's own re-gate + merge loops (already
# heartbeated via that same helper). Same best-effort contract as every other call site here.
MY_TOKEN="$(cat "$(git rev-parse --git-dir)/land-lock-token" 2>/dev/null || true)"   # lode-q9pm
[ -n "$MY_TOKEN" ] || echo "land: WARNING -- no own-token available; land-lock ownership check is" \
  "DISABLED for this call (lode-67nk) -- land-lock.sh REFUSES it outright (exit 2, lode-yuwt)" \
  "rather than re-stamping blind, so this heartbeat simply does not fire (|| true below)" >&2
scripts/land-lock.sh heartbeat "$MY_TOKEN" || true

# $LANDED: the ids that actually stayed merged through Section 3 -- read back from the file Section 3's
# merge loops appended to as each branch merged (lode-sfnb), never restated by hand. On the Green path
# that is $ACCEPTED minus any mid-loop needs-rebase kick-backs; on the Red/isolation path the replay
# loop truncated the file and re-recorded only the branches it kept merged, so bounced culprits and
# held dependents are already excluded. An EMPTY file is legitimate (every branch kicked back or
# bounced) and correctly closes nothing; a MISSING one means Section 3 never ran -- abort loudly, same
# policy and same shared script (scripts/land-state-load.sh, lode-dc4n) as the first-pass accepted
# load above -- this used to be a bare `cat ... || exit 1` with no diagnostic at all, the one
# $STATE_DIR load in the skill that dropped lode-0jan's loud/silent distinction one section later.
STATE_DIR="$(git rev-parse --git-dir)/land-state"   # re-derive -- fresh Bash invocation again
LANDED=$(scripts/land-state-load.sh "$STATE_DIR/landed" -- \
  "Section 3 never ran (or never reached its end-of-loop write). Nothing to close.") || exit 1
for id in $LANDED; do
  bd close "$id" --reason "Landed on trunk via /land (merge <sha>)"
  bd update "$id" --remove-label ready-for-land   # tidy the queue label off the (now closed) ticket --
    # symmetric with the needs-rebase/escalate/bounce exits, which have always done this; the success
    # path was the one exit that forgot (lode-myh6). Keep this AFTER the close: a crash between the two
    # leaves only the old, benign stale label, whereas stripping FIRST would strand an open, label-less
    # ticket outside the queue for good -- the label, not the status, is the queue (Section 1), which is
    # also why this closes the reopen hazard: with the label gone there is nothing left to re-admit.
done

# Closing the last child of an epic completes it — flag it for the closing-side review.
# I only NOTICE completion here (I am the one that closed it); the review itself is the
# separate `/epic-audit` skill. For each just-closed ticket, `scripts/epic-completion-check.sh`
# walks to its parent epic and decides whether that epic is now fully child-complete and not
# already flagged/audited (lode-v4rk: the derivation THIS inline snippet used to run —
# `bd show <epic-id> --json | jq '.dependents[]'` — was dead code, since `bd show` only
# populates `.dependents` when called with the opt-in `--include-dependents` flag; without it
# `$kids` was always `[]`, and the `(($kids|length)>0)` false-positive guard silently ate every
# pass. The script is extracted, not inlined, so it carries its own fixture-backed regression
# tests — tests/test_epic_completion_check.py, including a test that reproduces the OLD bug and
# proves it — since no gate here would ever catch a markdown-embedded jq snippet regressing).
# The script is read-only; this loop is the one place that actually writes the label.
for id in $LANDED; do
  RESULT=$(scripts/epic-completion-check.sh "$id")
  [ -z "$RESULT" ] && continue
  PARENT=$(printf '%s' "$RESULT" | awk '{print $2}')
  bd label add "$PARENT" epic-ready-to-audit   # /epic-audit picks it up
done

scripts/bd-dolt-push.sh               # publish the closes, epic-ready-to-audit labels, and any bounce tickets over refs/dolt/data — durable, cross-machine

for id in $LANDED; do
  git push origin --delete "land/$id"   # GC the merged remote branch — a bare ref delete, not a
                                             # worktree/uncommitted-work risk, so this stays per-ticket
                                             # regardless of the local worktree-GC decision below.
done

# Local worktree + branch GC is NOT done per-ticket (lode-h1vn). There used to be a loop here that read
# metadata.review_worktree/review_branch off each just-landed ticket and ran `git worktree remove
# --force` unconditionally — no `locked` check, no dirty-tree check. It is DELETED; the backstop sweep
# below is now the only local worktree/branch reclaim, and it catches every just-landed builder worktree
# on the same pass (this pass's `--no-ff` merge is what makes each one's HEAD an ancestor of trunk, a
# few lines above). Discovering worktrees live from `git worktree list --porcelain` also beats trusting
# per-ticket metadata that can drift.
#
# WHAT THIS COSTS, because "the backstop subsumes it" is true of the CANDIDATE set but NOT the RECLAIMED
# set: the backstop gates on `locked` + clean-tree + HEAD-ancestry, none of which the old loop had, so it
# reclaims strictly LESS. A landed builder worktree that is DIRTY, LOCKED, or carries commits that never
# reached origin is now KEPT where the old loop force-removed it — the dirty case being a PERMANENT leak
# (it stays dirty, so every later pass skips it too; a human must clear it). That is deliberate and is
# the trade lode-9hgu already made: leak a directory rather than destroy uncommitted work. Measured to be
# rare — real post-build/post-review worktrees read clean.
#
# ONE UNENFORCED COUPLING keeps this loop reclaiming anything at all; if it breaks it silently
# reclaims NOTHING, and since the per-ticket loop is gone there is no second net and no alarm:
#   1. `.gitignore` (lode-9hgu) — a finished worktree is full of untracked build junk (`venv/`, `.nox/`,
#      `__pycache__/`); it reads clean ONLY because those are ignored. Un-ignore one and every worktree
#      reads dirty.
# (CLOSED, lode-bns3) bd export churn used to be a SECOND unenforced coupling — this comment used to
#   say the gate ASSUMES a `bd` write never dirties the tree, contradicted by Section 3 (~line 354,
#   now reconciled) asserting the opposite for its own merge path. Measured three times (lode-h1vn's
#   review, lode-bns3's build, lode-bns3's review): neither a bare `bd` read nor a `bd` write dirties a
#   worktree by itself. The positive cause of the staged export is NOT established — `bd dolt pull` is
#   the suspected trigger, but bd-sync discipline below states that as a defensive assumption, not a
#   measurement, and it did not reproduce when tried (see Section 3's reconciled note; do not restate
#   the suspicion as fact). None of that matters to this loop, which no longer needs the premise
#   settled in EITHER direction — the whole point is that the gate is correct whatever the trigger
#   turns out to be, since the export is by invariant never work. It now EXCLUDES
#   `.beads/issues.jsonl` / `.beads/interactions.jsonl` from the cleanliness judgment outright, via
#   `:(exclude)` pathspecs on the dirty-tree guard (in `scripts/worktree-gc-classify.sh` since
#   lode-9owc — see the WHERE THE PREDICATES LIVE note below) — so a staged or modified export, from
#   whatever cause, present or future, can never zero out this sweep on its own.
# If you touch `.gitignore`, re-check that this loop still reclaims.
#
# Full record — the three options, the measurement, why deletion beat guarding: docs/decisions.md,
# lode-h1vn entry.

# WHERE THE PREDICATES LIVE, READ THIS FIRST (lode-9owc): every predicate the comments from here to
# the loop describe — both ancestry arms, the lode-9hgu dirty-tree guard and its `:(exclude)` list,
# and the lode-yrtu dir-only age floor — now lives in `scripts/worktree-gc-classify.sh`, NOT in the
# fence below. So "the dirty-tree guard below", "the loop below also tests", "either predicate" and
# friends in this block mean "in the classifier the loop below calls." Only the CODE moved, and it
# moved so it could be shellcheck'd and unit-tested (tests/test_worktree_gc_classify.py) instead of
# living unreachable by any gate in a markdown fence. The prose split that leaves: this block holds
# the SWEEP-level contract (which worktrees are candidates at all, and what this sweep promises
# about them); the script's header holds the per-arm detail. Change a predicate there and this block
# is what tells you whether you were allowed to.
#
# Backstop: now the ONLY local worktree/branch reclaim in this pass — catches every just-landed
# builder worktree (per the reasoning above) plus whatever it always caught: a stale/missing
# review_worktree pointer, a build that never got GC'd on its own machine, a reviewer/rebase-pickup
# worktree from a multi-cycle review that no ticket's single review_worktree field can point at
# (lode-r78 — the reviewer and a rebase pickup each check `land/<id>` out into their OWN fresh
# worktree per lode-k5e/lode-8k3, so a ticket reviewed more than once leaves extra land/<id>-branched
# worktrees a per-ticket net could never see anyway). Walks the raw porcelain blocks directly, so a worktree with no matching
# ticket, or a ticket with stale/wrong metadata, still gets reclaimed.
#
# NOTE (lode-vs7g): `/code`'s own orchestrating session now reclaims a reviewer's or rebase-pickup's
# launch worktree proactively, right after that subagent returns (either outcome — ready-for-land or
# land-escalated), deriving it from the ticket id via the `land/<id>--<worktree-dir>` branch name — see
# `.claude/skills/code/SKILL.md` and docs/decisions.md's lode-vs7g entry. This backstop is UNCHANGED
# and stays exactly as it was, but it is a PARTIAL net, not a total one: it only ever reclaims a
# worktree whose branch is already merged into trunk, so it cannot cover an escalated ticket (whose
# branch never merges) — that case is closed by /code's reclaim, not here. Expect this to fire far less
# often now: mostly for a /code session that died mid-fan-out, before it could reclaim.
#
# ONE loop covers BOTH branch-attached and DETACHED worktrees (lode-jiyk unifies what were formerly
# two separate WORKTREE sweeps here: a branch-NAME-keyed one, lode-r78, and a later HEAD-sha-keyed
# one, lode-mxeu, added because the name-keyed one structurally cannot see a detached worktree).
# Both tested the literally identical predicate — "this worktree's tip is already merged
# into trunk" — by two different routes: a branch-name lookup against a `git branch --merged trunk`
# list (branch NAMES, so it can only ever match a worktree that HAS a branch), or a direct
# `git merge-base --is-ancestor <HEAD-sha> trunk` (needs no branch name at all). The SHA form is
# strictly more general — it subsumes the branch-attached case too — so ONE loop, keyed on HEAD-sha
# ancestry rather than a branch-name pattern, replaces both nets: this sweep no longer cares whether
# a candidate worktree's branch (if any) is named `worktree-agent-*`, `land/*`, or something else
# entirely — every worktree under `.claude/worktrees/` is a candidate, so a new worktree-BRANCH-naming
# convention cannot leak past THIS loop.
#
# That name-independence is scoped to worktrees and does NOT extend to the bare-ref backstops below:
# the second and third still enumerate `refs/heads/land/*` and `refs/heads/worktree-agent-*` by name,
# because `refs/heads/*` is shared with human branches and a name-blind "delete any merged local ref"
# would eat them. A new BARE-REF namespace can therefore still leak exactly as lode-j5i0's did — so
# if you add one, audit those two, not this one. (lode-j5i0's sweep is the THIRD backstop below; it is
# alive and untouched — it was never one of the two unified here.)
#
# CONTRACT (lode-9hgu closed the zero-divergence residual this paragraph used to describe; lode-amif
# widened the ancestry predicate itself, and the dirty-tree guard below gates BOTH arms): ANY
# worktree under `.claude/worktrees/` that is unlocked, AND EITHER has not diverged from `trunk` OR —
# for a branch-attached worktree — has not diverged from its own branch's origin counterpart, AND is
# clean is reclaimable by this loop, whoever made it (lode-amif: the ancestry predicate widened from
# "merged into trunk" alone to "captured on origin," so an escalated ticket's reviewer/rebase-pickup
# worktree, whose branch never merges into trunk by definition, is reclaimable too, once its content
# is safely on `origin/land/<id>`). A worktree freshly branched off `trunk` HEAD (or freshly checked
# out at its origin branch's current tip) is trivially "merged"/"captured" by zero divergence — that
# proxy alone would read TRUE for a live, uncommitted build/review the instant its worktree is created
# — so the loop below also tests the ACTUAL invariant directly ("is this work captured anywhere
# else"): `git -C "$WT" status --porcelain`. A dirty tree is never reclaimed, regardless of lock
# state, ancestry, or who made the worktree — this is what actually protects the worktree classes that
# hold NO lock by the time this sweep sees them: an interactive `EnterWorktree` session, a human's
# hand-made worktree (CLAUDE.md mandates one for all work), and an exited agent's leftover scratch.
# There are exactly TWO lock sources in this system, and neither covers those three:
#   1. The Claude Code HARNESS locks every `isolation: worktree` launch worktree for the LIFETIME of
#      the agent standing in it (reason: `claude agent <name> (pid <n> start <n>)`), released when
#      the agent exits. Neither `.claude/agents/code-reviewer.md` nor `coding`'s rebase pickup calls
#      `git worktree lock` itself, but both run in such a worktree — so a LIVE reviewer/pickup
#      worktree is `locked` and this loop skips it outright, never reaching the predicate. Verified
#      against a running reviewer (lode-amif's own).
#   2. `.claude/agents/coding.md` ALSO locks its producer build worktree explicitly (lode-oqr),
#      because it unlocks again at its first commit — earlier than the harness would.
# So the zero-divergence residual bites only an EXITED agent's worktree, and only its UNCOMMITTED
# scratch — and even there, the dirty-tree guard below is the real backstop, not `locked`: `git -C
# "$WT" status --porcelain` prints nothing both when the tree is CLEAN and when the command itself
# errors (missing dir, corrupt worktree admin, …), so the guard distinguishes "clean" (proceed) from
# "could not tell" (skip) rather than treating empty output as always meaning clean. On the trunk arm
# an unguarded zero-divergence read was once a real hazard — it destroyed two builds' uncommitted work
# outright before the dirty guard existed (lode-oqr's explicit lock closed the narrower pre-first-
# commit window; lode-9hgu's dirty guard closed the rest). On the origin arm the same guard is what
# keeps a captured-but-dirty reviewer/rebase-pickup worktree from being reclaimed too: content merely
# pushed to `origin/land/<id>` is captured, but a worktree with additional uncommitted changes on top
# of that push is not, and must still be KEPT — otherwise this widened arm reopens exactly the hole
# lode-9hgu just closed. Accepted residual: a CLEAN worktree at zero divergence (trunk arm) or clean at
# its origin counterpart's tip (origin arm) that raises no lock — a human's hand-made worktree they
# happen to be sitting in, or an exited agent's clean leftovers — is still reclaimable; nothing is
# destroyed (the tree is clean), the directory just vanishes out from under whoever is standing in it.
# A LIVE harness agent's worktree is NOT in that set — its harness lock (above) drops it in the
# `locked` check below, before either predicate is ever evaluated. The trade is intentional: the
# failure direction is now "remove an empty checkout," never "destroy uncommitted work," on either arm.
#
# Skip anything `locked` — that's the git-native in-use signal, and it's load-bearing here: a
# currently-running sibling worktree whose branch hasn't diverged from trunk yet is trivially
# "merged" into trunk by content identity, so `locked` must gate this even though `merged` alone
# looks sufficient. `merged` is the same safety invariant that justified the old per-ticket loop's own
# unconditional `--force` ("the build artifact is on trunk now — force is safe") before that loop was
# deleted in favor of this backstop owning all local worktree/branch reclaim (lode-h1vn) — for a
# **builder's own** `worktree-agent-*` worktree specifically, `merged` is what proves the ticket already
# landed: its branch is never pushed anywhere, so the origin arm added by lode-amif is always false for
# it. WHAT `merged` NO LONGER IMPLIES (lode-yrtu): an in-flight
# `ready-for-code-review`/`ready-for-land`/`land-escalated` ticket's builder worktree used to be
# excluded outright, both ancestry arms being false for it. It is now reachable by the DIR-ONLY arm
# below once its last commit is older than $MIN_AGE_SECONDS and its tree is clean — its DIRECTORY is
# reclaimed while its branch REF is deliberately KEPT, so no commit is ever lost, but do not read
# `merged` as a promise that a builder worktree's directory survives. A `land/<id>`-branched
# **reviewer/rebase-pickup** worktree is different since lode-amif: once its branch is pushed to
# `origin/land/<id>`, the origin arm can make it reclaimable even though its branch has not (and, if
# escalated, never will) merge into trunk — that is the gap lode-amif exists to close. This `locked`
# check used to be a no-op in practice:
# nothing on the producer side ever raised it, so every producer build was "merged" (trivially, by
# zero divergence) and reclaimable from the moment its worktree was created until its first commit --
# this destroyed two builds' uncommitted work outright (branch and all, not just the checkout) before
# the gap was understood (lode-oqr). `.claude/agents/coding.md` now locks the worktree as the
# producer's first action and unlocks it right after its first commit, closing that window; this
# loop's `locked` filter needed no change. `locked` is still checked first (cheapest, and the
# git-native in-use signal), but as of lode-9hgu it is no longer the ONLY thing standing between a
# live, uncommitted tree and `--force` removal — the dirty-tree guard below is the backstop for every
# worktree class that never raises `locked` at all (see the CONTRACT paragraph above). The one
# accepted trade-off specific to `locked`: a crash strictly between lock and first commit leaves a
# locked worktree this sweep won't auto-reclaim -- rare (a normal build commits within minutes) and
# resolved by a manual `git worktree unlock` (or a future cleanup ticket), not by this loop, since
# correctness (never destroy a live build) matters more here than eagerness.
#
# Scoped to paths under .claude/worktrees/ so this can never touch the main checkout (its tip is
# always merged into itself, so the predicate alone wouldn't exclude it — the path guard is what
# does, and it costs nothing). If the worktree has a branch, delete it too (`git branch -D`); a
# detached worktree has none, so worktree removal alone is the entire reclaim.
# lode-bns3 (observability): count each candidate into exactly one bucket — reclaimed, or skipped for
# locked / not-merged / dirty — so the summary line after the loop can tell "reclaimed 0 of 0, nothing
# to do" apart from "reclaimed 0 of N, everything was skipped" (a regression that zeroes out GC must be
# visible here, not silent). `locked` is counted in the loop body now rather than filtered inside awk,
# so every candidate under .claude/worktrees/ reaches the summary, whichever bucket it lands in.
#
# FIELD ORDER IS LOAD-BEARING — DO NOT REORDER (`$BR` must stay LAST). Tab is IFS *whitespace*, so
# `read` collapses adjacent tabs into a single delimiter and does NOT preserve an empty MIDDLE field.
# `branch` is the one field that can be empty (a DETACHED worktree — a case this loop explicitly
# supports, see the `git branch -D` note above). With `branch` in the middle, a detached worktree's
# line (`path\thead\t\tlocked`) shifts every later field left: `$BR` swallows the locked flag and
# `$LOCKED` reads EMPTY, so `[ "$LOCKED" = "1" ]` is FALSE and a LOCKED, LIVE agent's worktree sails
# past the locked gate into the `--force` below — precisely the "rip a worktree out from under a
# running agent" harm the gate exists to prevent (the pre-lode-oqr disaster). Keeping `branch` last
# makes its empty case a TRAILING delimiter, which `read` discards harmlessly ($BR="").
RECLAIMED=0; RECLAIMED_DIR_ONLY=0; SKIP_LOCKED=0; SKIP_NOTMERGED=0; SKIP_DIRTY=0; FAILED=0
STALE_LOCKS_FOUND=0
# lode-yrtu: minimum age (seconds) of a NOT-MERGED worktree-agent-*'s last commit before its
# DIRECTORY (never its branch ref) becomes eligible for the dir-only reclaim below -- see
# docs/agents-workflow.md's "Worktree-GC widened" section for the tunable + default (documented
# there, not configuration.md, per that page's own scope note: dev-tooling for the landing loop,
# not an application knob).
MIN_AGE_SECONDS="${LAND_WORKTREE_DIRONLY_MIN_AGE_SECONDS:-21600}"
# lode-9owc: the per-candidate DECISION is scripts/worktree-gc-classify.sh (see WHERE THE PREDICATES
# LIVE above), extracted the same way lode-ivth extracted scripts/recycled-worktree-guard.sh and
# lode-yrtu extracted scripts/worktree-lock-stale.sh out of this same loop. What stays HERE is
# exactly what a side-effect-free script cannot own: reading the porcelain candidates, resolving a
# STALE lock (a real mutation), and the two DESTRUCTIVE calls the script only ever recommends.
while IFS=$'\t' read -r WT SHA LOCKED BR; do
  if [ "$LOCKED" = "1" ]; then
    # lode-yrtu: the lock recorded here is PER-SESSION, not per-agent -- measured: several worktrees
    # can share ONE lock-owner pid (the parent session process), so a DEAD session leaves every
    # worktree it ever locked stuck at this check forever, since `locked` is tested before any other
    # predicate. scripts/worktree-lock-stale.sh proves the recorded pid is either not running at all,
    # or has been REUSED by an unrelated later process (via /proc/<pid>/stat's own starttime, matched
    # against the token the harness recorded at lock time) -- see the script's header for the full
    # mechanism and why a plain PID-liveness probe (signal 0) alone cannot safely make this call. A
    # lock the script cannot positively prove dead is left alone (fail closed): $LOCKED stays "1" and
    # classify below reports keep-locked, same as before.
    LOCK_REASON=$(git worktree list --porcelain | awk -v want="$WT" '
      /^worktree / { path=$2; reason="" }
      /^locked/    { reason=substr($0,8) }
      /^$/         { if (path==want) { print reason; exit }; path="" }
    ')
    if scripts/worktree-lock-stale.sh "$LOCK_REASON"; then
      STALE_LOCKS_FOUND=$((STALE_LOCKS_FOUND + 1))
      # `git worktree remove` refuses a still-locked worktree even with `--force` -- that flag
      # overrides "has modifications," never "is locked" (verified: dry-run against a fabricated
      # repo, lode-yrtu). Proving the SESSION is dead is not the same as clearing git's own on-disk
      # lock, so unlock it now, unconditionally, then reflect the resolution in $LOCKED so classify
      # below judges this candidate as unlocked rather than short-circuiting to keep-locked.
      git worktree unlock "$WT" 2>/dev/null || true
      LOCKED=0
    fi
  fi
  # scripts/worktree-gc-classify.sh is the single source of truth for the bucket -- see its own
  # header for what each name means and why. It takes no action; the case below performs the two
  # destructive calls the script only ever recommends, and counts each candidate into exactly one
  # bucket (lode-bns3: reading `git worktree remove`'s own exit status, not merely the fact that we
  # attempted it, so the summary line can never report "reclaimed N" when every remove FAILED).
  BUCKET=$(scripts/worktree-gc-classify.sh "$WT" "$SHA" "$LOCKED" "$BR" "$MIN_AGE_SECONDS")
  case "$BUCKET" in
    keep-locked)    SKIP_LOCKED=$((SKIP_LOCKED + 1)) ;;
    keep-notmerged) SKIP_NOTMERGED=$((SKIP_NOTMERGED + 1)) ;;
    keep-dirty)     SKIP_DIRTY=$((SKIP_DIRTY + 1)) ;;
    dir-only)
      if git worktree remove --force "$WT"; then
        RECLAIMED_DIR_ONLY=$((RECLAIMED_DIR_ONLY + 1))   # ref intentionally KEPT -- no `git branch -D`
      else
        FAILED=$((FAILED + 1))
      fi
      ;;
    full-reclaim)
      if git worktree remove --force "$WT"; then
        [ -n "$BR" ] && git branch -D "$BR" 2>/dev/null || true
        RECLAIMED=$((RECLAIMED + 1))
      else
        FAILED=$((FAILED + 1))    # git printed its own error; surface it in the summary too
      fi
      ;;
    *)
      # Never observed -- a defensive net against a future classify bug printing something outside
      # its own documented bucket set. Fails CLOSED (counted as failed, worktree left untouched)
      # rather than silently falling through either reclaim arm.
      echo "worktree GC: unexpected classify output '$BUCKET' for $WT -- treating as failed" >&2
      FAILED=$((FAILED + 1))
      ;;
  esac
done < <(git worktree list --porcelain | awk '
  /^worktree / { path=$2; head=""; branch=""; locked=0 }
  /^HEAD / { head=$2 }
  /^branch refs\/heads\// { branch=substr($0,19) }
  /^locked/ { locked=1 }
  /^$/ { if (path!="" && path ~ /\/\.claude\/worktrees\//) print path"\t"head"\t"locked"\t"branch; path="" }
')
git worktree prune          # drop any now-stale worktree admin entries
# lode-bns3 (observability): always emit one line. "reclaimed 0 of 0" (nothing to do) reads differently
# from "reclaimed 0 of N" (everything was skipped — worth investigating), and the reason breakdown
# makes a regression that silently zeroes out GC visible here instead of indistinguishable from idle.
TOTAL=$((RECLAIMED + RECLAIMED_DIR_ONLY + SKIP_LOCKED + SKIP_NOTMERGED + SKIP_DIRTY + FAILED))
echo "worktree GC: reclaimed $((RECLAIMED + RECLAIMED_DIR_ONLY)) of $TOTAL candidate(s) under .claude/worktrees/ (full=$RECLAIMED, dir-only=$RECLAIMED_DIR_ONLY, stale-locks-treated-as-unlocked=$STALE_LOCKS_FOUND; skipped: locked=$SKIP_LOCKED, not-merged=$SKIP_NOTMERGED, dirty=$SKIP_DIRTY; failed=$FAILED)"

# Second backstop: dangling local land/<id> refs with no worktree attached at all (so the
# worktree-GC loop above never even considered them) and no remote counterpart left (lode-r78). That
# loop only runs `git branch -D` when it also found a matching
# worktree; a local land/<id> branch that already lost its worktree by some other path (or
# never had one materialize beyond the fetch+checkout in coding.md's rebase pickup /
# code-reviewer.md) is invisible to it. "Remote gone" is sufficient signal on its own: an
# in-flight ticket's origin/land/<id> always exists (the producer pushed it in build step 8
# and nothing deletes it until /land lands, bounces, or drops the ticket), so a missing
# remote means this local ref is already stale. No extra locked/merged check is needed here
# — `git branch -D` itself refuses harmlessly if the branch is still checked out in some
# worktree, which is exactly the case the loop above would have just finished reclaiming.
# List origin's land refs ONCE (a single round-trip, not one `ls-remote` per local ref — a
# machine can accumulate dozens of stale land refs) and only sweep if that listing SUCCEEDED:
# an unreachable origin makes `ls-remote` exit non-zero, and reading that as "every remote
# land branch is gone" would force-delete every local land ref on a transient network blip.
# A failed listing therefore skips the sweep; an empty-but-successful one correctly means
# every local land ref is stale (grep against the empty set matches nothing → all deleted).
# STRIP THE WORKTREE SUFFIX BEFORE COMPARING (lode-em6v): since lode-em6v the reviewer and
# the rebase pickup check the branch out under `land/<id>--<their-own-worktree-dir>`, never
# the bare `land/<id>`, and that suffixed name can NEVER byte-match origin's `land/<id>`.
# Comparing it raw would make the "remote still exists — keep" arm dead code for every ref
# this sweep now sees, silently demoting the backstop to "delete every land/* ref not
# currently checked out" and force-deleting an in-flight ticket's ref (its unpushed commits
# with it) the moment its worktree goes away by any route. `${BR%%--*}` maps the local name
# back to the remote one it corresponds to (`land/x--agent-ab12` → `land/x`) and leaves a
# bare `land/x` untouched, so the ORIGINAL "remote gone ⇒ stale" semantics hold for both
# shapes. Safe because a bd id never contains `--` (ids are `lode-<slug>`, single hyphens),
# so the first `--` is always the worktree-suffix delimiter.
if REMOTE_LAND=$(git ls-remote --heads origin 'land/*' 2>/dev/null); then
  REMOTE_LAND=$(printf '%s\n' "$REMOTE_LAND" | sed 's#^.*refs/heads/##')
  # lode-yrtu (lode-bns3 treatment): report only deletions that ACTUALLY happened, reading `git
  # branch -D`'s own exit status rather than announcing one "before the fact" behind `|| true`.
  # OBSERVED live (bd show lode-yrtu): this backstop once printed "deleting stale local ref
  # land/lode-rlyx--agent-aad6b30a923856fb7" while the ref still existed afterward -- `git branch -D`
  # had refused it (still checked out in a locked worktree) and the trailing `|| true` swallowed that
  # failure silently. Same class of bug lode-bns3 already fixed for the WORKTREE loop above (counting
  # an attempt rather than the remove's real exit status). Process substitution (`< <(...)`), not a
  # pipe, so these counters survive past the loop instead of dying in a subshell.
  B2_DELETED=0; B2_FAILED=0
  while read -r BR; do
    printf '%s\n' "$REMOTE_LAND" | grep -qxF "${BR%%--*}" && continue   # remote still exists — keep
    if git branch -D "$BR" 2>/dev/null; then
      B2_DELETED=$((B2_DELETED + 1))
    else
      B2_FAILED=$((B2_FAILED + 1))    # refused (e.g. still checked out somewhere) -- report it, don't claim success
    fi
  done < <(git for-each-ref --format='%(refname:short)' 'refs/heads/land/*')
  echo "bare-ref backstop2 (land/*): deleted $B2_DELETED stale local ref(s) (failed=$B2_FAILED)"
fi

# Third backstop: dangling local worktree-agent-* refs with no worktree attached at all
# (lode-j5i0 — the same bug as lode-r78, but the OTHER ref namespace: the second backstop
# above only ever swept refs/heads/land/*, so a worktree-agent-* ref orphaned by any route other
# than the worktree sweep above — which can only ever reclaim a ref that still HAS a worktree to
# remove — was swept by nothing and accumulated without bound — 17 confirmed on the landing
# machine). This namespace needs a
# DIFFERENT guard than land/*: a worktree-agent-* branch is never pushed to origin, so it
# has no remote counterpart, ever — "remote gone" is meaningless here and would delete a
# LIVE, still-building branch (every producer branch would read as "remote gone" the
# instant it's created). The correct guard is the same PREDICATE the worktree sweep above
# applies — merged into `trunk` (the work is safely captured elsewhere) — but reached by a
# branch-NAME lookup rather than that loop's HEAD-sha ancestry test, because a bare ref has no
# worktree and therefore no porcelain `HEAD ` line to test; plus not currently checked out in
# any worktree. $MERGED is computed right HERE, immediately above its ONLY consumer (the worktree
# sweep keys on HEAD-sha ancestry and needs no branch name, so nothing else reads it). Computing it
# after the sweeps above is safe: `trunk` has not moved, and every ref they deleted is gone from
# `for-each-ref` too, so the set is identical for every ref still listed. `git branch -D` itself also
# refuses harmlessly if the branch is still checked out somewhere, but that alone is not the guard
# being relied on — the explicit merged check is what keeps an in-flight, not-yet-merged build ref
# from ever being a candidate in the first place.
MERGED=$(git branch --merged trunk --format='%(refname:short)')
CHECKED_OUT=$(git worktree list --porcelain | awk '/^branch refs\/heads\//{print substr($0,19)}')
# lode-yrtu (lode-bns3 treatment): same fix as backstop 2 just above -- report only what `git branch
# -D` actually did, and use process substitution rather than a pipe so these counters survive.
B3_DELETED=0; B3_FAILED=0
while read -r BR; do
  printf '%s\n' "$CHECKED_OUT" | grep -qxF "$BR" && continue   # still checked out somewhere — keep
  printf '%s\n' "$MERGED" | grep -qxF "$BR" || continue        # not merged into trunk — keep (in-flight)
  if git branch -D "$BR" 2>/dev/null; then
    B3_DELETED=$((B3_DELETED + 1))
  else
    B3_FAILED=$((B3_FAILED + 1))
  fi
done < <(git for-each-ref --format='%(refname:short)' 'refs/heads/worktree-agent-*')
echo "bare-ref backstop3 (worktree-agent-*): deleted $B3_DELETED stale local ref(s) (failed=$B3_FAILED)"

MY_TOKEN="$(cat "$(git rev-parse --git-dir)/land-lock-token" 2>/dev/null || true)"   # lode-q9pm
[ -n "$MY_TOKEN" ] || echo "land: WARNING -- no own-token available; land-lock ownership check is" \
  "DISABLED for this call (lode-67nk) -- land-lock.sh REFUSES it outright (exit 2, lode-yuwt)" \
  "rather than releasing blind, so the lock stays held until the staleness window reclaims it" >&2
scripts/land-lock.sh release "$MY_TOKEN"   # the pass is fully done -- release now rather than
                                     # waiting out the staleness window (lode-aps3; see Section 0)
```

`bd close` unblocks dependents — that is *why* the lander closes (the producer never does): a closed
ticket frees the next layer of `bd ready`. Closing is mine because the merge decision is mine.

The worktree GC is **best-effort and machine-local**, and (since **lode-h1vn**) entirely the
end-of-pass backstop sweep's job — there is no separate per-ticket removal step any more. **Nothing in
`/land` reads `review_worktree`/`review_branch`**, and as of **lode-2m89** nothing writes them either:
the deleted loop was their only GC consumer, the backstop discovers worktrees directly off
`git worktree list --porcelain`, and `/code`'s own reclaim *derives* its target from the ticket id
rather than trusting a recorded path (lode-vs7g) — so the fields were pure dead weight and `coding.md`
stopped recording them (see lode-2m89's `docs/decisions.md` entry). Discovering worktrees live instead
of trusting recorded paths is strictly better anyway: there is no bookkeeping to drift. Builds can
happen on several machines, and a worktree on another machine simply isn't in this machine's
`git worktree list`, so it's invisible to this sweep and that other machine's
own `/land` (or a later sweep there) reclaims it. A worktree that is `merged`+`unlocked`+clean is
reclaimed **fully** (directory *and* branch ref), which for a just-landed ticket's builder worktree is
true precisely *because* this pass just `--no-ff` merged it into trunk a few lines above. **Since
lode-yrtu, failing the ancestry gate no longer means the tree is kept.** On a **bounce** the branch is
dropped but the rebuild ticket may still want the tree, and on an **escalate** the work is held for a
human — in both cases the *builder's* worktree HEAD never merges into `trunk`, and its
`worktree-agent-*` branch is never pushed, so neither ancestry arm ever becomes true. Its **branch
ref** is what survives those outcomes now, not its directory: once its last commit ages past
`LAND_WORKTREE_DIRONLY_MIN_AGE_SECONDS` (default 6h) and its tree is clean, the dir-only arm reclaims
the directory and keeps the ref, so every commit stays reachable but the checkout itself does not
persist indefinitely. A **dirty** builder worktree is still never touched, in any bucket (lode-9hgu).
(Scope all of that to the **builder's** worktree deliberately: a reviewer's or rebase-pickup's *own*
launch worktree is a different thing — `/code` reclaims that one proactively on an escalation,
lode-vs7g, and **lode-amif** widens this loop's predicate to reclaim it via *origin*-ancestry precisely
*because* an escalated branch never merges.) This backstop sweep
is now the **only** net over the same machine's worktrees: it doesn't consult any ticket's metadata, so
it reclaims **any** worktree under `.claude/worktrees/` — branch-attached (`worktree-agent-*`,
`land/<id>--<worktree-dir>`, or any other name) or **detached** alike — regardless of whether any
ticket ever pointed at it (no ticket does, since lode-2m89). lode-jiyk unified what were originally
two separate **worktree** sweeps here: an early one keyed on branch **name** (`lode-r78`), and a
later one keyed directly on
**HEAD-sha ancestry** (`lode-mxeu`) added because a detached worktree has no branch name for the first
sweep to match. Both tested the identical predicate — "this worktree's tip is already merged into
trunk" — so now there is one loop: it requires the worktree to be **unlocked** (no in-flight agent owns
it — or, since lode-yrtu, holding a lock whose recorded owner process is *provably* dead, which
`scripts/worktree-lock-stale.sh` must positively prove before the lock is cleared) and its **HEAD
commit** an ancestor of `trunk` (`git merge-base --is-ancestor <HEAD-sha> trunk` — the work is safely
captured elsewhere). The **FULL** reclaim (directory *and* branch ref) therefore still needs no
branch-name pattern to keep in sync as new worktree-branch-naming conventions are added. lode-yrtu
adds the loop's one deliberate exception to that: the **dir-only** arm does key on a branch NAME
(`worktree-agent-*`), because what it is really testing — "no agent will ever want this exact
checkout again, and its ref will survive to hold the commits" — has no ticket-metadata-free signal
other than the branch shape, this sweep consulting no ticket metadata by design. So a NEW
worktree-branch-naming convention leaks past the dir-only arm (it simply won't be reclaimed) but
never past the full one. That name-independence is otherwise **scoped to this loop**: the
bare-ref backstops below still enumerate `land/*` and `worktree-agent-*` by name (they must —
`refs/heads/*` is shared with human branches), so a new *bare-ref* namespace can still leak exactly as
`lode-j5i0`'s did. (`lode-j5i0`'s own sweep is the **third** backstop below — alive and untouched; it
was never one of the two unified here.) "Unlocked" is a real signal now, not a formality: a producer
(`.claude/agents/coding.md`) locks its worktree the instant it starts building and unlocks it right
after its first commit, so this sweep only ever finds a worktree unlocked once its build has either not
started or already diverged from `trunk` — never mid-build with uncommitted, unreclaimed-elsewhere work
sitting in it (lode-oqr). Dropping the branch-name filter makes the sweep's **contract** explicit where
it used to be incidental: *any* worktree under `.claude/worktrees/` that is unlocked and has not
diverged from `trunk` is reclaimable, whoever created it — and a tree freshly branched off `trunk` HEAD
is trivially "merged" by zero divergence, so uncommitted work in one is **not** protected by the merged
check. At the time this loop was written, `locked` was the only thing holding it off — and a hand-made
worktree raises no lock (both lock sources are agent-side: see the CONTRACT paragraph above), so one
under `.claude/worktrees/` had to be committed or locked by hand to survive. This residual is
**pre-existing, not new** (both unified sweeps already had it: the old name-keyed one matched
`worktree-agent-*`, which is exactly what an interactive session gets, and the old detached one was
already name-blind); dropping the name filter just makes it impossible to overlook. **lode-9hgu** has
since closed the *destructive* half of this (see below): it **added** a real guard on the actual
invariant (is the tree dirty?) *alongside* the ancestry checks — which still run and still decide
candidacy — rather than relying solely on the zero-divergence-vulnerable "merged" proxy. So today a
**dirty** hand-made worktree survives even unlocked and merged-by-zero-divergence; a **clean** one at
zero divergence is still reclaimable, which is the accepted residual the CONTRACT paragraph above
records. A branch-attached candidate here is typically the reviewer's or a rebase
pickup's *own* launch worktree, per the lode-k5e/lode-8k3 architecture (they `git fetch origin
land/<id>` and check it out into a locally **uniquely-named** branch — `land/<id>--<their-own-worktree-dir>`
since **lode-em6v**, plain `land/<id>` before it. Before lode-em6v this reused the bare `land/<id>` name,
which git permits in only one worktree at a time, so whenever `land/<id>` was already checked out
elsewhere (a leaked worktree from an earlier cycle, since neither agent ever removed its own launch
worktree) the agent fell back to `git checkout --detach FETCH_HEAD`, leaving a **detached**, branchless
worktree — and the leak was **self-compounding**: a stale worktree from one cycle was precisely what
forced the next cycle onto the detaching path. **lode-em6v closed this at the source** by making the
local name unique by construction, so the collision — and the detach fallback it forced — can no longer
arise in **normal operation**; this backstop keeps catching a detached worktree regardless, as a
crash-safety net for a killed process, not because the steady-state leak it was originally built to
catch still occurs) — a ticket reviewed across more than one cycle leaves *extra* such worktrees
(branch-attached or detached) that no single `review_worktree` field can name, so this backstop is the
only net that ever reclaims them (lode-r78, lode-mxeu); `merged`+`unlocked` excludes an in-flight one
regardless of whether it has a branch. If the worktree has a branch, this backstop deletes it too
(`git branch -D`); a detached worktree has none, so worktree removal alone is the entire reclaim. A
**separate** pass right after the worktree sweep (see the script above) deletes any local `land/<id>`
**branch ref** whose `origin/land/<id>` counterpart no longer exists — the worktree sweep above only
deletes a local branch when it also found an attached worktree, so a bare ref with no worktree (e.g.
`git worktree remove`d by some other path) would otherwise linger forever once its remote is gone. That
pass is the one place the **lode-em6v** renaming *does* reach: it keys on an **exact** name match
against origin's listing, which a suffixed `land/<id>--<worktree-dir>` can never satisfy, so it strips
the suffix (`${BR%%--*}`) before comparing — the comment above the sweep has the full reasoning and why
skipping the strip would silently turn this backstop into a ref shredder, force-deleting an in-flight
ticket's ref (and its unpushed commits) the moment its worktree goes away by any route. A **third** pass
sweeps the mirror-image gap in the *other* namespace (lode-j5i0): a bare `worktree-agent-*` ref with no
worktree attached at all is invisible to both nets above (the first only matches refs that still have a
worktree; the second only matches `land/*`), so it was swept by nothing and accumulated without bound —
17 confirmed orphans on the landing machine, all already merged. Unlike `land/<id>`, a `worktree-agent-*`
branch is never pushed to origin, so "remote gone" can't be the guard here (it would fire on every
branch, live or not); the guard is `merged`-into-`trunk` plus not currently checked out anywhere — the
same safety *predicate* the worktree sweep applies, reached by a branch-**name** lookup (`git branch
--merged trunk`) rather than that sweep's HEAD-sha ancestry test, since a bare ref has no worktree and
so no HEAD sha to test.

**Update (lode-amif): the worktree sweep's predicate widened from "merged into trunk" alone to
"merged into trunk OR captured on origin."** Everything above describes the sweep as it stood after
lode-jiyk: `unlocked` + `HEAD-sha is-ancestor-of trunk`. That predicate structurally cannot reclaim an
**escalated** ticket's reviewer/rebase-pickup worktree — an escalated branch is, by definition, held
for a human decision and never merges into `trunk`, so if the `/code` session that would otherwise
eagerly reclaim it (lode-vs7g) itself dies mid-fan-out before running that reclaim, the worktree leaks
**indefinitely**: the trunk-ancestry test is never satisfied, so backstop 1 never even considers it a
candidate. The gap is one level up from what lode-vs7g closed (lode-vs7g handles the normal-exit case,
including a clean escalation return; this is the crash-before-that-point case).

The fix widens the loop's predicate with a second arm: `git merge-base --is-ancestor "$SHA" trunk ||
{ [ -n "$BR" ] && git merge-base --is-ancestor "$SHA" "origin/${BR%%--*}"; } || continue` (see the
script above and its inline comment). The real safety invariant was never "merged into trunk" — that
was always a stand-in for "this worktree's content already exists safely elsewhere." A
reviewer/rebase-pickup worktree satisfies that invariant by construction the moment it has pushed to
`origin/land/<id>` (lode-k5e/lode-8k3), on **either** outcome, `ready-for-land` or `land-escalated` — so
testing ancestry against that origin ref directly reaches exactly the cases the trunk-only test missed.
`${BR%%--*}` strips the lode-em6v worktree-uniqueness suffix the same way backstop 2 already does, so
the new arm resolves to `origin/land/<id>` regardless of which locally-suffixed name the branch was
checked out under. A detached worktree (no `$BR`) and a builder's own `worktree-agent-*` worktree (never
pushed to origin, so its origin counterpart doesn't exist and the ancestor test simply fails) are
unaffected — the new arm is false for both, so they fall through to the unchanged `trunk`-only
behavior. The new arm carries the *same* zero-divergence residual the trunk arm already has (see the
CONTRACT paragraph above and lode-9hgu) rather than introducing a new one — and on this arm it is
**benign**. A freshly-checked-out reviewer/pickup worktree is trivially "captured on origin"
(identical to origin's current tip) until its first local commit, but for that entire window it is
`locked`: the harness locks every `isolation: worktree` launch worktree for the lifetime of the agent
standing in it, so the sweep's existing `locked` filter drops a **live** reviewer/pickup worktree
before the predicate is ever evaluated. What the arm can reach is an **exited** agent's worktree,
which at zero divergence holds only uncommitted, ungated scratch from a run that never finished —
authoritative content is on `origin/land/<id>` and the ticket is re-reviewed from there. That is
precisely the worktree this widening exists to reclaim. lode-9hgu has since landed (6591ba9). It did
not *remove* the ancestry proxies — both arms still run, and still decide candidacy — it added a real
dirty-tree guard (`git -C "$WT" status --porcelain`) immediately below them, gating **both**: a worktree
captured on `trunk` or on `origin` but left dirty is now KEPT, not reclaimed. See the CONTRACT paragraph
above and the guard itself in the loop.

---

## Needs rebase — kick back

A **needs-rebase** is the outcome of the [2b precheck](#2b-cheap-conflict-precheck--does-it-still-merge-onto-trunk)
(or a Section-3 textual conflict): the branch **can't merge onto current `trunk`**, but its content
was never judged bad — I never ran `land-review` on it. It is a **third outcome, distinct from bounce
and escalate**: not a rebuild (nothing is wrong with the work), not a human decision (there's nothing
to decide — it just needs to replay onto where `trunk` moved). So I keep everything and hand it
straight back to the producer.

**This block is its own, separate Bash invocation from whichever producer detected the conflict
(2b's `merge-precheck.sh` call, or one of Section 3's two merge loops) — none of that block's shell
state, including `$CONFLICTS`, survives to here (lode-rfon, the same defect class as lode-sfnb's
`$MSG`/`$ACCEPTED`/`$LANDED`).** Read the conflicting paths back from the file the producer wrote
under `$STATE_DIR/conflicts/<id>` instead, and refuse — loudly — rather than kick back with a blank
paths section if that file is missing or empty:

```bash
STATE_DIR="$(git rev-parse --git-dir)/land-state"     # re-derive -- fresh Bash invocation; the
                                                       # FILE under $STATE_DIR is what survived,
                                                       # never a bash variable
# scripts/land-state-load.sh --require-nonempty (lode-dc4n) -- same "missing or empty, both fatal"
# policy as the isolation-replay accepted load above, and the same script; this used to be its own
# fourth hand-rolled spelling (`cat ... 2>/dev/null` + a separate `[ -n ... ]` check).
CONFLICTS=$(scripts/land-state-load.sh "$STATE_DIR/conflicts/<id>" --require-nonempty -- \
  "the producer site (2b's merge-precheck.sh call, or a Section-3 merge loop) did not persist the" \
  "conflicting paths. Refusing to kick back with a blank paths section.") || exit 1

bd update <id> --remove-label ready-for-land --add-label needs-rebase \
  --append-notes "NEEDS REBASE (/land): origin/land/<id> no longer merges cleanly onto trunk @ $(git rev-parse --short origin/trunk).
Conflicting paths:
$CONFLICTS
/code's step-0 pickup merges current trunk into land/<id>, re-gates, commits, and pushes the result
itself (an ordinary, non-force push), then swaps needs-rebase back to ready-for-land (lode-cln)."
scripts/bd-dolt-push.sh       # publish the label swap + note over refs/dolt/data
# The branch is KEPT (no delete). The build worktree is KEPT. No supersede, no new ticket, no close.
```

Unlike a bounce, I **do not** supersede, create a rebuild ticket, or drop the branch, and unlike an
escalate there's no question for a human — the producer just replays its own already-reviewed work
onto the new `trunk`. The ticket stays `in_progress`; the `needs-rebase` label (not `ready-for-land`)
is now its state. **`/code` picks this up automatically** (lode-wfl): every invocation sweeps
`bd list --label needs-rebase --status in_progress` first and dispatches a `coding` producer to
merge current `trunk` into `land/<id>`, re-gate, commit, and push the result itself — an ordinary,
non-force push, since the merge only appends and never rewrites what's already on `land/<id>` — then
swap the label straight back to `ready-for-land` itself (lode-cln; full mechanics in
[`docs/agents-workflow.md`](../../../docs/agents-workflow.md#the-step-0-pickup-merges-it-never-rebases-lode-cln))
— no human nudge needed unless the merge itself conflicts and the two sides genuinely disagree (that
escalates, `land-escalated`, same as any other genuine decision).

## Bounce — clear failure

A **bounce** is a confident "this branch should not land as-is" (an unmet acceptance criterion,
silent scope creep, a violated invariant, a wrong approach — or drift/conflict from Sections 2a/3).
The original ticket is **superseded** by a fresh ticket that carries the `land-review` findings, so a
producer can rebuild from a clean brief.

**Before doing anything else: check for live descendants (lode-02v).** A bounce **deletes**
`land/<id>`, and a prior pass did that with no idea another live branch had already merged `land/<id>`
in. Deleting the branch does not delete its commits from the dependent: the dependent went on carrying
the rejected content — including the very defect the bounce was rejecting — on a foundation that no
longer existed and would never land (OBSERVED: lode-6qh / lode-96t). So: **read the descendants of
`<id>` straight out of [1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)'s
map** (the *full* relation — a transitive dependent is stranded exactly as badly as a direct one). The
map is already built for this pass; do **not** re-derive it here with an ad-hoc `--contains` probe
against `<id>`'s tip — that is the tip test 1a exists to avoid.

- **No descendants** → proceed with the bounce exactly as below.
- **A live `land/<dep>` descendant found** → I do **not** silently drop `land/<id>`. I escalate
  instead — the lander does not decide this; the escalate exit already exists for exactly this shape
  of question:

  ```bash
  bd update <id> --remove-label ready-for-land --add-label land-escalated \
    --append-notes "ESCALATION (/land bounce, lode-02v): land-review bounced this branch (findings
  below), but land/<dep> is a LIVE branch that already merged land/<id>'s commits — deleting land/<id>
  now would silently strand land/<dep>, which would carry the very defect this bounce is rejecting.
  Needs a human decision: FOLD (supersede both <id> and <dep> into one combined rebuild ticket — see
  the disposition rule below for which branch, if either, is worth keeping to lift from), SEQUENCE
  (rebuild <id> alone; <dep> stays blocked/parked until the rebuild lands, then rebases normally onto
  it), or DROP (neither is wanted anymore — close both).

  LAND-REVIEW FINDINGS: <the bounce findings, verbatim>"
  scripts/bd-dolt-push.sh
  # BOTH land/<id> and land/<dep> are KEPT (no delete) until the human resolves it.
  ```

  This is a superset of an ordinary escalate: it names the specific dependent and the specific
  question (fold/sequence/drop), so the human resolving it (see [Resolving a
  `land-escalated` branch](#resolving-a-land-escalated-branch)) has everything needed without
  re-deriving the stack relationship. `<dep>` itself is left exactly as it is — still whatever label
  it currently carries (it may not even be at `ready-for-land` yet) — this bounce escalation doesn't
  touch it.

**No descendants — the ordinary bounce.** First derive the blocks-dependent set with its exit status
tested, THEN create the rebuild ticket, THEN mark the original superseded with **`bd supersede`** (the
dedicated command — `supersedes` is **not** a `--deps` type):

```bash
# Derive blocks-dependents FIRST, before anything else changes state (lode-xm1h). Capture the
# output so the exit status is testable -- a bare `for DEP in $(...)` discards it. If OTHER tickets
# depend on <id> via a `blocks` edge (e.g. a diagnosis spike that gates its follow-ups), supersede
# below CLOSES <id> -- so bd treats that blocker as satisfied and those dependents unblock
# PREMATURELY, while the real work still sits unbuilt in the rebuild. Re-pointing each dependent
# onto the rebuild (below) is what keeps that from happening; if this derivation itself can't be
# trusted, nothing downstream can be either, so escalate instead of guessing.
#
# Extracted to scripts/blocks-dependents.sh (lode-verb), unlike the inline jq this replaced: that
# snippet was correct but ungated, and unlike the epic-completion checks lode-v4rk extracted (which
# fail silently SAFE on a schema/flag regression), a dropped re-point here fails silently UNSAFE --
# the dependent unblocks immediately against a rebuild that was never built. The script carries its
# own fixture-backed regression tests (tests/test_blocks_dependents.py) so a DERIVATION regression
# (e.g. the required --include-dependents flag getting dropped) fails a gate instead of failing
# silently. But a derivation regression isn't the only way this goes wrong (lode-xm1h): a bd
# RUNTIME failure (bd missing, Dolt DB locked, an id bd can't resolve) makes the script itself exit
# non-zero, which no gate on the script's internals can catch -- only the caller reading its exit
# status can. That is what this `if !` does.
if ! DEPS=$(scripts/blocks-dependents.sh <id>); then
  # Do NOT proceed blind to the supersede: continuing here would close <id> while never having
  # confirmed its blocks-dependents (if any), which unblocks them immediately against a rebuild
  # that doesn't exist yet -- the exact unsafe outcome lode-verb extracted this script to prevent.
  # Nothing has been created or changed yet at this point (no $NEW, no re-parent, no supersede), so
  # escalating here is a clean stop, not a partial one.
  bd update <id> --add-label land-escalated --remove-label ready-for-land \
    --append-notes "ESCALATION (bounce): scripts/blocks-dependents.sh <id> failed at runtime (bd
missing, Dolt DB locked, or an id it couldn't resolve) while deriving blocks-dependents ahead of a
supersede. Bounce does not proceed blind -- superseding without a reliable dependent list risks
re-pointing nothing while blocks-dependents silently unblock against an unbuilt rebuild. No rebuild
ticket was created; land/<id> is kept. Retry the bounce once the underlying bd failure clears."
  scripts/bd-dolt-push.sh
  # STOP here -- and stop with a STATEMENT, not a comment. A bare `# STOP` is INERT: control
  # would fall through the `fi` straight into `NEW=$(bd create ...)` / `bd supersede` (which
  # CLOSES <id>) / `git push --delete` below, superseding the ticket anyway -- the exact
  # "proceed blind to the supersede" outcome this guard exists to prevent (lode-xm1h). The
  # `exit 1` is what actually enforces the stop; do not drop it back to a comment. (Nothing has
  # been created or changed beyond the land-escalated label above, so this exit is a clean stop.)
  exit 1
fi

# UNGATED, unlike the derivation above: this `if !`/escalate structure lives in this markdown file,
# not in scripts/blocks-dependents.sh, so no automated test covers a regression to it (e.g. someone
# "simplifying" this back to a bare `for DEP in $(...)`) the way test_blocks_dependents.py covers
# the script's own derivation logic (lode-verb's own comment states the identical limitation for the
# jq-vs-script split; this is the same limitation, one level up, for lode-xm1h's fix). A future
# editor changing this block should either add a check that reads it directly, or preserve the
# `if !` shape as-is.

NEW=$(bd create --type=<same-type-as-original> \
  --title="<original title> (rebuild after land bounce)" \
  --description="Rebuild of <id>, bounced by /land semantic review.

REBUILD BRIEF (from land-review):
<the findings + what the rebuild must satisfy that the bounced branch did not>" \
  --json | jq -r '.id')

# Preserve epic parentage. If the bounced ticket was an epic's child, re-parent the rebuild onto
# the same epic BEFORE superseding — otherwise supersede closes the child and the epic loses it,
# reading falsely "complete" while the real work sits in an unlinked ticket. Re-parenting keeps the
# epic's completion accounting honest: the superseded child closes, but NEW is an open child, so the
# epic stays incomplete until the rebuild lands (and /epic-audit sees the real work).
# `.parent` (verified against real bd 1.1.0 output, lode-v4rk's audit) is the direct top-level
# field bd show already populates for a parent-child child -- simpler and no schema/flag pitfall to
# get wrong, unlike the blocks-dependent derivation below, which needs the opt-in
# --include-dependents walk and so lives in scripts/blocks-dependents.sh (no
# `.dependencies[]?`/`.dependents[]?` walk survives inline in this file -- lode-v4rk's audit
# extracted every other one; confirmed by grep).
PARENT=$(bd show <id> --json | jq -r '.[0].parent // empty')
[ -n "$PARENT" ] && bd dep add "$NEW" "$PARENT" --type=parent-child   # NEW becomes a child of the epic

# Re-point the non-parent dependents derived above ($DEPS, captured before $NEW existed).
for DEP in $DEPS; do
  bd dep add "$DEP" "$NEW"   # DEP now depends on the rebuild, not the superseded original
done

bd supersede <id> --with "$NEW"   # links <id> -> NEW and AUTO-CLOSES <id> as superseded
bd update <id> --remove-label ready-for-land   # tidy the queue label off the (now closed) original

git push origin --delete "land/<id>"    # drop the rejected branch (a rebuild gets a fresh land/<new-id>)
scripts/bd-dolt-push.sh                            # publish the new ticket + supersede over refs/dolt/data
```

`bd supersede` **closes** the original (with a reference to `NEW`) — superseded means *replaced*, and
`NEW` is the live work. That is the right outcome for a bounce: the bounced attempt is done-as-replaced,
not lingering open. (It is the one case where landing-side closes an `in_progress` producer ticket; a
normal **accept**/land closes via Section 4, an **escalate** never closes.)

**This same escalation shape applies to the [Escalate → land-escalated](#escalate--genuine-decision)
path's own bounce-like rebuild** below: it defers to "see Bounce above for why" rather than
duplicating the loop, so it inherits this fix by reference — a runtime failure there is handled
exactly as above, not a separate case to design.

### Branch disposition on a bounce — drop (default) vs. keep-for-lift (lode-02v)

Every bounce above deletes `land/<id>`. That's right whenever the finding is that the *content
itself* is what's wrong — nothing there is worth carrying forward. But it is not the only disposition
this skill has actually used: the lode-6qh/lode-96t resolution **kept** lode-96t's branch
(undocumented, until now) so the combined rebuild (lode-og3) could lift its error-handling
implementation verbatim rather than re-deriving it, because land-review had already judged that
content **sound on its own** — the branch just couldn't land *because of* something external to it
(its foundation, lode-6qh, was bounced out from under it).

**The rule:**

- **DROP (default)** — the bounce finding is about the branch's *own* content: an unmet acceptance
  criterion, a wrong approach, a violated invariant, silent scope creep. Nothing there survives review
  unchanged, so nothing is worth keeping. This is every ordinary bounce above.
- **KEEP-FOR-LIFT** — reserved for the fold resolution of a [strand escalation](#bounce--clear-failure)
  above: the *dependent's* branch (not the bounced base's) is kept when land-review — or the human
  resolving the escalation — judges its content independently sound, and the combined rebuild ticket
  says explicitly **"lift verbatim from `land/<dep>` @ `<sha>`"** rather than re-describing the same
  design from scratch (mirrors lode-og3's own FOLD-IN note:
  `git show <sha>:<path>` / `git diff <a>..<b>` pointers, not prose re-derivation). The **base's**
  branch (the one actually bounced) is still dropped — it was rejected for a reason, and folding
  doesn't rescue it.

**A kept branch is not GC'd for free — say so in the rebuild ticket.** Section 4's GC deletes
`land/$id` for each id in `$LANDED`, and a kept `land/<dep>` belongs to a ticket that was *superseded*,
not landed: it will never appear in `$LANDED`, so nothing deletes it automatically. The rebuild ticket
must therefore carry the disposal instruction alongside the lift pointers — **"lift verbatim from
`land/<dep>` @ `<sha>`; delete `land/<dep>` once this ticket lands"** — which is exactly what lode-og3's
FOLD-IN note does. Until then the branch is inert for landing (its ticket is closed, so it never
re-enters the queue) but it *does* stay live in `refs/remotes/origin/land/*` and therefore visible to
[1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd) — which is correct and
intended: a kept branch really does still contain its base's commits, and 1a enumerating **refs, not
tickets** is what keeps that visible.

## Escalate — genuine decision

An **escalate** is a real question only a human can answer (the ticket is ambiguous about "done";
acceptance is arguably met depending on an unrecorded decision; the branch took a
defensible-but-different approach; the ticket/branch is unidentifiable). I land **nothing** for it,
**keep its branch** for the human to pick up, mark it, and surface the question — **without blocking
the rest of the batch** (the accepted set still merges):

```bash
bd update <id> --add-label land-escalated --remove-label ready-for-land \
  --append-notes "ESCALATION (/land semantic review): <the decision needed, with options as land-review framed them>"
scripts/bd-dolt-push.sh
# origin/land/<id> is KEPT (no delete) until the human resolves it.
```

(A **stale-escalation sweep** — **surfacing**, not GC'ing, a `land-escalated` branch that has sat
unresolved unusually long — is a deferred refinement in `docs/decisions.md`, not part of v1. `/sweep`
already surfaces every open `land-escalated` item every pass regardless of age; a `land-escalated`
branch is never touched by an automated sweep — only the human-driven resolution exits below
remove the label and let the branch go.)

## Resolving a `land-escalated` branch

`land-escalated` is **not terminal** — a human resolves it, and every resolution **removes the
label**, so `bd list --label land-escalated` can reach empty. Resolution is a human action taken
outside a `/land` pass — typically at `bd show <id>` time; `/land` only ever *sets* the label
(above), never clears it. There are exactly four exits:

### (a) Land as-is — materialize the decision first, then re-enter the queue

A `/land` escalation means `land-review` hit a genuine ambiguity it couldn't settle on its own (an
arguable "done", an unclear acceptance criterion). If the human decides the branch **should** land,
**the branch itself needs no change** — this exit is exactly the "it's fine as-is" case. What changes
is the *ticket*: swapping the label back to `ready-for-land` with nothing else touched is **not a
complete transition**, because `/land`'s next pass re-dispatches `land-review`, which hits the *same*
ambiguity and escalates again — an infinite escalate↔ready loop.

So the swap is valid only once the human has **written the decision into the ticket** — edit the
acceptance criteria / description so the ambiguity `land-review` flagged no longer exists — *then*
swap the label. `land-review` stays authoritative on re-review; there is deliberately **no
"human-blessed" bypass label** that skips it (forcing a land past a `land-review` objection is an
out-of-band manual act, not a designed fast-path):

```bash
bd update <id> --acceptance="<revised, unambiguous acceptance criteria>"   # land-review reads
  # acceptance_criteria as the contract — this is the field that must change; add --description too
  # if the narrative text also needs updating. The BRANCH is untouched.
# If resolving this escalation required a COMMIT to land/<id> — which it does whenever the decision
# belongs in docs/ rather than a bd field — refresh land_head, or Section 2a reads the commit you
# just made as DRIFT and prescribes a bounce next pass (OBSERVED: lode-y3dw, 2026-08-05). This exit
# is the exposed one because it re-enters at ready-for-land with no reviewer in between; exits (b)
# and (d) route through a code-reviewer, which refreshes land_head itself.
# --set-metadata (upsert), NOT --metadata (which takes a whole JSON blob and would drop
# land_summary/review_head — verified 2026-08-05).
bd update <id> --set-metadata land_head="$(git rev-parse origin/land/<id>)"   # omit if nothing was committed
bd update <id> --remove-label land-escalated --add-label ready-for-land
scripts/bd-dolt-push.sh
# /land's NEXT pass re-runs land-review against the now-unambiguous ticket — same gate, no bypass.
```

### (b) Rebuild — supersede into a fresh ticket, drop the branch

If the human decides the branch must be rebuilt (the escalated approach was wrong, or the real
answer is "this needs a different design"), resolve it exactly like a `land-review`
[bounce](#bounce--clear-failure): open a new ticket carrying the decision, `bd supersede` the
original onto it, and drop the rejected branch (same epic re-parent / dependent re-point care as a
bounce applies here too).

**Before dropping the branch, run the same descendant check as Bounce (lode-02v):** a `land-escalated`
branch can have picked up a live stacked dependent while it sat waiting for a human, exactly like an
ordinary bounce candidate can — and a *stale* graph from the pass that escalated it is no use here, so
recompute [1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)'s relation
against the live refs and look `land/<id>` up in it. If it finds a live descendant, do **not** delete —
apply the same [fold/sequence/drop framing](#bounce--clear-failure) instead of proceeding blind, and
use the [keep-for-lift disposition](#branch-disposition-on-a-bounce--drop-default-vs-keep-for-lift-lode-02v)
rule if folding. No descendant found → proceed exactly as below.

```bash
NEW=$(bd create --type=<same-type-as-original> \
  --title="<original title> (rebuild after land-escalated)" \
  --description="Rebuild of <id>. Human resolution of the land-escalated decision:
<the decision + what the rebuild must satisfy that the escalated branch did not>" \
  --json | jq -r '.id')
# re-parent onto the same epic / re-point blocking dependents — see Bounce above for why.
bd supersede <id> --with "$NEW"          # closes <id> as superseded, links to $NEW
bd update <id> --remove-label land-escalated
git push origin --delete "land/<id>"     # drop the escalated branch — the rebuild gets a fresh land/<new-id>
scripts/bd-dolt-push.sh
```

### (c) Drop — close with reason, GC the branch

If the human decides the work simply shouldn't happen (overtaken by events, no longer wanted),
close the ticket directly and GC the branch.

**Same descendant check before deleting (lode-02v)** — a dropped branch's commits are just as live
inside a dependent as a bounced branch's would be; recompute [1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)'s
relation against the live refs before the delete below. A live descendant found means dropping this branch also
strands that dependent's foundation — surface that to the human as part of this same decision (does
the dependent get dropped too, or does it need to be re-founded on something else?) rather than
deleting silently.

```bash
bd close <id> --reason "<why this is dropped>"
bd update <id> --remove-label land-escalated
git push origin --delete "land/<id>"     # GC the branch — nothing will land it
scripts/bd-dolt-push.sh
```

### (d) Amend and re-gate — fix the already-landed defect, keep the branch and ticket (`lode-wp2r`)

This exit has **two triggers** (widened by `lode-2m93`; `lode-wp2r`'s original trigger and rationale
are preserved below as the first of the two, unchanged):

**Trigger 1 — `/land`'s combined re-gate (`lode-wp2r`, the original trigger).** Applies when the
escalation was raised by `/land`'s **combined re-gate** (Section 3 of this skill), not by
`land-review` or a producer gate, and all of the following hold:

- `land-review` **accepted** this branch (no semantic objection — the escalation happened *after*
  review, at re-gate time).
- The merge precheck (2b) was clean — the branch merges onto `trunk` without conflict.
- The re-gate failure is traceable to code **already on `trunk`**, not to anything this branch
  introduces — i.e. the branch would have gated green before whatever landed the defect, and gates red
  now only because it is the first thing to exercise the defective landed code.

Under those conditions, neither of the other exits fits: "land as-is" is defined for a branch that
needs no change, and this one does; "rebuild" discards a branch that `land-review` already judged
sound, which is the wrong instrument for a defect that isn't the branch's fault.

**Trigger 2 — a `land-review` semantic-review escalation whose resolution requires a scoped on-branch
edit (`lode-2m93`).** The three combined-re-gate conditions above attach to trigger 1 only — they are
meaningless here, since there is no re-gate failure to trace. This trigger's sole condition is that
the human, resolving a `land-review` escalation, decides the fix requires editing the branch, rather
than landing it as-is, rebuilding it, or dropping it. See [Re-entry per escalating
source](#re-entry-per-escalating-source--re-enter-at-the-gate-that-escalated) for the discriminator
this draws against the unwidened `land-review` row, which still re-enters at exit (a).

Either way, the human amends the branch itself — a small, scoped fix — and sends it back through the
pipeline **one gate earlier than a normal "land as-is,"** at `ready-for-code-review` rather than
`ready-for-land`: the amendment is new, ungated content that `land-review`'s original accept never
saw, so it needs its own technical review (`code-reviewer`) before a semantic re-review is worth
spending. Its rows are in [Re-entry per escalating
source](#re-entry-per-escalating-source--re-enter-at-the-gate-that-escalated), alongside exit (a)'s.

**Write the added scope into the ticket's acceptance criteria, not only into a note** — exit (a)'s
"materialize the decision first" rule, for the same reason, and applying to both triggers. The
re-entered branch still has to clear `ready-for-land`, where `/land`'s next pass re-runs
`land-review`, which reads `acceptance_criteria` as the contract; an amendment recorded only in the
notes reads to that re-review as scope creep on a branch it already accepted.

```bash
bd update <id> --acceptance="<original criteria + what the amendment must satisfy>"
bd update <id> --remove-label land-escalated --add-label ready-for-code-review \
  --append-notes "RESOLVED (human, amend-and-re-gate): <the landed defect + the fix>"
scripts/bd-dolt-push.sh
# The human may amend land/<id> before the swap, or leave it to the code-reviewer that /code's
# stranded-review sweep dispatches. Either way it re-gates and re-pushes land/<id>.
```

First observed and resolved this way in `lode-pcee` (2026-07-28): `land-review` had accepted the
branch and the 2b precheck was clean, but the combined re-gate failed on a whole-file substring
predicate bug in `tests/test_gate_lib.py::_consumers()` that had landed separately (`lode-bss5`). The
human's resolution tightened that predicate directly on `land/lode-pcee`, re-entered at
`ready-for-code-review`, and the branch landed from there — the precedent this exit formalizes.

Every exit ends the same way: **`land-escalated` is gone**, so a surfacer's queue — the forthcoming
`/sweep` (`lode-nps.1`) — can actually drain rather than growing monotonically.

**Scope.** The exits above resolve the label as **`/land`** sets it — from `land-review`'s
semantic-review escalation (exit (a) re-entry = `ready-for-land` when the branch needs no change,
exit (d) re-entry = `ready-for-code-review` when the human's resolution requires a scoped on-branch
edit — `lode-2m93`) and, for exit (d), also from `/land`'s own combined re-gate. `/code`'s producers
set the *same* label from three other places — a `coding` build-time clarifying decision, a
`code-reviewer` technical-review escalation, and a `coding` rebase-pickup conflict. Exits **(b)**
rebuild and **(c)** drop apply to every source unchanged — they only close the ticket and GC the
branch, and neither cares which gate escalated. Exit **(a)** does **not** generalize to a single
label: a build-time escalation never reached `ready-for-code-review` (it never had its technical
review), and a rebase-conflict escalation still does not merge onto `trunk` — re-entering either
blindly at `ready-for-land` would skip a gate that has never actually run. Exit **(d)** does not apply
to those three producer sources either — not because a producer's gate can never go red on a defect
inherited from `trunk` (it can), but because a producer-side branch still has a live `coding` or
`code-reviewer` agent free to fix what it finds in-band. Exit (d) is for the positions where no agent
is left holding the branch: `/land`'s own combined re-gate, and a `land-review` escalation whose
resolution the human decides needs an on-branch edit.

### Re-entry per escalating source — re-enter at the gate that escalated

DECISION (human, 2026-07-08, `lode-08g`): whichever gate could not resolve the ambiguity is the gate
that re-runs once the ambiguity is resolved — the same gate, against the now-unambiguous ticket, never
a later gate taking the resolution on faith.

| escalated by                                                                 | exit | re-entry label          |
|------------------------------------------------------------------------------|------|-------------------------|
| `/land` semantic review (`land-review`), resolution needs **no** branch edit | (a)  | `ready-for-land`        |
| `/land` semantic review (`land-review`), resolution needs **a** branch edit  | (d)  | `ready-for-code-review` |
| `code-reviewer` technical review                                             | (a)  | `ready-for-code-review` |
| `coding` rebase-pickup conflict                                              | (a)  | `needs-rebase`          |
| `coding` build-time clarification                                            | (a)  | `ready-for-code-review` |
| `/land` combined re-gate (defect already on `trunk`)                         | (d)  | `ready-for-code-review` |
| `/land` §2a malformed `land_head` metadata (`lode-xdg3`)                     | (a)  | `ready-for-land`        |

The two `land-review` rows are written as an explicit **no branch edit / a branch edit** pair so they
cannot both match one escalation: every `land-review` escalation matches exactly one of them, decided
by the human's resolution.

The exit column names which resolution applies (`lode-08g` for the exit (a) rows, `lode-wp2r` for the
unwidened exit (d) row, `lode-2m93` for the new `land-review` + on-branch-edit exit (d) row). What
separates the two `land-review` rows is **not** the escalation, but a property of the human's
*resolution*: whether it requires editing the branch. That is the one judgement call this table asks
a human to make — `land-review` objecting is common to both rows, so the discriminator has to live in
what the human decides to do about it, not in which gate raised the escalation. (This is consistent
with the combined-re-gate row's own precedent: branch-immutability does not separate rows either — a
`needs-rebase` pickup merges `trunk` in, and a build-time re-entry hands a deliberately unfinished
branch to `code-reviewer`, so exit (a) rows change the branch too.) Every
row follows the same mechanical shape — write the decision into the ticket first, then swap
`land-escalated` for the row's label and publish:

```bash
bd update <id> --append-notes "RESOLVED (human): <the decision>"
bd update <id> --remove-label land-escalated --add-label <ready-for-code-review|needs-rebase>
scripts/bd-dolt-push.sh
```

**The build-time case is the deliberately arguable one, decided rather than left implicit.** A
build-time escalation means the producer stopped mid-build, so its branch is green-but-possibly
**incomplete** and never reached `ready-for-code-review` on its own. Re-entering it there hands the
`code-reviewer` a branch that may not implement the whole ticket. That trade-off is accepted anyway —
routing every trivially-answerable build question through a full exit-(b) rebuild would over-charge a
question the branch itself may already answer correctly — under three conditions that make it safe
rather than silently risky:

1. The human writes the resolved answer into the ticket (`--append-notes`) **before** flipping the
   label, so the `code-reviewer` reads the resolved ambiguity rather than rediscovering it.
2. The `code-reviewer` is not obliged to pass a half-built branch: **escalate** is its standing
   non-pass outcome (revert to green, re-apply `land-escalated`), so a build-time re-entry asserts only
   that the *ambiguity* is resolved, not that the branch is *finished*. A still-incomplete branch
   escalates again on technical review — and `land-review`'s **bounce** verdict is the backstop if it
   slips past that too.
3. Re-entry at `ready-for-code-review` for the build-time source means "the decision is made, re-run
   the pipeline from technical review" — **not** "this branch is done."

---

## bd-sync discipline (non-negotiable)

I am the system's heaviest bd writer, and the repo runs **`import.auto: false`** (see `CLAUDE.md` /
`.beads/config.yaml`, fixed in lode-6ra): **Dolt is authoritative; `.beads/issues.jsonl` is an
export-only passive artifact, never a sync wire.** I honor that exactly:

- **Pull at the start, push after writes.** `bd dolt pull` opens the pass; `scripts/bd-dolt-push.sh`
  (a retry-on-reject wrapper around `bd dolt push` — backoff + re-pull between attempts, since a
  concurrent `/code` producer's write can transiently reject or lock-contend the push, lode-83d)
  follows *every* batch of bd writes (closes, bounce-creates, label changes). State travels
  cross-machine via `refs/dolt/data`, **never** via committed jsonl.
- **Never commit `.beads/issues.jsonl`, never `bd import` it.** I do not `git add` the jsonl export,
  and I never substitute `bd import` for `bd dolt pull` (import only upserts and silently misses
  deletions). When I merge a `land/<id>` branch, if it carried a jsonl diff I do **not** let it
  become a committed source of truth — Dolt + `bd dolt push` is the wire. (`import.auto: false`
  already stops the post-merge hook from re-importing a stale jsonl and reverting a close — the
  failure that bit lode-8bh / lode-wvf / lode-bxz; I do not re-enable that path.)
- **Never let the passive export block or enter a merge.** `bd dolt pull` can leave
  `.beads/issues.jsonl` *staged* (index != `HEAD`, worktree == index) — `git diff` reads clean in that
  state, so the drift is invisible until `git merge --no-ff` refuses it outright. I unstage it with
  `git restore --staged --worktree .beads/issues.jsonl` right before the Section 3 merge loop, every
  pass, on the assumption it may be staged even when `git diff` says otherwise.
- **Order so a close can't be reverted by a stale jsonl.** Push `trunk` and `bd close` the landed
  tickets, then `bd dolt push` to publish — the authoritative close lives in Dolt and is pushed
  immediately, never left to be overwritten by an intermediate committed jsonl on a later pull.

---

## What I never do

- **Land work I can't verify.** Drift (missing branch / SHA mismatch), a textual merge conflict (→
  `needs-rebase` kick-back), a red re-gate, or a `bounce` verdict all stop a branch from landing this
  pass.
- **Rebase a producer's branch myself, or run `land-review` on a branch that won't merge.** A branch
  that fails the 2b precheck is kicked back `needs-rebase` for the *producer* to rebase in its own
  worktree — I never touch a producer's worktree or rewrite its branch, and I don't spend a semantic
  review on contents a rebase will change.
- **Land on a `bounce`/`escalate`, or skip the semantic review.** The review is the *first* task per
  branch; only an `accept` enters the merge set.
- **Run two landers at once**, or run the loop on more than one machine — the local lock + one-machine
  convention is the v1 serialization guarantee.
- **Commit the passive `.beads/*.jsonl` export, or `bd import` it** in place of `bd dolt pull`.
- **Touch a producer's worktree, or record a design decision in a bd note** instead of `docs/` (that
  forks the record).
- **Delete a `land/<id>` branch (bounce, or exit-(b)/(c) of a `land-escalated` resolution) without
  first checking for a live descendant** (lode-02v). A branch that another live `land/<dep>` already
  merged in must not be silently dropped — see [Bounce](#bounce--clear-failure)'s descendant check.
- **Merge a stacked dependent before its base, or land a dependent whose base isn't in this pass's
  accepted set.** [Section 3a](#3a-order-the-accepted-set--base-before-dependent-hold-an-orphaned-dependent)
  orders the merge set and holds an orphaned dependent rather than merging it out of order.
- **Trust `builds_on` bd metadata as the mechanism for detecting a stacked branch.** It's a producer
  breadcrumb only; I always derive the stacked-branch graph from git containment
  ([1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)).
- **File a bd ticket for an incidental discovery** — something I notice about /land's own mechanics
  mid-pass, not a per-branch verdict. I **report** it in [Stop and report](#stop-and-report) instead,
  and the **human reading that report** decides whether it becomes a ticket. Not `/sweep` — it only
  surfaces what is already *in bd*, so it cannot see a discovery I never filed. That is the point, not
  a gap to close by filing one so `/sweep` can find it: filing *is* the dupe generator this rule
  removes. The rule is scoped narrowly and does **not** touch the two sanctioned `bd create` paths,
  which stay exactly as written: the [Bounce](#bounce--clear-failure) rebuild ticket, and exit-(b) of
  [Resolving a `land-escalated` branch](#resolving-a-land-escalated-branch). Both are per-branch
  verdicts — /land's actual job — not incidental discoveries.

  **Not filing is not the same as leaving work for the human.** When the discovery's whole remedy is
  a one-line doc change, the report must carry the *patch* — exact text, file, derived line number —
  not just the gap: see [If the whole remedy is a one-line doc
  change](#if-the-whole-remedy-is-a-one-line-doc-change-report-the-patch--not-the-gap). That is what
  keeps "don't file a ticket" from silently becoming "hand the human a research task."

  *Why not-filing loses nothing:* every pass **executes** this skill's own code, so every pass gets
  the same opportunity to notice the same flaw — the observation recurs on its own, without a ticket
  to carry it between passes. lode-v4rk's dead epic-completion check is the proof: three independent
  /land passes noticed it (lode-95yo, lode-3kdm, and the finding that became lode-v4rk itself) and
  each time the finding survived to be reconciled, with no ticket required to keep it alive between
  passes — passes two and three re-derived it from scratch without consulting the existing ticket.
  So *not* filing loses nothing, and it removes the dupe generator those three filings are evidence
  of.

  *Rejected alternative — "search bd before filing":* the intuitive fix, and the weakest available.
  It codifies the improvised filing path instead of removing it, expanding /land's scope in the one
  direction its charter (surface, don't decide) says it should not go. It also aims at a step that
  never happened: all three filings created the ticket **first** and searched afterwards or not at
  all — lode-3kdm's close note records a pass that "searched bd only AFTER creating". A search-first
  caveat only binds an agent already consulting this file's filing guidance, and an agent improvising
  a filing path this file does not sanction is, by construction, not that agent — so the caveat would
  have prevented none of the three. And it's prose discipline layered onto a file already saturated
  with prose, where the two dedup mechanisms this repo actually relies on are structural instead:
  `/epic-audit`'s `epic-audited` label and `/sweep`'s durable digest issue. Do not reintroduce a
  search-then-file step here on the strength of this being re-derived as "the obvious fix" — it was
  considered and rejected (lode-9t7u).

## Stop and report

When the pass ends I release the lock (`scripts/land-lock.sh release`, [Section
4](#4-land-the-survivors) — or, on any exit that never reaches it, the staleness window does,
[Section 0](#0-single-lander-lock--acquire-first-every-tick)) and report: how many branches I
reviewed; which
**landed** (with the `trunk` merge SHA, in merge order); which I **kicked back `needs-rebase`** (they
never reached `land-review`); which I **bounced** (and the new superseding ticket IDs); which I
**escalated** (and the decision each owes a human — including a bounce that turned into a strand
escalation, per [1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)/[Bounce](#bounce--clear-failure));
which I **held** as an orphaned stacked dependent (Section 3a) and what base it's waiting on; any
**epic** I flagged `epic-ready-to-audit` because this pass closed its last child; anything that
**drifted**; and any **incidental discovery** — something I noticed about /land's own mechanics
mid-pass that isn't a per-branch verdict (see [What I never do](#what-i-never-do)) — named here
rather than filed as a ticket. On any
genuine ambiguity in the landing mechanics themselves — not a per-branch verdict, which `land-review`
owns — I stop and surface it rather than guess.

### If the whole remedy is a one-line doc change, report the patch — not the gap

Naming a one-line fix as a "discovery" and stopping there hands the human a research task: re-find
the surface, re-derive the wording, decide whether it earns a ticket. For a remedy that small the
ticket costs more than the fix, and [What I never do](#what-i-never-do) has already ruled out filing
one. So whenever I can state the remedy in a line or two of prose, I report it as a patch the human
can apply directly, with all three of:

- **The exact replacement text**, written out in full — the words to paste, never a description of
  what they should say.
- **Where it goes** — file path plus the line number I actually **derived this pass** (`grep -n` at
  report time, never recalled or estimated, per
  [`docs/conventions.md`](../../../docs/conventions.md)'s derive-identifiers fiat) — *and* the anchor
  line **quoted verbatim**, so the location survives the number going stale between my pass and the
  human reading it.
- **What it changes**, in one sentence, so the human can accept or reject without opening the file.

**I still do not apply it.** I run on `trunk` in the main checkout, and a doc edit typed here reaches
`trunk` with no branch, no technical review, no `land-review` and no re-gate — bypassing every gate
this skill exists to be, for exactly the class of change (prose in `docs/`, or in a skill's own
markdown) that this repo treats as its source of truth. The patch text is a **hand-off**: the human
pastes it, or feeds it to `/code` as a ready-made brief that needs no rediscovery.

**The escape hatch, stated so it isn't quietly stretched.** This applies only when the remedy is
purely *how to word it*. The moment the fix needs a judgment call about *what to say* — which of two
behaviours is correct, whether a rule should exist at all — it is no longer a one-line patch, and it
goes back to being an ordinary reported discovery for the human to decide. Length is the symptom, not
the test: a two-line change that encodes a decision is a discovery; a five-line change that only
transcribes an already-settled one is still a patch.
