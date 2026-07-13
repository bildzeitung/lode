# lode — Agent dev workflow

How lode is *built*. The other docs describe the system; this one describes the **agent
loops that produce it** — a **design loop** that stress-tests a plan before it's built, a
**coding loop** in which a *builder* (Sonnet) produces a green branch and a separate *code-reviewer*
(Opus) runs the technical review on it (solo, or fanned out across several tasks at once with
`/code <id> <id> …`), and a **landing loop** in which a single `/land` lander is the **only** thing
that writes `trunk`. The three are the last three sections of this doc. See [design.md](design.md)
for the thesis and the build sequencing the work flows through.

The operational source of truth for each loop is its skill/agent definition under
[`.claude/`](../.claude); this doc is the map, not the mechanics. The hard project invariants live
in [`CLAUDE.md`](../CLAUDE.md) and [`AGENTS.md`](../AGENTS.md) — **where they and this doc disagree,
`CLAUDE.md` wins.**

---

## The loops

Work moves through three passes, with the human as the hinge:

1. **Design loop — `debate`.** Before anything is built, a plan, a beads ticket tree, a bug-fix
   approach, or a proposed `docs/` change is handed to the `debate` skill, which *pushes back*:
   it surfaces ambiguity, hidden assumptions, sequencing gaps, and risky approaches, and reports
   them to the human. It never edits `docs/` or beads as a side effect. The human revises until
   the plan is sound.
2. **Coding loop — `/code` → `coding` builder, then `code-reviewer`.** Once the plan is sound and
   captured as beads issues, `/code` runs each task in **two dispatched phases**. First a `coding`
   **builder** (Sonnet) carries it through an orderly cycle in an isolated worktree: claim → build →
   green gates → push a `land/<id>` branch → mark **`ready-for-code-review`** → **keep the worktree**
   → stop. Then a `code-reviewer` (Opus) fetches that pushed branch and checks it out into its **own**
   launch worktree, runs the technical review (`/code-review` + `/simplify`), re-gates, re-pushes, and
   swaps the ticket to **`ready-for-land`**.
   The builder never reviews its own work; neither agent merges, closes, or writes `trunk`.
