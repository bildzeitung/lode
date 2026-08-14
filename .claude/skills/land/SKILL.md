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
scripts/land-heartbeat.sh --release
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
scripts/land-heartbeat.sh
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

**This is [`scripts/stacked-graph.sh`](../../../scripts/stacked-graph.sh) (lode-s9xe.2), not something I
derive by hand.** It used to be a fenced snippet here whose outer "for every ordered pair" loop existed
only as a comment — a bare `continue` sat outside any loop, so it did not parse, and the O(n²) driver
plus the direction test got re-improvised, untested, on every pass for an algorithm whose failure is
silent. The detection rule (shared history off `trunk`, not tip-ancestry), the direction test, and the
two known gaps (force-push; branched-from-base rather than merged-base — OBSERVED 2026-08-07 on
`land/lode-35nu.9`/`land/lode-kuc7`) live in the script's own header and are pinned by
`tests/test_stacked_graph.py`, which builds real repos for each case, including the moved-base-tip and
two-merge-base shapes a plausible reimplementation gets wrong. **Do not re-derive this logic here.**

Run it **once** per pass, into `$STATE_DIR/graph` — a file, not a shell variable, because no shell
state survives between this skill's separate Bash invocations, and Section 3's merge loop
(`land-merge-batch.sh`) and the isolation-replay loop (`land-replay.sh`) both read it back to decide
which dependents a conflicting or bounced base takes with it:

```bash
STATE_DIR="$(git rev-parse --git-dir)/land-state"   # re-derive -- fresh Bash invocation (lode-sfnb)
mkdir -p "$STATE_DIR"
scripts/stacked-graph.sh --base-ref origin/trunk --report-unordered | tee "$STATE_DIR/graph"
```

`$STATE_DIR` is wiped at the top of every pass (Section 1, lode-wjw4), so `$STATE_DIR/graph` can never
be carried over from a prior one — a branch can be bounced, dropped, or landed in between.

Output is one tab-separated record per line:

