# lode — Agent dev workflow

How lode is *built*. The other docs describe the system; this one describes the **agent
loops that produce it** — a **design loop** that stress-tests a plan before it's built, and a
**coding loop** that implements one task end-to-end in an isolated worktree (with a fan-out
variant, `/code-parallel`, for several independent tasks at once). A third stage — a **landing
loop** that decouples *landing* from *building* through a durable hand-off — is **designed but
not yet built**; it is the last section of this doc. See [design.md](design.md) for the thesis and
the build sequencing the work flows through.

The operational source of truth for each loop is its skill/agent definition under
[`.claude/`](../.claude); this doc is the map, not the mechanics. The hard project invariants live
in [`CLAUDE.md`](../CLAUDE.md) and [`AGENTS.md`](../AGENTS.md) — **where they and this doc disagree,
`CLAUDE.md` wins.**

---

## The two loops

Work moves through two distinct passes, with the human as the hinge between them:

1. **Design loop — `debate`.** Before anything is built, a plan, a beads ticket tree, a bug-fix
   approach, or a proposed `docs/` change is handed to the `debate` skill, which *pushes back*:
   it surfaces ambiguity, hidden assumptions, sequencing gaps, and risky approaches, and reports
   them to the human. It never edits `docs/` or beads as a side effect. The human revises until
   the plan is sound.
2. **Coding loop — `/code` → `coding`.** Once the plan is sound and captured as beads issues, the
   `/code` skill dispatches exactly one `coding` subagent to carry **one** task through its
   orderly cycle in an isolated worktree: claim → build → green gates → `--no-ff` merge → close
   → push.

The boundary between them is deliberate: **debate decides *what* and *whether*; coding decides
*how* and *does it*.** The two are **separate tasks** — each has its own diagram below. Design
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

## The coding loop — `/code` → `coding`

`/code` is the **only** sanctioned way to start coding work from the main session (which is
otherwise told not to spawn agents). The skill resolves the task from its argument and dispatches
**exactly one** `coding` subagent — in the foreground, with `isolation: "worktree"`, never
`run_in_background`. (Skill: [`.claude/skills/code/SKILL.md`](../.claude/skills/code/SKILL.md);
agent: [`.claude/agents/coding.md`](../.claude/agents/coding.md).)

Argument resolution:

- **A bd issue ID** (`lode-1a8`) → claim and implement that issue.
- **Free text** ("add a `--json` flag to search") → the agent files the bd issue itself, then codes.
- **No argument** → the agent picks the top unblocked item from `bd ready` — *the subagent chooses,
  not the dispatcher*, honoring the dependency frontier and the phase-a skeleton order.

The subagent then runs its orderly cycle. The worktree is **handed to it by the harness**
(`isolation: "worktree"`) — a subagent pinned at the repo root cannot create its own, so it begins
*already inside* `.claude/worktrees/agent-<hash>` on a branch off local `trunk` HEAD. It works
in-cwd with plain git, and if its `pwd` is ever the repo root it **stops and reports** rather than
writing on `trunk`. It merges back with `git -C <main-checkout>`; the main session stays on `trunk`
and never edits files there. The final agent message isn't shown to the user, so `/code` relays
what came back — which issue, that gates passed, that it merged and pushed, or exactly where it
stopped and why.

