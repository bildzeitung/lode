# lode — How-to guides

Task-oriented walkthroughs: *"how do I do X?"* Each guide is a short, do-this-then-that
recipe for one concrete task.

These are deliberately **separate from the reference docs**. When you want the full,
authoritative catalogue — every knob, every default, every kind tag — read
[`configuration.md`](../configuration.md); it is long precisely because it is exhaustive.
When you just want to *get a thing done*, start here.

| Guide | Answers |
|---|---|
| [config-change.md](config-change.md) | How do I change a config setting? Where's the file, what's the format, how do I check it took? |
| [jira-setup.md](jira-setup.md) | How do I set up the JIRA (and Confluence) Cloud integration end to end? |

For standing lode up from a fresh clone (install, venv, issue DB, test suite), see
[`onboarding.md`](../onboarding.md) — that's the install how-to and lives one level up
because it predates this directory and is linked widely.

> **Convention.** One task per file, kebab-case filename. A guide *shows how*; it does
> not *decide* anything — settled architecture goes in the relevant design doc, open
> questions in [`decisions.md`](../decisions.md), every tunable in
> [`configuration.md`](../configuration.md). If a how-to starts explaining *why*, that
> paragraph belongs in a design doc with a link back to it.
