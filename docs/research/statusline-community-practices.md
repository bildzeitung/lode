# Claude Code statusline — community practices (research)

_Researched 2026-07-18 · deep-research harness · 16 sources fetched · 77 claims
extracted → 25 adversarially verified (3-vote) → 23 confirmed, 2 refuted._

This is **reference research**, not a lode design fiat — it captures what the
Claude Code community puts in custom status lines and why, to inform (not
dictate) lode's own fleet statusline (`.claude/statusline.sh`). Settled lode
design decisions still belong in [`docs/`](../design.md), not here.

Grounded primarily in the official Anthropic doc
([code.claude.com/docs/en/statusline](https://code.claude.com/docs/en/statusline)),
corroborated by real user configs and the leading community projects.

---

## Summary

Claude Code's `statusLine` command receives a rich JSON payload on stdin, and the
community has converged on a stable set of high-value segments built from it. The
near-universal **core five**: current **model**, **git branch + dirty**,
**context budget**, **session cost**, and **directory/worktree** — with "anything
beyond those five is decoration."

The single most-built, most-praised segment is a **colour-coded context-occupancy
meter** (bar + %), because Claude Code pipes a pre-calculated
`context_window.used_percentage` plus a 1M-aware `context_window_size` and an
`exceeds_200k_tokens` flag — no token math needed. **Rate-limit segments** are the
other major theme: stdin exposes `rate_limits.five_hour` / `.seven_day`
(`used_percentage` + `resets_at` epoch) for Pro/Max subscribers, rendered as usage
% plus reset countdowns.

**Where lode stands:** lode's fleet line already covers model class, git
branch+dirty+divergence, a 5h rate-limit gradient meter (now with a `↻` reset
countdown), and a context-occupancy gradient meter with an 80% `/compact` hint —
at or ahead of community practice on the context meter. The community-validated
gaps are: a 7-day rate-limit window, session cost/spend, and lines added/removed —
**most of which are per-session and clash with lode's deliberate "honest fleet
aggregate, never per-session claims" stance.**

---

## Prioritized segment ideas (with lode fit)

Ranked by value × fit with lode's machine-global fleet philosophy.

| # | Segment | Why valued | lode status / fit |
|---|---------|-----------|-------------------|
| 1 | **Context-occupancy meter** (bar + %, colour-coded) | The most-built segment; every major project ships it with warning thresholds. | ✅ **Have it** (gradient meter + 80% `/compact` hint). At/ahead of community. |
| 2 | **Model class** | Called "non-negotiable." | ✅ Have it (first word of `display_name`). |
| 3 | **Git branch + dirty (+ divergence)** | Core-five. | ✅ Have it, incl. ahead/behind vs merge target. |
| 4 | **5h rate-limit meter + reset countdown** | Standard in ccstatusline / claude-powerline / ccusage; `resets_at` gives the countdown. | ✅ Meter + `↻` countdown (added 2026-07-18). |
| 5 | **7-day rate-limit window (+ reset)** | Leading projects show *both* windows. Account-global → **fits fleet philosophy.** | ❌ Gap. Considered and **declined** (2026-07-18) — not sold on the value. Cheap to add later from `rate_limits.seven_day`. |
| 6 | **Session cost `$` / today / burn-rate `$/hr`** | ccusage's whole focus; core-five member. | ❌ Gap, but **per-session** → clashes with lode's no-per-session-claims rule. Skip unless opt-in. |
| 7 | **Lines added/removed** (`+42 -7`) | Cheap, common line-2 git segment. | ❌ Gap, per-session. Low-cost but same philosophy tension. |
| 8 | **Directory / worktree name** | Core-five. `workspace.current_dir`, `worktree.name`. | ~ lode shows a live-agent-worktree *aggregate* instead of a single cwd — deliberate. |
| 9 | **Reasoning-effort glyph** (`○◐●` / `Ⓐ`) | Appears in ccusage + photostructure. | ❌ Minor. Not obviously worth it. |
| 10 | **Compaction counter** (ccstatusline) | Counts `compact_boundary` markers / tokens reclaimed. | ❌ Novel; unproven value in a fleet view. |
| 11 | **Token throughput** (input/output tok/s) | ccstatusline widget. | ❌ Novel; likely noise in orchestration. |

---

## Auto-compact threshold findings

**There is no Anthropic-published numeric auto-compact trigger.** This is the
softest area of the research:

- `exceeds_200k_tokens` is a **fixed 200k flag**, unrelated to the actual window
  size.
- Community *estimates* of the real fire point **disagree**: one source says
  **~77%**, another **~83.5%** (modeling a fixed **~33k-token reserved buffer** on
  the 200k window). `claude-powerline` uses a configurable `autocompactBuffer`
  defaulting to **33000 tokens** — its own heuristic, not an Anthropic number.
- Implication for lode: an **80% flat `/compact` hint is a defensible convention,
  not a documented boundary.** On **1M-context** models a flat 80% = 800k tokens,
  which is *very early* if the true trigger is a fixed buffer below the window
  (~96%). **Decision (2026-07-18):** keep the flat 80% — the hint means "you're
  getting full, wrap up," not "compaction is imminent," so window-size-awareness
  was intentionally declined.

---

## Community projects

| Project | Notes |
|---------|-------|
| **[ccstatusline](https://github.com/sirmalloc/ccstatusline)** (sirmalloc) | Flagship: 11.8k★ / 518 forks, `npx`/`bunx` runnable. Widgets: context %, usable context %, window size, context bar, token counts, token I/O speeds, compaction counter, 1M-context detection. Reference implementation to mine. |
| **[claude-powerline](https://github.com/Owloops/claude-powerline)** (Owloops) | Powerline styling. "Block" segment (5h billing window + reset countdown) + distinct "Weekly" (7-day) segment. `autocompactBuffer` (default 33000) drives a "usable %". |
| **[ccusage](https://ccusage.com/guide/statusline)** | Cost/burn-focused. Packs 6 segments: model+effort, session cost, today's cost, 5h block cost + time left, `$/hr` burn rate, context %. Example: `🤖 Fable 5 (high) \| 💰 $0.23 session / $1.23 today / $0.45 block (2h 45m left) \| 🔥 $0.12/hr \| 🧠 25,000 (12%)`. (Its `$` are ccusage estimates, may differ from stdin `cost.total_cost_usd`.) |
| **[TheoBrigitte/claude-statusline](https://github.com/TheoBrigitte/claude-statusline)** | Shows context three ways at once (bar `###------`, SI tokens `42k/200k`, `5%`). Independent `[rate_limit_5h]` / `[rate_limit_7d]` modules with usage %, reset countdown, warn/critical thresholds. |

---

## stdin JSON schema (what's achievable)

Fields Claude Code pipes to the `statusLine` command on stdin (confirmed by the
official doc + 6 corroborating sources; 9 verifier votes, all 3-0):

- `model.id`, `model.display_name`
- `workspace.current_dir` (== `cwd`), `workspace.project_dir`
- `context_window.{used_percentage, remaining_percentage, context_window_size,
  total_input_tokens, total_output_tokens, current_usage}`
  - `context_window_size` = **200000** default, **1000000** for extended-context
    models.
  - `used_percentage` is **pre-calculated** — docs Best Practices: "Use
    `used_percentage` for the simplest accurate context state."
- `exceeds_200k_tokens` (bool, fixed 200k threshold)
- `cost.{total_cost_usd, total_lines_added, total_lines_removed, total_duration_ms}`
- `rate_limits.{five_hour, seven_day}` → each `{used_percentage (0–100),
  resets_at (Unix epoch)}`. **Pro/Max only, after the first API response**; each
  window may be **independently absent**.
- `worktree.name`, `output_style.name`, `transcript_path`, `version`, `session_id`

---

## Caveats / open questions

**Caveats:**

- Field semantics have shifted: `context_window` added ~v2.1.132; **post-v2.1.132,
  `total_input_tokens`/`total_output_tokens` reflect *current* context, not
  cumulative session totals** (this is what lode wants for occupancy).
- `used_percentage` can be **`null`** early in a session or right after `/compact`
  — consumers must handle it (`jq '// empty'`). lode keeps a token-ratio compute
  with a `used_percentage` fallback.
- `rate_limits` can be **absent/malformed** on some plans or intermittently
  (GitHub issues #40094, #45133, #52326).
- "Most-praised" rankings come from individual blogs (editorial opinion, not usage
  telemetry) — treat exact ranking as directional; the field schema is
  high-confidence primary.
- ccusage `$` figures are its own estimates, may diverge from stdin
  `cost.total_cost_usd`.

**Refuted claims** (killed 0-3 by verifiers, not relied on):

- That `rate_limits` was added in Claude Code v1.2.80.
- A specific "primary motivation" framing for the context segment.

**Open questions:**

1. Claude Code's **actual** auto-compact trigger point — no source documents a
   number; heuristics only (33k buffer, 77–83.5%). Would need empirical
   measurement to fire lode's hint at the real boundary.
2. Do `worktree.name` / `rate_limits` populate reliably in lode's fleet/headless
   agent invocations (parallel worktrees, possibly non-Pro/Max or automated)? Docs
   scope `rate_limits` to interactive Pro/Max after first API response.
3. Would per-agent cost/burn tracking be meaningful at the *fleet* level, or only
   per-session? Aggregating spend across concurrent worktrees needs summing
   outside the per-invocation stdin.
4. Token-throughput / compaction-counter utility in a multi-agent orchestration
   line (vs a single interactive session) is unestablished.

---

## Sources

Primary (authoritative):

- [code.claude.com/docs/en/statusline](https://code.claude.com/docs/en/statusline) — official Anthropic doc
- [github.com/sirmalloc/ccstatusline](https://github.com/sirmalloc/ccstatusline)
- [github.com/Owloops/claude-powerline](https://github.com/Owloops/claude-powerline)
- [github.com/TheoBrigitte/claude-statusline](https://github.com/TheoBrigitte/claude-statusline)

Secondary / community:

- [ccusage.com/guide/statusline](https://ccusage.com/guide/statusline)
- [claudedirectory.org/blog/claude-code-statusline-guide](https://www.claudedirectory.org/blog/claude-code-statusline-guide)
- [claudefa.st/blog/tools/statusline-guide](https://claudefa.st/blog/tools/statusline-guide)
- [photostructure.com/coding/claude-code-statusline](https://photostructure.com/coding/claude-code-statusline/)
- [dandoescode.com/blog/claude-code-custom-statusline](https://www.dandoescode.com/blog/claude-code-custom-statusline)
- [felipeelias.github.io/2026/03/17/claude-statusline.html](https://felipeelias.github.io/2026/03/17/claude-statusline.html)
- [github.com/levz0r/claude-code-statusline](https://github.com/levz0r/claude-code-statusline)
- [zenn.dev — claude-code-context-warning](https://zenn.dev/trust_delta/articles/claude-code-context-warning-001?locale=en)
- [lalatenduswain — "Context left until auto-compact"](https://lalatenduswain.medium.com/understanding-context-left-until-auto-compact-0-in-claude-cli-b7f6e43a62dc)
- Gists: [AKCodez](https://gist.github.com/AKCodez/ffb420ba6a7662b5c3dda2edce7783de), [plribeiro3000](https://gist.github.com/plribeiro3000/17354a5214a97f59c8fef9e37b30c87e)