3. **Landing loop — `/land`.** A single lander drains the `ready-for-land` queue: it semantically
   reviews each branch, merges the accepted set into `trunk`, re-gates, closes the tickets, and
   pushes. It is the **only** thing that writes `trunk` (see
   [the landing loop](#the-landing-loop--build-review-land)).

The boundaries are deliberate: **debate decides *what* and *whether*; the coding loop decides *how*
and *builds and reviews* it; the landing loop decides *whether it lands* and *does the merge*.**
Keeping the merge decision out of the hands of the agent that wrote the code is the point. Design
decisions settle into `docs/` and beads; only then does code get written (see
[how the loops connect](#how-the-loops-connect)).

---

## The design loop — `debate`

`debate` is a single, non-looping pass whose only job is to **argue with the plan**. You give it
something about to be built; it reads the *whole* thing before forming an opinion, challenges it on
the axes that apply, and hands the criticisms back. It does **not** implement, close tickets,
dispatch other agents, or silently rewrite issues — it runs once and stops. (Skill:
[`.claude/skills/debate/SKILL.md`](../.claude/skills/debate/SKILL.md).)

What it reads depends on the mode:

- **A conversation plan / approach** — the proposal as written (re-read, not remembered).
- **A beads issue or epic** — `bd show <id>`, then `bd dep tree <id>` and `bd show` on every
  subtask: titles, descriptions, acceptance criteria, notes, design.
- **A proposed `docs/` change** — cross-checked against the source of truth in `docs/`; a plan that
  contradicts a settled decision is itself a finding.

It then challenges on four axes — **ambiguity** (is "done" unambiguous?), **assumptions** (an
unrecorded architectural/tech decision? a repo state that may not hold? an uncited external?),
**sequencing & dependencies** (are all blockers captured? does the stated order actually work?),
and **acceptance/verifiability** (could someone write the test without more clarification?). For a
bug-fix approach it also weighs **root cause vs. symptom**, **side effects**, and whether a simpler
standard pattern exists.

Findings come back grouped by item, each a *genuine blocker* — no padding, and a clean bill is a
valid outcome. The human decides what to do with them; nothing is persisted unless explicitly asked
(`bd update <id> --notes=…` / `--design=…`).

```mermaid
flowchart TD
    START["Human: 'debate this'<br>(plan / ticket / bug-fix / doc change)"] --> MODE{"Which mode?"}
    MODE -->|"ambiguous"| ASK["Ask which thing<br>before analysing"]
    ASK --> READ
    MODE -->|"clear"| READ["Read the WHOLE thing first<br>(bd show + dep tree, or<br>re-read proposal; cross-check docs/)"]

    READ --> CH["Challenge on the axes that apply"]
    CH --> A1["Ambiguity<br>is 'done' unambiguous?"]
    CH --> A2["Assumptions<br>unrecorded decision? repo state?"]
    CH --> A3["Sequencing<br>blockers captured? order valid?"]
    CH --> A4["Acceptance<br>verifiable by a test?"]

    A1 --> REP["Report findings to human<br>(grouped by item; genuine blockers only;<br>clean bill is valid)"]
    A2 --> REP
    A3 --> REP
    A4 --> REP

    REP --> STOP["Stop — runs once, no loop,<br>no dispatch, no side-effect edits"]
    STOP -.->|"human revises, may re-debate"| START
    REP -.->|"only if asked"| PERSIST["bd update --notes / --design"]

    classDef start fill:#fcf8e3,stroke:#8a6d3b,color:#1b1b1b;
    classDef work fill:#d9edf7,stroke:#31708f,color:#1b1b1b;
    classDef out fill:#dff0d8,stroke:#3c763d,color:#1b1b1b;
    class START,ASK start;
    class READ,CH,A1,A2,A3,A4 work;
    class REP,STOP,PERSIST out;
```

---

## The coding loop — `/code` → `coding` + `code-reviewer`

`/code` is the **only** sanctioned way to start coding work from the main session (which is
otherwise told not to spawn agents). The skill resolves the task from its argument and runs each task
in **two dispatched phases** — a `coding` **builder** (Sonnet), then a `code-reviewer` (Opus). **Bare
`/code`** fans out across the whole ready frontier; `/code --single` does the top one task;
`/code <id>` / `/code <id> <id> …` name the work explicitly — in every case it's **N builders in
parallel** (one per task, each in its own isolated worktree), each followed by its own reviewer. There
is **no `/code-parallel`**. (Skill:
[`.claude/skills/code/SKILL.md`](../.claude/skills/code/SKILL.md); agents:
[`.claude/agents/coding.md`](../.claude/agents/coding.md),
[`.claude/agents/code-reviewer.md`](../.claude/agents/code-reviewer.md).)

**Topology — run only one `/code` invocation at a time (concurrent invocations unsupported,
lode-pzr).** Fan-out (N builders in parallel) is safe and is the supported way to get parallelism —
each producer works a distinct ticket in its own worktree and pushes its own `land/<id>` branch, so
producers never collide with each other. But `/code` also sweeps for stranded work at the *start* of
every invocation — `needs-rebase` kick-backs and stranded `ready-for-code-review` re-entries — and
those sweeps select on a ticket's **label**, which is only cleared at the very *end* of the agent
dispatched at it. Two concurrent `/code` invocations can therefore both sweep in a ticket whose
agent from the *other* invocation is still live, and dispatch a second agent onto the same worktree
via `git -C`. The observed consequence today is benign (the loser's push non-fast-forward-rejects;
clean-tree assertions guard the worktree), but it is a race, not an invariant — so, mirroring how
`/land` states its one-lander, one-machine topology invariant ([below](#mechanics-decided)), `/code`
states its own: **don't start a second `/code` while one is still running against this repo; pass
more IDs (or use bare `/code`) to the same invocation instead.** This is narrower than `/land`'s
rule — it only forbids a *second concurrent invocation*, not a second machine outright, since a lone
`/code` invocation's own builders already may legitimately fan out across machines (see the coding
loop's [producers may fan out across machines](#running-the-loop-family-unattended--epic-audit-sweep)
note) as long as no other invocation's sweep is running concurrently with them. See
[decisions.md](decisions.md) for the rationale.

Argument resolution:

- **No argument** (the default) / **`--all-ready`** → fan out across the **independent, unblocked**
  `bd ready` frontier, honoring the dependency graph and the phase-a skeleton order.
- **`--single`** → one task: the agent picks the top unblocked item from `bd ready` — *the subagent
  chooses, not the dispatcher*.
- **A bd issue ID** (`lode-1a8`) / **several IDs** → claim and implement those (one builder each).
- **Free text** ("add a `--json` flag to search") → the agent files the bd issue itself, then codes.

Each builder then runs its orderly cycle. The worktree is **handed to it by the harness**
(`isolation: "worktree"`) — a subagent pinned at the repo root cannot create its own, so it begins
*already inside* `.claude/worktrees/agent-<hash>` on a branch off local `trunk` HEAD. It works
in-cwd with plain git, and if its `pwd` is ever the repo root it **stops and reports** rather than
writing on `trunk`. Before touching a file it **locks that worktree** (`git worktree lock`) — a
freshly created worktree has zero commits beyond `trunk`, so until the first commit its branch reads
as trivially "merged" into `trunk` by content identity, which is exactly what `/land`'s end-of-pass
backstop sweep otherwise treats as safe to reclaim; nothing raised that lock before (lode-oqr), so
every producer build was silently exposed for that window. It unlocks again right after its first
commit, once the branch has genuinely diverged and the backstop's own `merged`-into-`trunk` check
takes over as protection for the rest of the build. It claims the issue, builds the simplest thing
that works, takes it green through the gates, then **pushes a `land/<id>` branch to origin, marks the
ticket `ready-for-code-review` (recording its worktree path), keeps the worktree, and stops** — it
does **not** review its own work.

Then `/code` dispatches a **`code-reviewer`** (Opus) for that ticket. It fetches the pushed `land/<id>`
branch and checks it out **into its own launch worktree** — never `git -C` into the builder's
worktree, never `EnterWorktree`, and never the builder's worktree at all — runs the **technical
review** (`/code-review --fix` + `/simplify`, re-gate, keep the last green commit), re-pushes
`land/<id>`, and swaps the ticket to **`ready-for-land`**. Neither agent merges, closes, or writes
`trunk` — landing is [`/land`](#the-landing-loop--build-review-land)'s job. Final agent messages aren't
shown to the user, so `/code` relays what came back across **both** phases — which issue, that the
build gates and the technical review passed, the pushed branch and head SHA, or exactly where it
stopped (a build- or review-time escalation) and why.

> **Adding a brand-new `src/lode/*.py` module?** Build a worktree-local venv before `nox` — run
> `./scripts/python-init.sh` from *inside* the worktree. The shared `./venv` editable install
> resolves `lode` to the **main checkout's** `src`, so a new module that exists only in the worktree
> is invisible to it and `nox -s tests` fails with `ModuleNotFoundError`. **Editing an existing
> module needs no fresh venv** — that file already resolves under the main-checkout package.

```mermaid
flowchart TD
    INV["Human: /code &lt;arg&gt;<br>(bare /code fans out — one builder per ready task)"] --> RES{"Resolve arg"}
    RES -->|"bd id(s)"| T1["Claim that issue"]
    RES -->|"free text"| T2["Agent files the issue, then codes"]
    RES -->|"--single / none"| T3["Agent picks top of bd ready<br>(dependency frontier · phase-a order)"]

    T1 --> DISP["Phase 1 — dispatch coding builder<br>(Sonnet · isolation: worktree)"]
    T2 --> DISP
    T3 --> DISP

    DISP --> WT["Starts ALREADY inside<br>.claude/worktrees/agent-&lt;hash&gt;<br>(branch off local trunk HEAD)"]
    WT --> GUARD{"pwd is repo root?"}
    GUARD -->|"yes"| BAIL["STOP & report —<br>never write on trunk"]
    GUARD -->|"no, in worktree"| CLAIM["claim (bd update --claim)"]

    CLAIM --> IMPL["Read issue + acceptance + design,<br>then implement (Typer · ./venv ·<br>simplest thing that works)"]
    IMPL --> COMMIT["Commit in worktree<br>(Co-Authored-By trailer)"]
    COMMIT --> CLEAN1{"git status --short<br>empty?"}
    CLEAN1 -->|"no"| COMMIT
    CLEAN1 -->|"yes"| GATES{"Quality gates"}
    GATES -->|"nox -t fix · nox -s tests ·<br>validate-mermaid (if diagram)"| GFAIL{"Pass?"}
    GFAIL -->|"no"| FIX["Fix & re-run —<br>never hand off on a failing gate"]
    FIX --> GATES
    GFAIL -->|"yes"| FIXCOMMIT["Commit gate output<br>(fixes + nox -t fix reformatting)"]

    FIXCOMMIT --> PUSH["git push -u origin HEAD:land/&lt;id&gt;"]
    PUSH --> CLEAN2{"git status --short<br>empty?"}
    CLEAN2 -->|"no — edits after push,<br>never gated"| COMMIT
    CLEAN2 -->|"yes"| HANDOFF["Builder: mark ready-for-code-review<br>(worktree path · head SHA) ·<br>KEEP worktree · bd dolt push · STOP"]
    HANDOFF --> REV["Phase 2 — code-reviewer (Opus):<br>fetch + checkout land/&lt;id&gt; into OWN worktree ·<br>/code-review --fix + simplify · re-gate"]
    REV --> MARKL["Swap to ready-for-land<br>(head SHA · summary) ·<br>re-push land/&lt;id&gt; · bd dolt push · STOP"]
    MARKL --> DONE["/land lands it (separate loop) ·<br>/code relays both phases"]

    classDef start fill:#fcf8e3,stroke:#8a6d3b,color:#1b1b1b;
    classDef work fill:#d9edf7,stroke:#31708f,color:#1b1b1b;
    classDef gate fill:#fcf8e3,stroke:#8a6d3b,color:#1b1b1b;
    classDef bad fill:#f2dede,stroke:#a94442,color:#1b1b1b;
    classDef good fill:#dff0d8,stroke:#3c763d,color:#1b1b1b;
    class INV,RES start;
    class T1,T2,T3,DISP,WT,CLAIM,IMPL,COMMIT,FIXCOMMIT,PUSH,HANDOFF,REV work;
    class GATES,GFAIL,GUARD,CLEAN1,CLEAN2 gate;
    class BAIL,FIX bad;
    class MARKL,DONE good;
```

### Concurrency cap (lode-2cf)

`/code`'s fan-out (builders and reviewers, plus the step-0/step-1 sweep dispatches) shares **one
concurrency cap** for the whole invocation — it was previously unbounded, dispatching the entire ready
frontier at once.

**Why.** On 2026-07-10 the Claude Code host process crashed twice, each time with **7** concurrent
`/code` agents in flight (builders + reviewers), orphaning the whole fleet mid-run. Each agent's gate
is `nox -s tests` with pytest-xdist `-n auto` = **8 workers**, each holding a cached ONNX cross-encoder
(the [reranker](configuration.md#retrieval-and-ranking)) in memory — 7 agents × 8 workers on a
15GiB/8-core WSL2 VM is the prime suspect for the crash (no `dmesg` OOM lines survived, since WSL2
restarts the whole VM on a crash, so the memory hypothesis is strong but not conclusively proven —
the cap is cheap insurance regardless). After manually staggering to **~4** concurrent agents, the
identical workload completed with zero further crashes.

**What.** `.claude/skills/code/SKILL.md` computes `CODE_MAX_CONCURRENT_AGENTS` once, at the start of
every invocation, before its step-0 sweep. **Never** more than that many agents — builders and
reviewers combined, across every dispatch source (step 0's rebase pickups, step 1's stranded-review
pickups, Phase 1 builders, Phase 2 reviewers) — run concurrently; the rest of the resolved task set
queues and dispatches as running agents complete and free a slot.

**Default derivation (deliberately simple — a static per-machine number the user sets beats a clever
heuristic that guesses wrong, per this ticket's own design note).** Read `MemAvailable` from
`/proc/meminfo` (falling back to `MemTotal` if `MemAvailable` is absent), divide by an estimated
**~3GiB per-agent gate footprint** (an 8-worker xdist run holding a cached reranker), then clamp to
`[1, nproc/2]` and floor at 1. On the 15GiB/8-core WSL2 machine the crash occurred on, this resolves to
**4** with no user action — matching the empirically-stable stagger count.

**Override — machine-local, no `SKILL.md` edit.** The env var `LODE_CODE_MAX_CONCURRENT_AGENTS`, when
set, wins outright over the derivation (no clamping — an explicit user choice is trusted as-is). Set
it durably per machine via `.claude/settings.local.json`'s `"env"` block (gitignored, applied to every
Claude Code session on that machine):

```json
{ "env": { "LODE_CODE_MAX_CONCURRENT_AGENTS": "6" } }
```

or export it in the shell that launches `claude` for a one-off override. The derivation/default logic
itself stays in the committed skill so it travels to every clone; only the override is machine-local.
The skill re-reads the env var fresh at the start of every invocation, so a changed value takes effect
on the next `/code` run without any other action.

### Invariants the coding loop never breaks

A quick card; the full list is in [`.claude/agents/coding.md`](../.claude/agents/coding.md) and
[`CLAUDE.md`](../CLAUDE.md).

| Thing | Rule |
|---|---|
| Default branch | `trunk` — **never** edit directly *and never landed by a producer*; `/land` owns every write to it |
| Worktrees | harness-made (`isolation: "worktree"`) under `.claude/worktrees/`, branched from local `trunk` HEAD, pushed to `origin/land/<id>`; the **builder keeps its worktree** (the reviewer no longer drives it — it checks `land/<id>` out into its own worktree instead — but `/land`'s worktree GC still keys off it; removed after land) |
| Worktree lock | builder `git worktree lock`s it before step 4, `git worktree unlock`s it right after its first commit — closes the gap where a zero-divergence worktree reads as "merged into `trunk`" to `/land`'s backstop reclaim sweep (lode-oqr) |
| Models | builder on **Sonnet** (cheap), code-reviewer on **Opus** (review quality); neither reviews work it authored |
| Concurrency cap | `/code` never runs more than `CODE_MAX_CONCURRENT_AGENTS` agents (builders + reviewers + sweep dispatches) at once; memory-derived default (4 on the 15GiB/8-core WSL2 crash machine), overridable via `LODE_CODE_MAX_CONCURRENT_AGENTS` (env var / `.claude/settings.local.json`'s `"env"` block) — [full rationale above](#concurrency-cap-lode-2cf) (lode-2cf) |
| Task tracker | **bd only** — no TodoWrite, no markdown checklists; file an issue *before* non-trivial work |
| Design decisions | doc edits under `docs/`, never a bd note or memory (that forks the record) |
| Gates | `nox -t fix`, `nox -s tests`; `scripts/validate-mermaid.sh` for diagram changes — never hand off / mark ready on a failing gate |
| Clean tree | `git status --short` empty before gating and before hand-off — `nox` gates the working tree, not `HEAD`, so the tree that gated green must be the tree committed and pushed; a dirty tree at either point silently drops uncommitted work (lode-tpt) |
| CLI framework | **Typer** (never argparse); venv at `./venv` |
| Done (coding loop) | builder hands off at `ready-for-code-review` (worktree kept); code-reviewer reviews, re-gates, swaps to `ready-for-land` (`bd dolt push`); `/land` does the merge/close |

---

## How the loops connect

The two loops share one substrate: **`docs/` and beads are the source of truth between them.**
Debate reads that substrate and argues against the plan; the human folds the criticisms back into
the docs and the ticket tree; the coding loop then reads the settled docs and the claimed issue and
writes code to them. Nothing skips the middle — a design decision that exists only in a chat
transcript, a bd note, or memory has *forked the record*, and the next loop will trust the docs and
miss it. Keep the decisions in the docs, and the two loops stay in agreement.

---

## Running the loop family unattended — `/epic-audit`, `/sweep`

The build/review/land story above is what happens to *one* task. Running the whole family — `/code`
+ `/land` + `/epic-audit` + `/sweep` — is intended to run **unattended**, each leg self-paced via
`/loop` (`/loop 5m /land`, `/loop 30m /epic-audit`, `/loop 30m /sweep`), draining its own queue
without a human present for the steady-state case. Most outputs already have a downstream consumer:
a `needs-rebase` ticket is picked up by the next `/code` sweep, a `land-review` bounce re-enters `bd
ready` as a fresh ticket, and `/epic-audit` runs itself as its own `/loop`.

Two kinds of output had **no** consumer at all, and a third loop leg was needed to close that gap
rather than any new prevention mechanism:

- A **`land-escalated`** branch — a genuine decision only a human can make — sat wherever `/land` (or
  a producer) left it; nothing pinged anyone, and (until the resolution transitions defined
  [below](#the-lander--land-drained-by-a-self-paced-loop) existed) nothing even removed the label
  once resolved.
- An **`epic-audited`** epic with every child closed sits open forever: `/epic-audit` deliberately
  never closes an epic itself (closure stays a human capability judgment — see
  [decisions.md](decisions.md)), so nothing re-arms or notices that it's ready for a human to close.

**`/sweep`** (skill: [`.claude/skills/sweep/SKILL.md`](../.claude/skills/sweep/SKILL.md)) is the
fourth loop leg that closes this gap — **surface-only, lowest-privilege**: it makes **no** decision
and dispatches **no** builder, lander, or auditor. Each pass it collects every open `land-escalated`
branch, every open `human`-labeled decision ticket, and every epic that is `epic-audited` + open +
fully child-closed; dedups the set against a durable digest issue it owns (located by the reserved
label **`sweep-digest`**, itself excluded from the `land-escalated` query so the digest can never
select itself); rewrites that digest only when the queue actually changed; and pushes one
notification per pass, only when something genuinely new appeared. The digest issue is the entire
write footprint — one self-owned bd row, Dolt-durable so the dedup state survives across machines and
sessions the way the `ready-for-land` queue does.

**Topology — landing-side loops are a one-machine invariant.** `/land`, `/epic-audit`, and `/sweep`
are all expected to run on **one** machine. This is not a new rule invented for `/sweep`: it is
`/land`'s existing single-lander-lock convention ([below](#mechanics-decided)), stated explicitly now
that a second and third landing-side loop exist alongside it — the lock only ever guarded overlapping
`/land` ticks on that one machine, and says nothing about where `/epic-audit` or `/sweep` run, so the
convention has to be named, not assumed. `/code` producers are the one leg that **may** fan out across
machines, because they write disjoint issue rows and push branches rather than touching any
landing-side shared state — but only *within* one `/code` invocation: two *concurrent* `/code`
invocations are unsupported, because their start-of-run sweeps race (lode-pzr; see [the coding
loop's topology note](#the-coding-loop--code--coding--code-reviewer)).

Two further safeguards a `/debate` pass considered for this unattended story — a `/land`
bounce-lineage cap and a `/code` rebase-attempt cap — were deliberately **deferred**, and an
epic-auto-close mechanism was deliberately **rejected**; see [decisions.md](decisions.md) for the
rationale and each one's revisit trigger. `/sweep` itself is the detector for the deferred caps: it
would surface a stuck bounce or rebase lineage the moment one actually occurs, well before a cap
would trigger.

---

## The landing loop — build, review, land

> **One landing path for everything.** Producers (the coding loop above) build a branch and a separate
> Opus code-reviewer technically reviews it; once it carries `ready-for-land`, a single `/land` lander
> is the **only** thing that ever writes `trunk` — solo or batch, one machine or several. This
> decouples *landing* from *building* through a durable hand-off, so the merge decision lands with the
> agent that *didn't* write the code.

### Why one landing path

In-session landing breaks down the moment work goes parallel or spans more than one sitting:

- **It isn't durable.** The build→land hand-off lives in the building session's context. If that
  session compacts, crashes, or is closed between "branch is green" and "branch is merged," the work
  is stranded on a branch nobody lands.
- **It doesn't cross machines.** In-session landing ties the merge to the machine that did the build.
  Development here happens across **multiple machines**, and a local worktree branch on machine A is
  invisible to a lander on machine B.
- **It puts the merge decision in the wrong hands.** The agent that *wrote* the code is the worst
  judge of whether it should land — it's attached to its own work.

So **all landing goes through `/land`, no exceptions** — solo or batch, one machine or several. The
durable hand-off is two facts: the branch lives on **origin**, and "ready-for-land" lives in
**beads**. Either survives a dead session and is visible from any machine.

### Two reviews, two stages

Review splits along a clean seam, and — crucially — **neither half is done by the author of the
code:**

- **Technical review — *the second phase of the coding loop*.** Bugs, cleanup, over-design,
  complexity. A separate **`code-reviewer` (Opus) owns this** — it fetches the pushed branch, checks
  it out into its *own* worktree, and fixes problems there, but it did **not** write the code.
  Splitting it out (the builder runs cheaper, on Sonnet) is what buys the independence *and* puts the
  review spend where it matters. It runs **autonomously** and you only hear about it on a real fork
  (see below).
- **Semantic review — *the first task of `/land`*.** Does it meet the ticket's acceptance? Is scope
  clean (no silent creep)? Are the design and the lode invariants honored? Is the approach right?
  This is the **build-side twin of `debate`**, and it's done by the lander, *not* the builder — the
  independence is the point. `debate` critiques the *plan* before building; the semantic review
  critiques the *result* before landing.

Both run autonomously and surface to you on the **same rule**: only a **genuine decision** pulls you
in (and, for the technical review, "I think I'm making it worse"). Everything else they handle.

### The producers — `/code`, solo or fan-out

There is **no separate `/code-parallel`** — once landing leaves the producer, building one task and
building five are the same act, just a different count:

- **bare `/code`** / `/code --all-ready` — **N builders in parallel** across the ready frontier.
- `/code --single` — one builder (top of `bd ready`).
- `/code <id>` / `/code <id> <id> …` — one builder per named id.

Each builder (the `coding` agent, on **Sonnet**), in its worktree:

1. **Claims and builds** the simplest thing that works; `nox -t fix` / `nox -s tests` green.
2. **Pushes the branch to origin** (`git push -u origin HEAD:land/<id>`) — the durable, cross-machine
   artifact (a *new* branch ref doesn't race `trunk`, so parallel producers stay safe).
3. **Marks the ticket `ready-for-code-review`** with the review context (worktree path, branch, head
   SHA), **keeps its worktree**, and **stops**. It does **not** review its own work, merge, close,
   push `trunk`, or touch the main checkout. A build-time **clarifying decision** is the only thing
   that escalates it (revert to green, `land-escalated`, surface async — never blocking a sibling).

#### Concurrent `bd dolt push` under fan-out — the load-bearing assumption (validated, lode-nps.3)

Bare `/code` already fans out N producers writing to the **same** beads database, so concurrent `bd
dolt push` to `refs/dolt/data` is a **pre-existing** load-bearing assumption behind running the loop
family unattended — not a new risk introduced by any later addition. Validated against Dolt's own
documented push/merge semantics (a stress test is corroboration only — a green run cannot prove the
absence of a rare lost-write race; it is not the basis for this determination):

- **No silent lost write.** `dolt push` — the primitive `bd dolt push` wraps, including in the
  **embedded** (in-process Dolt engine) mode lode runs in — is **fast-forward-only, exactly like
  `git push`**: "Pushes to existing branches require that your changes are fast-forward changes
  relative to what you are pushing to"
  ([DoltHub docs](https://www.dolthub.com/docs/concepts/dolt/git/remotes)), enforced by an atomic
  compare-and-swap on the branch ref. Two racing pushes can never both silently win — one is
  atomically accepted, the other is **rejected outright** and must retry (pull → merge → push again).
  Because lode's Dolt remote is itself a plain git remote (`sync.remote:
  git+ssh://…/lode.git`, storing chunks under `refs/dolt/data`), a git server's own atomic ref-update
  is a second, independent enforcement layer underneath Dolt's client-side check — the guarantee is a
  property of the remote ref update, not of any one client code path. Producers writing disjoint issue
  rows (one ticket per producer) merge cleanly on retry via Dolt's cell-level merge. The only
  multi-machine failure on record corroborates this by exclusion rather than confirming it directly:
  [beads#2466](https://github.com/gastownhall/beads/issues/2466) was a recurring `bd dolt pull` merge
  conflict on *shared bookkeeping rows* in the `metadata` table — "not a conflict on actual issue
  data," in the issue's own words — fixed in beads v0.60.0 by moving that auto-push state out of the
  replicated table and into a local file. (lode runs bd 1.0.4, well past that fix.)
- **Invariant:** a `bd dolt push` from any producer can be *rejected* under concurrent writers, but
  never *silently loses* another writer's already-committed data. Treat a non-zero exit from
  `bd dolt push` as "needs pull + retry," never as proof of corruption.

This investigation also surfaced two **gaps** the above safety net does not cover — filed as a
follow-up (lode-83d):

1. **No retry-on-reject existed.** All four `bd dolt push` call sites in the skills
   (`.claude/agents/coding.md`, `.claude/agents/code-reviewer.md`, `.claude/skills/land/SKILL.md`,
   `.claude/skills/epic-audit/SKILL.md`) were bare calls with no exit-status check — a rejected push
   failed without the caller noticing, undermining the "durable, cross-machine" hand-off the comments
   assumed.
2. **Embedded mode is documented as the wrong mode for this concurrency.** lode runs bd in
   **embedded (in-process Dolt engine)** mode (`bd dolt show` → `Mode: embedded`), and every git
   worktree of this repo shares **one** physical store on a given machine (`.beads/embeddeddolt` at the
   main checkout — confirmed via `bd where` from inside a linked worktree; no `.beads/redirect` is
   needed for this). Beads' own docs are explicit this mode is **single-writer, enforced via file
   lock**, and recommend Dolt **server mode** for "high-concurrency scenarios (multiple agents)" —
   precisely what `/code`'s bare fan-out is. The documented failure is a hard `database is locked`
   error with no built-in retry.

**Fixed (lode-83d): a shared retry-on-reject wrapper, not a Dolt server-mode migration.** The four
files above now go through `scripts/bd-dolt-push.sh`, which retries a non-zero-exit `bd dolt
push` with exponential backoff + jitter, pulling (folding in the winner's commit) between attempts.
One mechanism covers both gaps: a rejected push gets a fast-forwardable retry, and a transient
embedded-mode lock (held for at most one bd operation) clears well inside the backoff window.
Switching to Dolt server mode was evaluated and deliberately **not** done — it trades a few seconds
of occasional retry for a standing per-machine daemon (`dolt sql-server` + lifecycle/config), which
is the wrong weight for this workload. Full rationale and the revisit trigger are in
[decisions.md](decisions.md).

**lode-83d's own enumeration was prefix-blind (lode-bpl).** It found its four files with
`grep -rl "rtk bd dolt push" .claude/`, which cannot see a call written without the `rtk` prefix —
CLAUDE.md's golden rule says to always prefix with `rtk`, but that's a human convention, not
something a literal grep enforces. A prefix-agnostic re-audit
(`grep -rnE '(rtk +)?bd +dolt +push'` over `.claude/`, `docs/`, and `scripts/`, worktrees excluded)
turned up two more unwrapped call sites inside unattended loops, now also routed through the
wrapper: `.claude/skills/land/SKILL.md`'s exit-(a) re-entry step (a bare call added by lode-08g,
after lode-83d's audit ran) and `.claude/skills/sweep/SKILL.md`'s publish step (a skill that didn't
exist yet when lode-83d ran its audit). Three more bare mentions survive as **deliberate
exemptions**, not oversights: `.claude/skills/debate/SKILL.md` (`/debate` is human-invoked and
interactive — a failed push is observed directly, unlike the unattended loops above — see the
in-line note at its persist step), and `.beads/README.md` / `AGENTS.md` (generic, beads-generated
quick-reference boilerplate demonstrating the base `bd` CLI to a human reader, not an automated call
site in any skill). Any future "where do we call X" audit across `.claude/` should grep
prefix-agnostically from the start — the failure mode was the enumeration method, not any one
missed file.

### The code-review pass — `code-reviewer` (Opus)

`/code` then dispatches a **`code-reviewer`** (Opus) for each `ready-for-code-review` ticket. Because
the technical review now lives in its own agent — not the builder — **neither review of a branch is
done by its author** (the lander's semantic review is the other). The reviewer:

1. **Fetches `origin/land/<id>` and checks it out into its own launch worktree** — not `git -C`
   into the builder's worktree, and not `EnterWorktree`: the worktree-isolation guard refuses to run
   commands resolved into a path-entered worktree ("commands from a worktree-isolated agent must run
   inside its worktree"), and a launch worktree still at `trunk` HEAD has an empty diff against the
   builder's real branch, so driving the builder's worktree in place both fought a guard *and*
   produced an empty-diff review (lode-k5e). Checking the pushed branch out locally sidesteps both.
2. **Runs the technical review** — `/code-review --fix` (bugs) and `/simplify` (over-design /
   complexity) — then **re-gates**, keeping the last **green** commit; if a refinement breaks the gates
   unrecoverably or trades simplicity for complexity, it **reverts to green**.
3. **Re-pushes `land/<id>`** and **swaps the ticket to `ready-for-land`** (refreshed head SHA +
   summary), then stops. Its escalation rule mirrors the builder's: a genuine **decision**, or "I'm
   making it worse," reverts to green, swaps the label to `land-escalated`, and surfaces async —
   landing nothing.

```mermaid
flowchart TD
    INV["bare /code · /code --single · /code &lt;id&gt; … · /code --all-ready"] --> N{"one or many?"}
    N -->|"one"| ONE["1 builder"]
    N -->|"many"| FAN["N builders<br>(parallel · isolated worktrees)"]
    ONE --> BUILD["coding builder (Sonnet):<br>claim · build (simplest thing) ·<br>nox -t fix / nox -s tests green"]
    FAN --> BUILD
    BUILD --> BESC{"build-time<br>clarifying decision?"}
    BESC -->|"yes"| BHOLD["Revert to green · push ·<br>record review_worktree ·<br>land-escalated · surface async"]
    BESC -->|"no"| PUSH["git push -u origin land/&lt;id&gt; ·<br>mark ready-for-code-review<br>(worktree path · SHA) · KEEP worktree"]
    PUSH --> REV["code-reviewer (Opus):<br>fetch + checkout land/&lt;id&gt; into OWN worktree ·<br>/code-review + simplify --fix · re-gate"]
    REV --> RESC{"clarifying decision?<br>or making it worse?"}
    RESC -->|"yes"| RHOLD["Revert to green · re-push ·<br>land-escalated · surface async"]
    RESC -->|"no"| MARK["Swap to ready-for-land<br>(SHA · summary) · re-push land/&lt;id&gt; · STOP"]

    classDef start fill:#fcf8e3,stroke:#8a6d3b,color:#1b1b1b;
    classDef work fill:#d9edf7,stroke:#31708f,color:#1b1b1b;
    classDef gate fill:#fcf8e3,stroke:#8a6d3b,color:#1b1b1b;
    classDef bad fill:#f2dede,stroke:#a94442,color:#1b1b1b;
    classDef good fill:#dff0d8,stroke:#3c763d,color:#1b1b1b;
    class INV,N start;
    class ONE,FAN,BUILD,PUSH,REV work;
    class BESC,RESC gate;
    class BHOLD,RHOLD bad;
    class MARK good;
```

### The lander — `/land`, drained by a self-paced loop

A **single** `/land` lander owns every write to `trunk`, **run self-paced as `/loop 5m /land` on one
machine** — start it and it drains the `ready-for-land` queue in the background while you build; stop
it by ending the loop, and there's no daemon to manage. Run exactly **one** such loop at a time:
being the *single* lander is what serializes landing (the single-lander lock in
[Mechanics](#mechanics-decided) below keeps overlapping ticks from colliding). Guaranteeing only one
is active across machines is an open mechanism (see [decisions.md](decisions.md)). A drain pass:

1. **Semantic review — the first task.** For each ready-for-land branch, the `debate`-twin reviews it
   against the ticket (acceptance, scope, design, invariants, approach) and returns a verdict:
   **accept** → into the merge set; **bounce** (a clear failure it's confident about) → open a **new
   ticket carrying the findings, linked to the original** (the original is superseded), and drop the
   rejected branch; **ambiguous** (a genuine decision) → **escalate** to you, land nothing.
2. **Batch-merge the accepted set** `--no-ff` into `trunk`, then **re-gate once** on the combined
   result (two branches green in isolation can break *combined* — a clean git merge with broken
   behaviour). If green, proceed. If red, **isolate**: redo the merges one at a time to find the
   culprit, bounce it (→ new ticket, as above), and keep the survivors.
3. **Land the survivors:** push `trunk`, `bd close` the landed tickets, `bd dolt push`, delete the
   merged remote branches.

The re-validation that beads and git haven't drifted (branch still on origin, SHA matches) happens
before the merge; a drifted or missing branch is bounced like any other failure.

```mermaid
flowchart TD
    Q[("ready-for-land queue<br>(beads + remote branches)")] --> LAND["/land<br>(single lander · /loop 5m /land)"]
    LAND --> SEM["Semantic review — FIRST task<br>(debate-twin, per branch:<br>acceptance · scope · design · invariants)"]
    SEM --> V{"verdict"}
    V -->|"accept"| ACC["into merge set"]
    V -->|"bounce (clear fail)"| NEW["New ticket with findings ·<br>linked to original (superseded) ·<br>drop branch"]
    V -->|"ambiguous"| HUMAN["Escalate — needs a decision<br>(leave for human · land nothing)"]
    ACC --> MERGE["Batch-merge accepted set<br>--no-ff into trunk"]
    MERGE --> RG{"Re-gate once<br>on combined trunk"}
    RG -->|"green"| PUSH["push trunk · close landed ·<br>bd dolt push · delete merged branches"]
    RG -->|"red"| ISO["Isolate: redo per-ticket merge+gate ·<br>bounce the culprit (→ new ticket) ·<br>keep survivors"]
    ISO --> PUSH

    HUMAN --> RES{"human resolves<br>(outside a /land pass)"}
    RES -->|"land as-is"| MATFIX["Write the decision into the ticket ·<br>(branch unchanged) · swap to ready-for-land"]
    RES -->|"rebuild"| RSUPER["bd supersede → new ticket ·<br>drop branch"]
    RES -->|"drop"| RDROP["bd close (reason) ·<br>GC branch"]
    MATFIX -.->|"next /land pass<br>re-runs land-review"| SEM

    classDef start fill:#fcf8e3,stroke:#8a6d3b,color:#1b1b1b;
    classDef work fill:#d9edf7,stroke:#31708f,color:#1b1b1b;
    classDef gate fill:#fcf8e3,stroke:#8a6d3b,color:#1b1b1b;
    classDef bad fill:#f2dede,stroke:#a94442,color:#1b1b1b;
    classDef good fill:#dff0d8,stroke:#3c763d,color:#1b1b1b;
    class Q good;
    class LAND,SEM,ACC,MERGE,ISO work;
    class V,RG,RES gate;
    class NEW,HUMAN bad;
    class PUSH,MATFIX,RSUPER,RDROP good;
```

**Resolving `land-escalated`.** The label is not terminal — a human resolves it, outside a `/land`
pass, and every resolution **removes `land-escalated`** so a surfacer's queue can actually drain
instead of growing monotonically. Exactly three exits (full mechanics in
[`land/SKILL.md`](../.claude/skills/land/SKILL.md#resolving-a-land-escalated-branch)):

- **Land as-is** — the **branch is unchanged** (this exit is exactly the "it's fine as-is" case); the
  human **writes the decision into the ticket** (edits the acceptance criteria / description to remove
  the ambiguity `land-review` flagged), *then* swaps the label back to `ready-for-land`. A bare label
  swap with nothing else changed is **not** a valid transition: `/land`'s next pass just re-dispatches
  `land-review`, which hits the same ambiguity and escalates again. There is deliberately **no
  "human-blessed" bypass label** — `land-review` stays authoritative on re-review; forcing a land past
  its objection is an out-of-band manual act, not a designed fast-path.
- **Rebuild** — handled exactly like a `land-review` bounce: `bd supersede` the original onto a new
  ticket carrying the human's decision, and drop the branch.
- **Drop** — `bd close` the ticket with a reason, and GC the branch.

These resolve the label as `/land` sets it. `/code`'s producers set the same label for build-time,
technical-review, and rebase-conflict escalations: **rebuild** and **drop** apply to those unchanged,
while **land as-is** re-enters at the gate that escalated it rather than at `ready-for-land` —
`ready-for-code-review` for both a `code-reviewer` technical-review escalation and a `coding`
build-time clarification (the arguable case, decided: re-entry asserts the *ambiguity* is resolved,
not that the branch is finished — the `code-reviewer` can still escalate a half-built branch), and
`needs-rebase` for a `coding` rebase-pickup conflict; full mechanics and per-source table in
[`land/SKILL.md`](../.claude/skills/land/SKILL.md#exit-a-per-source--re-enter-at-the-gate-that-escalated)
(`lode-08g`).

**Making the `ready-for-code-review` re-entry executable (lode-08g's decision had two gaps; both
closed by lode-t83):** re-entering at `ready-for-code-review` is only a real re-entry if something
carries the hand-off and something else consumes the label —

1. A `coding` build-time escalation used to skip recording `review_worktree`/`review_branch`/
   `review_head` entirely (it only wrote that metadata in the green hand-off path). A re-entered
   ticket therefore had no worktree for `code-reviewer` to drive. **Fixed:** the build-time escalation
   now records those three metadata fields against the reverted-to-green commit at escalation time,
   even though it doesn't set the label itself — so the fields are already there the moment a human
   applies `ready-for-code-review`.
2. Neither `bd ready` (the ticket stays `in_progress`) nor `/code`'s Phase 2 (which only reviews a
   ticket its *own* Phase 1 just built) ever picked the re-entered ticket back up — it was worse off
   than a `land-escalated` ticket, which at least `/sweep` surfaces. **Fixed:** `/code`'s step-0
   sweep gained a sibling step that looks for `ready-for-code-review` + `in_progress` tickets the same
   way it looks for `needs-rebase`, and dispatches a `code-reviewer` at each — mirroring the
   `needs-rebase` sweep exactly, just one gate earlier in the pipeline.

### Delegated destructive git ops (lode-cln)

**Destructive git operations belong to the orchestrating session. A delegated subagent never
performs one.** lode holds this as a design rule on its own merits — it would be the right
architecture even if nothing else enforced it:

- **A single, identifiable owner for every destructive action**, rather than N delegated agents
  each independently capable of one.
- **The action happens in the session the human running the command is actually watching**, where
  they can see it — not buried inside a subagent's dispatch.
- **It's the same principle this repo already lives by and documents**: only `/land` writes
  `trunk`, and a `coding` producer never merges its own work (see
  [above](#the-landing-loop--build-review-land)). Here it's applied one level down — to a
  force-push instead of a merge.

Concretely, for a `/code` step-0 rebase pickup:

- `/code` step 0 dispatches the `coding` subagent to fetch `origin/land/<id>`, check it out in its
  own launch worktree, rebase onto `origin/trunk`, re-gate (`nox -t fix` / `nox -s tests`), commit,
  and **stop — without pushing.** It reports back its branch name and head SHA. Issuing the
  force-push from inside a delegated dispatch prompt is disallowed, so step 0 does not do that —
  the subagent's job, as designed, genuinely contains no destructive step.
- The orchestrating `/code` session runs the force-push itself, as a direct Bash call —
  `git push --force-with-lease origin <reported-branch>:land/<id>`. Every worktree under this repo
  shares one `.git` object store, so the orchestrator needs no `git -C` and never enters the
  subagent's worktree to do this. `/code` then refreshes the head-SHA metadata and swaps
  `needs-rebase` to `ready-for-land` itself, per-hit as each dispatched producer returns.

Relocating the push to the orchestrator is a relocation of responsibility, not a concealment of it —
the step moves to the session already accountable for the rest of `/code`'s writes; it isn't hidden
from anywhere it was previously visible.

**Unchanged:** a rebase conflict where the two sides genuinely disagree (not a mechanical,
independent-addition conflict) still escalates to a human. That boundary was never in question —
see [`.claude/skills/code/SKILL.md`](../.claude/skills/code/SKILL.md) step 0's dispatch prompt.
This also doesn't affect a **clean fan-out of fresh builds** (Phase 1) or **technical reviews**
(Phase 2) — only step 0's rebase-pickup force-push.

`.claude/settings.json`'s `autoMode.allow` list carries an entry permitting exactly this direct
`--force-with-lease` push to `land/*`. Two other entries exist alongside it — permitting
ticket-scoped edits to this repo's own agent/skill instruction docs, and permitting `/land`'s
deletion of already-merged `land/<id>` branches — and neither was re-verified by this ticket; treat
their effectiveness as still unconfirmed rather than assumed.

### Mechanics (decided)

- **Queue state is a label, not a status.** A built-but-unlanded ticket stays `in_progress` and moves
  through **two labels**: the builder sets **`ready-for-code-review`** (the code-reviewer's queue), and
  the reviewer swaps it to **`ready-for-land`** (the lander's queue). Escalations and bounces are their
  own labels (`land-escalated`, …), composing on top of the lifecycle status rather than replacing it.
  (A claimed ticket already drops out of `bd ready`, so a producer won't re-grab work waiting to review
  or land.)
- **Hand-off context is minimal — head SHA + a one-line summary** (small JSON in bd fields, read via
  `bd show --json`). The builder's worktree path is recorded too, but the code-reviewer no longer
  drives it: it fetches `origin/land/<id>` and checks the branch out into its **own** launch worktree
  instead, so the head SHA (`review_head`) is the field it actually depends on; the worktree path
  survives only for `/land`'s worktree GC (below). The lander re-reviews and re-gates, so stored
  gate-results would be decorative; the SHA exists only to detect drift (a push onto the branch
  *after* it was marked ready). The branch name isn't stored — it's derived (below).
- **Branches are `land/<ticket-id>` on origin** (`git push -u origin HEAD:land/<id>`) — derivable from
  the ticket, no opaque `worktree-agent-<hash>` refs on the remote. **GC:** delete `origin/land/<id>`
  on a successful land *or* a bounce (a rebuild gets a fresh `land/<new-id>`); keep it for an
  *escalated* ticket until the human resolves it, which always removes `land-escalated` — see
  "Resolving `land-escalated`" above (a sweep to surface long-abandoned escalations, rather than
  resolve them, is a later hygiene task).
  On a clean land the lander **also removes the builder's local worktree** (and its branch), keyed off
  the `review_worktree` metadata — best-effort and machine-local: the `git worktree list` guard skips a
  worktree that lives on another machine, so the build machine reclaims its own.
- **Single-lander lock (v1): a local "skip if already running" guard + the convention that the
  `/land` loop runs on one machine.** The guard stops a `/loop 5m /land` tick from overlapping a
  still-running land on the same machine; the one-machine convention covers cross-machine. A
  **distributed remote-lock ref** (atomic `refs/locks/land` on origin, owner + timestamp for
  stale-break) is the documented upgrade for true concurrent multi-machine landing — and the natural
  seam toward real CI.

### Where this is heading — a green-branch merge queue

This is, deliberately, a **merge queue**: producers open reviewed "ready" branches, a single lander
semantically reviews and drains the green ones into `trunk`. It is the first step toward a proper
**CI/CD** setup — the natural end state is the re-gate and merge moving to **real CI** (a service
that merges green, approved PRs), with `/land` the local-dev stand-in until then. v1 keeps the lander
a local agent so it stays simple and needs no external infrastructure. The open sub-choices this
design defers — the single-lander lock, the `ready-for-land` representation, the landing-context
schema, remote-branch naming and cleanup — are recorded in [decisions.md](decisions.md).
