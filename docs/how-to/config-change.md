# How to change a config setting

> Every tunable knob and its default is catalogued in
> [`configuration.md`](../configuration.md). This guide is only *how to change one*.

## TL;DR

1. Find the knob's **exact field name** in [`configuration.md`](../configuration.md).
2. Put it in `$LODE_HOME/config.toml` (default `~/.lode/config.toml`) as a **flat**
   `key = value` line — no `[section]` headers.
3. Run `lode config` to confirm the resolved value is what you expect.

## 1. Find the knob name

Config keys are the `Settings` field names from
[`src/lode/config.py`](../../src/lode/config.py), one per row in the
[`configuration.md`](../configuration.md) tables (e.g. `refresh_ttl_s`,
`no_egress_default`, `qa_llm`). Use the name **verbatim** — the file is validated with
`extra="forbid"`, so a typo'd key is a hard error, not a silently ignored line.

## 2. Edit `config.toml`

The file lives under the single on-disk root, `$LODE_HOME` (default `~/.lode`). It's
**optional** — if it doesn't exist, every knob uses its default, which is a fully
working state. Create it if it's not there:

```toml
# ~/.lode/config.toml
# A FLAT TOML table. Keys are Settings field names. No [section] headers —
# Settings itself is flat, so a [section] header is an unrecognized key and fails.

refresh_ttl_s     = 1800
no_egress_default = true
qa_llm            = "claude-sonnet-4-6"
```

Types follow TOML: bare numbers for ints/floats, `true`/`false` for bools, quoted
strings, and `[ "a", "b" ]` arrays for list-valued knobs (e.g. the redaction-pattern
lists).

## 3. Verify it took

```bash
lode config
```

`lode config` prints the resolved on-disk paths and then a **knob table** showing every
runtime/tune setting's *currently resolved* value — defaults, with your `config.toml`
layered on top. It also tells you whether a `config.toml` was found. If your new value
shows up in that table, it's live.

## Load order (what wins)

`lode.config.load_settings()` resolves settings in this order, lowest to highest
precedence:

1. The field **defaults** declared in `config.py`.
2. Your **`config.toml`**, if present.
3. Explicit **caller overrides** (a test fixture; there is no per-knob CLI flag or env
   var today — only `LODE_HOME` / `LODE_LOG_LEVEL` are env-var knobs, plus the connector
   credentials in [jira-setup.md](jira-setup.md)).

So `config.toml` beats a default, and an explicit override beats `config.toml`.

## When you get it wrong

The file is **validated on load** — a bad config fails immediately rather than silently
running at defaults:

- A TOML **syntax error** → `TOMLDecodeError`.
- An **unrecognized key** (typo, or a `[section]` header) → `pydantic` `ValidationError`
  (`extra="forbid"`).
- An **out-of-range or wrong-typed value** → `ValidationError` from that field's
  validator (e.g. a malformed base URL, a non-compiling redaction regex).

The CLI catches all of these at its boundary and prints a one-line
`invalid config file <path>: …` on stderr with exit 1 — not a traceback.

## Notes

- **Secrets are never echoed.** Knobs marked `secret=True` (the Atlassian API tokens) are
  excluded from the `lode config` knob table by construction. `lode config` will *not*
  confirm a token value back to you — that's deliberate, not a bug. See
  [jira-setup.md](jira-setup.md).
- **Relocating everything at once:** point `LODE_HOME` at a different directory and the
  DB, vector store, logs, lock, *and* `config.toml` all move with it — one root, one
  `cp -r` to back up.