```mermaid
flowchart TD
    INV["Human: /code &lt;arg&gt;"] --> RES{"Resolve arg"}
    RES -->|"bd id"| T1["Claim that issue"]
    RES -->|"free text"| T2["Agent files the issue, then codes"]
    RES -->|"none"| T3["Agent picks top of bd ready<br>(dependency frontier · phase-a order)"]

    T1 --> DISP["Dispatch ONE coding subagent<br>(foreground · isolation: worktree)"]
    T2 --> DISP
    T3 --> DISP

    DISP --> WT["Starts ALREADY inside<br>.claude/worktrees/agent-&lt;hash&gt;<br>(branch off local trunk HEAD)"]
    WT --> GUARD{"pwd is repo root?"}
    GUARD -->|"yes"| BAIL["STOP & report —<br>never write on trunk"]
    GUARD -->|"no, in worktree"| CLAIM["claim (bd update --claim)"]

    CLAIM --> IMPL["Read issue + acceptance + design,<br>then implement (Typer · ./venv ·<br>simplest thing that works)"]
    IMPL --> GATES{"Quality gates"}
    GATES -->|"nox -t fix · nox -s tests ·<br>validate-mermaid (if diagram)"| GFAIL{"Pass?"}
    GFAIL -->|"no"| FIX["Fix & re-run —<br>do NOT merge on a failing gate"]
    FIX --> GATES
    GFAIL -->|"yes"| COMMIT["Commit in worktree<br>(Co-Authored-By trailer)"]

    COMMIT --> CLOSE["bd close --suggest-next"]
    CLOSE --> MERGE["git -C main: commit passive .beads export,<br>then merge --no-ff &lt;branch&gt;"]
    MERGE --> PUSH["git -C main: pull --rebase · push ·<br>status = 'up to date' · bd dolt push"]
    PUSH --> DONE["Worktree auto-removed on exit;<br>/code relays result to human"]

    classDef start fill:#fcf8e3,stroke:#8a6d3b,color:#1b1b1b;
    classDef work fill:#d9edf7,stroke:#31708f,color:#1b1b1b;
    classDef gate fill:#fcf8e3,stroke:#8a6d3b,color:#1b1b1b;
    classDef bad fill:#f2dede,stroke:#a94442,color:#1b1b1b;
    classDef good fill:#dff0d8,stroke:#3c763d,color:#1b1b1b;
    class INV,RES start;
    class T1,T2,T3,DISP,WT,CLAIM,IMPL,COMMIT,CLOSE,MERGE,PUSH work;
    class GATES,GFAIL,GUARD gate;
    class BAIL,FIX bad;
    class DONE good;
```

### Invariants the coding loop never breaks

A quick card; the full list is in [`.claude/agents/coding.md`](../.claude/agents/coding.md) and
[`CLAUDE.md`](../CLAUDE.md).

| Thing | Rule |
|---|---|
| Default branch | `trunk` — **never** edit directly; every change goes through a worktree |
| Worktrees | harness-made (`isolation: "worktree"`) under `.claude/worktrees/`, branched from local `trunk` HEAD, merged `--no-ff`, auto-removed on exit |
| Task tracker | **bd only** — no TodoWrite, no markdown checklists; file an issue *before* non-trivial work |
| Design decisions | doc edits under `docs/`, never a bd note or memory (that forks the record) |
| Gates | `nox -t fix`, `nox -s tests`; `scripts/validate-mermaid.sh` for diagram changes — no merge on a failing gate |
| CLI framework | **Typer** (never argparse); venv at `./venv` |
| Done | not done until `git push` *and* `bd dolt push` succeed |

---

## How the loops connect

The two loops share one substrate: **`docs/` and beads are the source of truth between them.**
Debate reads that substrate and argues against the plan; the human folds the criticisms back into
the docs and the ticket tree; the coding loop then reads the settled docs and the claimed issue and
writes code to them. Nothing skips the middle — a design decision that exists only in a chat
transcript, a bd note, or memory has *forked the record*, and the next loop will trust the docs and
miss it. Keep the decisions in the docs, and the two loops stay in agreement.

---

## The landing loop — durable land hand-off (planned)

> **Status: designed, not yet built.** Today both `/code` and `/code-parallel` *land in the same
> session that built the work* — the coding agent (or the `/code-parallel` orchestrator) merges
> `--no-ff` into `trunk`, pushes, and closes the issue itself. The landing loop replaces that
> in-session land with a **durable hand-off**; the sections above describe the current behaviour,
> this one the intended evolution.

### Why decouple landing from building

In-session landing has two limits that show up the moment work goes parallel or spans more than one
sitting:

- **It isn't durable.** The build→land hand-off lives in the orchestrator's context. If that session
  compacts, crashes, or is closed between "branch is green" and "branch is merged," the work is
  stranded on a branch nobody lands.
- **It doesn't cross machines.** Landing in-session ties the merge to the machine that did the build.
  Development here happens across **multiple machines**, and a local worktree branch on machine A is
  invisible to a lander on machine B.

The fix is to make "this branch is built and green, ready to merge" a **durable fact** — recorded in
beads, with the branch itself living somewhere both machines can see — and to let a **separate
lander** act on it whenever and wherever it next runs.

### The producers — build a green branch, mark it ready, stop

A code agent (solo `/code` or one of a `/code-parallel` fan-out) runs its build cycle as today —
claim → build → `nox -t fix` / `nox -s tests` green — but instead of merging it:

1. **Pushes its branch to the remote** (`git push -u origin <branch>`). The durable artifact is a
   **remote branch**, not a local worktree branch: it survives the worktree being auto-removed on
   session exit, and it is reachable from any machine. (Pushing a *new* branch ref does not race
   `trunk` the way pushing `trunk` does, so parallel producers stay safe.)