- `EDGE  <dependent>  <base>  direct` — `<base>` is `<dependent>`'s **nearest** base. This is the one
  [2c](#2c-run-the-semantic-gate) hands `land-review` to diff against; a *transitive* base would make
  the diff carry the intermediate branch's work as if it were this branch's. [Section
  3a](#3a-order-the-accepted-set--base-before-dependent-hold-an-orphaned-dependent) orders the merge set
  off these.
- `EDGE  <dependent>  <base>  transitive` — still a real dependency. `direct` + `transitive` together
  are the **full relation**, which is what [Bounce](#bounce--clear-failure) and the
  [exit-(b)/(c) resolution paths](#resolving-a-land-escalated-branch) need to ask "does deleting this
  branch strand a live descendant?"
- `UNORDERED <a> <b>` — related but with no derivable direction (siblings that share a base, or a
  producer that branched directly off `land/<base>` instead of merging it in). Treat as **related**: do
  not delete either branch without the descendant question being answered by a human.

**Exit 2 is a machine fault, never "no stacks."** Stop the pass and surface it: a query that could not
run must not be read as an empty graph, because that is precisely how a dependent gets merged before
its base.

A producer records a `builds_on` bd field as a breadcrumb, but that is intent only — the graph always
comes from git, never from bd.

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
scripts/land-heartbeat.sh
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

**This loop is [`scripts/land-merge-batch.sh`](../../../scripts/land-merge-batch.sh) (lode-s9xe.4), not
an inline `for`.** It drives `land-merge-one.sh` per id exactly as the loop above did (same `if CMD;
then rc=0; else rc=$?; fi` idiom, for the same reason: the negated form's `$?` is always 0 and would
read a machine-fault 2 as a clean merge), and on a real conflict it calls
[`scripts/drop-from-accepted.sh`](../../../scripts/drop-from-accepted.sh) (lode-s9xe.3) to enforce 3a's
invariant — the conflicting branch AND its dependents (1a's full relation, transitively) leave
`$STATE_DIR/accepted` **in the file**, not just in this shell's view of it, before the isolation-replay
loop below can re-read a stale copy and re-merge one of them. This used to be a `grep -vxF`/`mv` recipe
left as a comment for the agent to implement inline, at the exact moment a conflict had just fired;
`drop-from-accepted.sh` is the executable version, with its own tests.

```bash
STATE_DIR="$(git rev-parse --git-dir)/land-state"   # re-derive here -- this is a fresh Bash
MSG_DIR="$STATE_DIR/msg"                                # invocation; nothing from 3a's block persists
CONFLICTS_DIR="$STATE_DIR/conflicts"                    # except the FILES 3a wrote under $STATE_DIR
MY_TOKEN="$(cat "$(git rev-parse --git-dir)/land-lock-token" 2>/dev/null || true)"   # lode-q9pm
[ -n "$MY_TOKEN" ] || echo "land: WARNING -- no own-token available; land-lock ownership check is" \
  "DISABLED for this call (lode-67nk)" >&2

scripts/land-merge-batch.sh \
  --accepted "$STATE_DIR/accepted" --landed "$STATE_DIR/landed" \
  --msg-dir "$MSG_DIR" --conflicts-dir "$CONFLICTS_DIR" \
  --graph "$STATE_DIR/graph" --token "$MY_TOKEN"
```

`--accepted` is 3a's precompute, loaded (via `scripts/land-state-load.sh`, lode-dc4n) with the same
"missing fatal, empty OK" policy as ever: a **missing** file means 3a never ran at all — the
silent-failure shape lode-sfnb's governing rule exists to catch, and the script exits 2 for it. An
**empty** file is a legitimate outcome (lode-0jan) — every branch was already bounced, escalated,
held, or kicked back `needs-rebase` before this call started — and processes zero ids, so the re-gate
below is skipped and the pass falls through to Section 4 exactly as a real merge would. `--graph` is
[1a](#1a-compute-the-stacked-branch-graph--once-per-pass-from-git-never-from-bd)'s
`$STATE_DIR/graph`; omitting it silently skips the dependent drop on a conflict, so pass it always.

It prints one line per id, in accepted-set order:

| Record | Means | What I owe it |
|---|---|---|
| `LANDED <id>` | merged cleanly, appended to `$STATE_DIR/landed` | nothing |
| `CONFLICT <id>` | real textual conflict against a branch already merged this pass; left the accepted set | a [needs-rebase kick-back](#needs-rebase--kick-back) |
| `HELD <id>` | removed from the accepted set as a dependent of a branch this call classified `CONFLICT` (or an earlier `HELD`'s own dependent) | a HELD note — not conflicted, not rejected, it simply has no foundation left this pass |

**Exit 0** = the batch ran to completion (`LANDED`/`CONFLICT`/`HELD` are all non-fault outcomes — a
batch that conflicts or holds every id still exits 0). **Exit 2** = machine fault (per lode-9i2p's
rule this is never a branch verdict — including every `land-merge-one.sh` exit other than its
documented 0/1/2, and any `drop-from-accepted.sh` fault): stop the pass and surface the script's own
stderr as a human decision. There is no exit 1 — a batch of multiple ids has no single verdict to
report through the exit code.

It runs **no gates** and makes **no tracker writes** — every `nox` call and every `bd` write stays
mine, below.

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

  **This is [`scripts/land-replay.sh`](../../../scripts/land-replay.sh) (lode-s9xe.13), not a second
  copy of Section 3's merge loop.** It drives the same `land-merge-one.sh` / `drop-from-accepted.sh`
  pair the first-pass batch uses, plus what only the isolation path needs: its own
  `assert-main-checkout.sh` guard (a fresh Bash invocation needs its own — Section 1's cannot reach it),
  the `git reset --hard <base-ref>` that discards the first-pass merge, the baseline gate run, and the
  per-branch `git reset --hard HEAD~1` that bounces a culprit. One script, not a fenced block with a
  comment asking a human to "keep the two loops the same shape" — that was an unenforced sync invariant
  over the most destructive code in this file.

  ```bash
  STATE_DIR="$(git rev-parse --git-dir)/land-state"   # re-derive -- fresh Bash invocation
  MSG_DIR="$STATE_DIR/msg"
  CONFLICTS_DIR="$STATE_DIR/conflicts"
  MY_TOKEN="$(cat "$(git rev-parse --git-dir)/land-lock-token" 2>/dev/null || true)"   # lode-q9pm
  [ -n "$MY_TOKEN" ] || echo "land: WARNING -- no own-token available; land-lock ownership check" \
    "is DISABLED for this call (lode-67nk)" >&2

  scripts/land-replay.sh \
    --accepted "$STATE_DIR/accepted" --msg-dir "$MSG_DIR" \
    --conflicts-dir "$CONFLICTS_DIR" --landed "$STATE_DIR/landed" \
    --graph "$STATE_DIR/graph" --token "$MY_TOKEN" --base-ref origin/trunk
  ```

  **`--accepted` is loaded `--require-nonempty` — DELIBERATELY ASYMMETRIC with the first-pass batch**,
  which lets an empty accepted set through (lode-0jan). This script only runs after a combined re-gate
  turns red, and a nothing-merged pass skips that re-gate (and therefore this script) entirely, so an
  empty set here should be unreachable — which is exactly why it stays fatal rather than relaxed for
  symmetry: if it ever does arrive, `trunk` is byte-identical to `origin/trunk`, so the red is
  attributable to no branch in this pass, and a loud stop is the only honest outcome. `--landed` is
  truncated internally, immediately after the reset and before the baseline gates, so a baseline stop
  never leaves a durable record naming merges the reset just discarded.

  **The baseline gates run inside the script, once, before it attributes anything to a branch** —
  `nox -s tests` then `nox -s lock_currency`, both against the bare `--base-ref`. This is not optional:
  no gate here is a pure function of the tree, and this replay *deletes* what it blames. An ambient
  `FORCE_COLOR=3` in the landing session's own shell — not set anywhere in this repo — once reddened
  `nox -s tests` on a bare `origin/trunk` with nothing merged (lode-kq4v; `tests/conftest.py` now scrubs
  it, along with `NO_COLOR`/`TTY_COMPATIBLE`/`TTY_INTERACTIVE`, from every pytest invocation it collects
  for). Trusting an unbaselined red would have bounced the first branch in `--accepted`: `bd supersede`
  closes its ticket, a rebuild ticket carries a fabricated "turned the gate red" finding, and `git push
  origin --delete land/<id>` destroys the reviewed branch — for a variable this repo does not set.
  `nox -s lock_currency` fails the same test for its own reason: it asks whether the lock is a fixed
  point of the tree *plus* ambient tooling *plus* today's PyPI.

  It prints one line per id it actually reaches, in accepted-set order:

  | Record | Means | What I owe it |
  |---|---|---|
  | `LANDED <id>` | merged AND gated clean; stays merged on local `trunk` | nothing |
  | `CONFLICT <id>` | real textual conflict against an earlier survivor merged this replay; left the accepted set | a [needs-rebase kick-back](#needs-rebase--kick-back), not a bounce — its content wasn't judged bad, it just needs to replay onto the new `trunk` |
  | `BOUNCED <id>` | merged cleanly but turned a gate red; backed out via `git reset --hard HEAD~1`, left the accepted set | a **bounce** (new rebuild ticket, drop the branch) — not a textual conflict |
  | `HELD <id>` | removed from the accepted set as a dependent of a branch this call classified `CONFLICT` or `BOUNCED` | a HELD note, same as the first-pass batch |

  **Exit 0** = the replay ran to completion. **Exit 2** = machine fault — bad usage, a required file
  missing, the baseline gate itself red or faulting, or a called script faulting outside its own
  documented content verdict (lode-9i2p): stop the pass, land nothing further, surface the script's own
  stderr. There is no exit 1, same reasoning as `land-merge-batch.sh`: a batch of ids has no single
  verdict for the exit code to carry.

  **Every exit — a stop, or a normal finish holding whatever prefix it kept merged — leaves local
  `trunk` exactly as the script's own resets left it.** I restore none of it further; that is
  [Section 1](#1-setup-the-pass--dolt-authoritative-fetch-origin)'s job (lode-k9ef). The survivors it
  reports `LANDED` stay merged on local `trunk`; each `BOUNCED` id is handled like any other bounce, and
  each `CONFLICT` id like any other needs-rebase kick-back.

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

**MISTAKES.md — narrow, explicit exception to "report the patch, not the gap."** This check is owed
**every `/land` pass that reaches a verdict on at least one branch — not conditional on this pass
having an accepted set to merge.** It lives here, in Section 4, because that is where the trunk push
already sits and the common case (some branches landed) reaches it naturally here. But `land-review`
returns a `MISTAKES.md CANDIDATE` "on every verdict, not only bounce/escalate" (`land-review.md`), so
a pass where every branch bounces, kicks back `needs-rebase`, or escalates — leaving no accepted set,
and on some paths never reaching this section at all — still owes this check. If this section runs
this pass, it runs here, as below. If it does **not** — no branch was merged and nothing below this
point executes — the identical block runs instead from [Stop and report](#stop-and-report)'s own
entry point for that case, which also pushes the entry itself (`git push origin trunk`), since no
merge push follows it there. Before the trunk push below, check whether this pass surfaced a
qualifying mistake — either one I noticed myself this pass, or a `MISTAKES.md CANDIDATE` block a
`land-review` dispatch returned this pass — it cannot commit from its disposable worktree, so filing
is mine. "Qualifying" is CLAUDE.md directive 9's bar — not every bounce or drift. If nothing qualifies
this pass, skip this block entirely. If something does:

```bash
scripts/assert-main-checkout.sh || exit 1     # same reason as the reformat commit above (lode-pxyt)
# Dedup by INCIDENT, not exact wording (CLAUDE.md directive 9: "grep for the incident, not just
# exact wording") -- one exact phrase would miss the same root cause re-described in different
# words. -i: case-insensitive; -E: alternate a few candidates -- the ticket id if one exists, the
# file/script/mechanism at fault, and a couple of paraphrases of the failure -- rather than one
# fixed sentence.
grep -niE "<ticket id>|<mechanism or file at fault>|<a paraphrase of the failure>" MISTAKES.md
```

- **Already present** (a prior stage — a producer in its worktree, the code-reviewer, an earlier
  `/land` pass — already filed the same incident) → skip. Entries are append-only; do not double-file.
- **Not present** → append a new entry at the **top** of the log (newest first), in directive 9's
  entry shape, then commit it **directly on `trunk`** — the same narrow exception CLAUDE.md's
  workflow gotchas already carry for a direct doc-only trunk commit. This file is append-only prose
  with no gate risk (no code, no tests, nothing `nox` or a technical review would catch), so unlike
  an ordinary "gap" its remedy is never a decision that needs review:

  ```bash
  scripts/assert-main-checkout.sh || exit 1                          # fresh Bash invocation; this commit names no ref or path either (lode-pxyt)
  git add MISTAKES.md
  git commit --no-verify -q -m "docs: record <short incident name> in MISTAKES.md"   # --no-verify: same beads pre-commit hook reason as above
  git show --stat HEAD   # confirm only MISTAKES.md rode along
  ```

  This commit rides in the same push as the reformat commit above and the merge commits already on
  `trunk` — I do not push separately per entry.

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
scripts/land-heartbeat.sh

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

# Local worktree + branch GC is NOT done per-ticket (lode-h1vn) -- it is entirely
# scripts/worktree-gc-sweep.sh's job, run once here at the end of every pass. It catches every
# just-landed builder worktree on the same pass (this pass's `--no-ff` merge is what makes each one's
# HEAD an ancestor of trunk, a few lines above), discovering worktrees live off `git worktree list
# --porcelain` rather than trusting per-ticket metadata that can drift.
#
# WHY A SCRIPT. This was ~80 lines of destructive shell (worktree remove --force, branch -D) fenced
# here, where nothing lint-checked it and only a markdown scanner could assert anything about it. It
# performs the most dangerous operations in the harness, so it belongs where it can be shellcheck'd and
# unit-tested (tests/test_worktree_gc_sweep.py and friends). The per-candidate DECISION lives in
# scripts/worktree-gc-classify.sh (lode-9owc); this sweep is what a side-effect-free classifier cannot
# own -- reading the porcelain, resolving a stale lock, and the two destructive calls the classifier
# only ever recommends. Its own header carries the full contract: the unlocked+clean+ancestry predicate
# (widened by lode-amif to cover a captured-on-origin escalated-ticket worktree, not just one merged
# into trunk), the lode-9hgu dirty-tree guard that protects an exited agent's uncommitted scratch, the
# lode-yrtu dir-only age floor for a not-yet-merged builder worktree, and both bare-ref backstops
# (`land/*` orphaned by remote deletion, `worktree-agent-*` orphaned with no remote at all) that catch
# what the worktree sweep itself structurally cannot see. Takes **no `--base-ref`** (lode-0867): the
# classifier hardcodes `trunk`, and lode has exactly one default branch.
#
# ONE UNENFORCED COUPLING keeps this sweep reclaiming anything at all: `.gitignore`. A finished
# worktree is full of untracked build junk (`venv/`, `.nox/`, `__pycache__/`) and reads clean ONLY
# because those are ignored. Un-ignore one and every worktree reads dirty and the sweep silently
# reclaims NOTHING. Re-check this whenever `.gitignore` changes.
scripts/worktree-gc-sweep.sh
```

It prints one summary line per sweep plus one per bare-ref backstop. **"reclaimed 0 of 0" (nothing to
do) reads differently from "reclaimed 0 of N" (everything was skipped)** — a regression that zeroes
out GC must be visible here, not indistinguishable from idle. Exit 2 is a machine fault or a wrong
checkout; it never means "nothing to reclaim" — surface it as a human decision same as any other
machine-fault exit in this section.

Then release the lock — the pass is fully done, and releasing now beats waiting out the staleness
window for no reason (lode-aps3; see Section 0):

```bash
scripts/land-heartbeat.sh --release
```

`bd close` unblocks dependents — that is *why* the lander closes (the producer never does): a closed
ticket frees the next layer of `bd ready`. Closing is mine because the merge decision is mine.

The worktree GC is **best-effort and machine-local**, and (since **lode-h1vn**) entirely
`scripts/worktree-gc-sweep.sh`'s job — there is no separate per-ticket removal step any more, and
**nothing in `/land` reads or writes `review_worktree`/`review_branch`** (lode-2m89: the deleted
per-ticket loop was their only consumer; the sweep discovers worktrees live off `git worktree list
--porcelain` instead, and `/code`'s own proactive reclaim of a reviewer's or rebase-pickup's launch
worktree, lode-vs7g, derives its target from the ticket id rather than a recorded path). Builds can
happen on several machines: a worktree on another machine is invisible to this sweep and reclaimed by
that machine's own `/land` instead.

The sweep reclaims any worktree under `.claude/worktrees/` — branch-attached or detached, whoever
created it — that is **unlocked**, **clean**, and captured elsewhere: an ancestor of `trunk`, or (since
**lode-amif**, for a reviewer/rebase-pickup worktree whose branch never merges by definition, i.e. an
escalated ticket) an ancestor of its own `origin/<branch>`. A just-landed builder worktree qualifies the
moment this pass's `--no-ff` merge lands, above. A **bounced** or **escalated** builder worktree
qualifies for neither ancestry arm (its content never reaches `trunk` or `origin`), so it is kept —
except its *directory* alone, once its last commit ages past `LAND_WORKTREE_DIRONLY_MIN_AGE_SECONDS`
(default 6h): the dir-only arm reclaims the checkout but deliberately keeps the branch ref, so no commit
is ever lost. A **dirty** worktree is never touched, in any bucket. Full contract, every predicate, and
the design history behind each one (lode-jiyk, lode-r78, lode-mxeu, lode-yrtu, lode-em6v, lode-9hgu,
lode-amif, lode-j5i0) live in `scripts/worktree-gc-sweep.sh`'s and `scripts/worktree-gc-classify.sh`'s
own headers — read there, not here, for the reasoning behind a specific bucket.

The two bare-ref backstops the script also runs catch what the worktree sweep structurally cannot: a
local `land/<id>` ref with no worktree attached and no `origin/land/<id>` counterpart left, and a local
`worktree-agent-*` ref with no worktree attached at all (never pushed to origin, so it needs the
`merged`-into-`trunk` guard instead of a remote-existence check). Both are name-keyed, unlike the
worktree sweep itself, because `refs/heads/*` is shared with human branches.

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
# Omit this whole block if nothing was committed. Derived into a variable and shape-checked
# before the write (lode-uvjr) — a malformed value here reads as drift on a later pass.
LAND_HEAD="$(git rev-parse origin/land/<id>)"
scripts/validate-sha40.sh land_head "$LAND_HEAD" || exit $?
bd update <id> --set-metadata land_head="$LAND_HEAD"
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

**Re-entering at `ready-for-code-review` (the `code-reviewer` and `coding` build-time rows above)
MUST also (re)write `metadata.review_head` as part of this same resolution, before publishing
(lode-uomo).** This hand-edit happens outside any `/code` run, and nothing else on this path forces
that field to exist — `/code` step 1's stranded-review sweep will not dispatch a `code-reviewer`
until a non-empty `review_head` can be established (deliberately: it will not guess a head to
review), so omitting this step used to strand the ticket at `ready-for-code-review` forever,
`in_progress` and unreturned by `bd ready`, invisible to everything except a repeated "needs a human"
line in `/code`'s own report every pass. (OBSERVED: `lode-1fzq` — resolved by hand exactly like this,
after the omission had already stranded it.) Step 1 now derives the field itself from the live
`origin/land/<id>` tip when it's missing, but that is a **backstop**, not a substitute: set it here,
at resolution time, same as the build-time escalation path already does on the agent side
(`coding.md`, `lode-t83` Gap 1). Validate before writing, exactly as the backstop does — an
`ls-remote` that resolves nothing prints nothing, and an unguarded write would put an *empty*
`review_head` on the ticket, re-creating the very state this step exists to prevent:

```bash
SHA="$(git ls-remote origin "refs/heads/land/<id>" | cut -f1)"
scripts/validate-sha40.sh review_head "$SHA" && bd update <id> --set-metadata review_head="$SHA"
  # --set-metadata (upsert), NOT --metadata (a full-blob replace that silently drops
  # land_head/land_summary/other keys already on the ticket)
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
  and the **human reading that report** decides whether it becomes a ticket. (This rule is about **bd
  tickets**; a MISTAKES.md append is a doc write, and its sanctioned path is in [Section
  4](#4-land-the-survivors) — lode-v1rk.) Not `/sweep` — it only
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

### MISTAKES.md filing on a pass that never reaches Section 4

If this pass reached [Section 4](#4-land-the-survivors) and ran its own MISTAKES.md block, that
already covers this pass — nothing further to do here. But whenever this pass ends with **no accepted
set to merge** on a path that skips Section 3 and Section 4 entirely (every branch bounced, kicked
back `needs-rebase`, or escalated; or the pass stopped early on a machine fault), the check is still
owed — `land-review` can return a `MISTAKES.md CANDIDATE` on any verdict, not only when a branch also
happens to land. Before releasing the lock, check whether this pass surfaced a qualifying mistake —
one I noticed myself, or a `MISTAKES.md CANDIDATE` a `land-review` dispatch returned this pass — using
the same bar and the same block [Section 4](#4-land-the-survivors) uses (CLAUDE.md directive 9's bar;
dedup by incident via `grep -niE`, never one exact phrase). If nothing qualifies, skip this entirely.
If something does, run it here, verbatim:

```bash
scripts/assert-main-checkout.sh || exit 1     # same reason as Section 4's copy (lode-pxyt)
grep -niE "<ticket id>|<mechanism or file at fault>|<a paraphrase of the failure>" MISTAKES.md
```

- **Already present** → skip. Entries are append-only; do not double-file.
- **Not present** → append a new entry at the **top** of the log (newest first), in directive 9's
  entry shape, then commit it directly on `trunk`, same as Section 4's copy:

```bash
scripts/assert-main-checkout.sh || exit 1
git add MISTAKES.md
git commit --no-verify -q -m "docs: record <short incident name> in MISTAKES.md"
git show --stat HEAD   # confirm only MISTAKES.md rode along
```

**This path has no merge push to ride in on — push it myself, right here**, since Section 4's own
`git push origin trunk` never runs on this path:

```bash
scripts/assert-main-checkout.sh || exit 1
# This push must carry the MISTAKES.md doc commit and NOTHING ELSE. Section 4's re-gate is the
# only thing that certifies merge output, and it did not run this pass -- so if anything other
# than the single commit just made is unpushed, this path is not the right one to push it.
# STOP and report instead; never advance origin/trunk past un-re-gated content.
test "$(git rev-list --count origin/trunk..trunk)" = 1 || exit 1
git push origin trunk
git status                 # MUST show trunk up to date with origin
```

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
