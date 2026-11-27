# lode — docs lookup index

The single normative statement of the docs-index guidance. This file is `@import`ed by
[`CLAUDE.md`](../CLAUDE.md), so its contents are mechanically inlined into the main session **and**
every non-fork subagent — the same mechanism [`docs/conventions.md`](conventions.md) uses for style
fiats. Keep it tight; it loads into every session.

**Answering "what did we decide about X" starts with the docs-index tool, not with reading a doc.**
`docs/decisions.md` alone is 400K+ and cannot be pulled into context, and the same lookup cost
applies across every design doc under `docs/`. Run the on-demand FTS5 lookup CLI first — it rebuilds
a never-tracked index over all of `docs/*.md` on every invocation:

```bash
uv run --frozen python scripts/docs_index_query.py "<search terms — a bd id, a phrase, anything>"
```

**Output contract:** ranked `path:line_lo-line_hi` pointers with a short snippet — never a whole doc,
never a synthesized answer.

**AND→OR fallback (`lode-qcp0`):** a multi-term query first runs an implicit-AND FTS5 `MATCH` (every
term must land in the same chunk); when that returns nothing, the index retries with `OR` semantics
over the same terms and marks the printed rows `[fallback: OR match]` instead of printing "No
results." (A transcript review found the AND-only mode missed roughly half of real multi-term
queries.)

**Split-entry `NOTE` (`lode-funp`, `lode-8cp63`):** an entry too long for one chunk, or a `##`
section of `decisions.md` that ends in a bullet list, is split across several, so a hit can show a
claim whose `**Update (` correction sits in a different chunk. Such a hit is followed by a `NOTE`
line giving the line numbers of the Update markers that may correct it — read those lines too.

**Zero-hit bd id (`lode-fg7w7.1`):** when the query is one bare bd id and nothing matches, a line
after `No results.` says why, and in each case searching `docs/` further for that id is wasted: the
id has an entry in `origin/trunk`'s docs that this checkout predates (the line gives the `git grep`
that prints it), or it exists in bd with no docs entry, or it is not in the local bd database. When
either check fails or times out, `No results.` is printed alone.

Read the cited range yourself once you have the pointer. Reach for a doc directly only when the
index turns up nothing relevant.