2. **Marks the ticket `ready-for-land`** and attaches the **landing context** — remote branch name,
   head SHA, the gate results, and a one-line summary — then **stops**. It does *not* merge, close,
   push `trunk`, or touch the main checkout.

### The lander — `/land`, drained by a self-paced loop

A single **`/land` merge agent** owns every write to shared state. It is run as a **self-paced
loop** — `/loop 5m /land` — so it drains the queue periodically while you work, with no daemon to
manage. Being the *single* lander is what serializes landing: only one process ever touches `trunk`,
so the index race that makes naive parallel landing flaky cannot happen.

For each `ready-for-land` ticket, one at a time, it:

1. **Re-validates — never trusts the ticket.** beads state and git state are separate stores that can
   drift, so it confirms the remote branch still exists and its head SHA matches the landing context
   before doing anything.
2. **Merges `--no-ff`** `origin/<branch>` into `trunk` in the main checkout.
3. **Re-runs the gates on the merged result.** Gates were green against an *older* `trunk`; two
   branches that each passed in isolation can break *combined* (a clean git merge with broken
   behaviour). Re-running `nox -t fix` / `nox -s tests` after the merge, before pushing, is what
   makes a deferred land trustworthy.
4. On success: **push `trunk`, `bd close`, `bd dolt push`, delete the remote branch.** On any failure
   (branch missing, SHA drift, conflict, gates red): **bounce the ticket** — flag it land-failed with
   the reason and leave the remote branch intact for follow-up — and move to the next. One bad branch
   never blocks the others.

```mermaid
flowchart TD
    subgraph PROD["Producers — build green branches"]
        P1["/code<br>(one task)"]
        P2["/code-parallel<br>(fan-out · N tasks)"]
    end
    P1 --> BUILD["coding agent:<br>claim · build · gates green ·<br>git push -u origin &lt;branch&gt;"]
    P2 --> BUILD
    BUILD --> MARK["Mark ticket ready-for-land<br>+ landing context<br>(remote branch · head SHA ·<br>gate results · summary)"]
    MARK --> Q[("ready-for-land queue<br>(durable: beads + remote branch)")]

    Q --> LAND["/land merge agent<br>(self-paced: /loop 5m /land)"]
    LAND --> ONE["Take ONE ticket<br>(single lander · serial ·<br>no trunk-index race)"]
    ONE --> VAL{"Re-validate:<br>branch on origin? SHA matches?"}
    VAL -->|"no"| BOUNCE["Bounce ticket<br>(land-failed + reason);<br>keep remote branch"]
    VAL -->|"yes"| MERGE["git -C main:<br>merge --no-ff origin/&lt;branch&gt;"]
    MERGE --> REGATE{"Re-run gates on merged trunk<br>(catch stale-trunk breakage)"}
    REGATE -->|"fail / conflict"| BOUNCE
    REGATE -->|"pass"| PUSH["push trunk · bd close ·<br>bd dolt push · delete remote branch"]
    PUSH --> NEXT["Next ready-for-land ticket"]
    NEXT -.-> ONE
    BOUNCE -.-> NEXT

    classDef start fill:#fcf8e3,stroke:#8a6d3b,color:#1b1b1b;
    classDef work fill:#d9edf7,stroke:#31708f,color:#1b1b1b;
    classDef gate fill:#fcf8e3,stroke:#8a6d3b,color:#1b1b1b;
    classDef bad fill:#f2dede,stroke:#a94442,color:#1b1b1b;
    classDef good fill:#dff0d8,stroke:#3c763d,color:#1b1b1b;
    class P1,P2 start;
    class BUILD,MARK,LAND,ONE,MERGE,PUSH,NEXT work;
    class VAL,REGATE gate;
    class BOUNCE bad;
    class Q good;
```

### Where this is heading — a green-branch merge queue

This is, deliberately, a **merge queue**: producers open "ready" branches, a single lander drains
the green ones into `trunk`. That is the first step on the path toward a proper **CI/CD** setup —
the natural end state is the re-validation and merge moving to **real CI** (e.g. a service that
merges green PRs), with `/land` being the local-dev stand-in until then. We are building toward it
on purpose; v1 keeps the lander a local agent so it stays simple and needs no external
infrastructure. The open sub-choices that this design defers are recorded in
[decisions.md](decisions.md).
