# Decision entries
One file per entry: `docs/decisions/<id>-<slug>.md`, `<id>` a random 5-char lowercase base36
string, cited as `d-<id>`. It opens with this frontmatter, between two `---` lines:

    id: <id>
    date: YYYY-MM-DD
    status: open | decided
    supersedes: [<id>, ...]
    related: [<id>, ...]

`id` is the filename's id; edges are inline lists of ids, never slugs, on the NEW entry only;
`superseded_by` and superseded status are derived, not stored. A committed entry is never edited or
deleted. A correction, whole or partial, is a new entry: a full copy of the old with the change and
`supersedes: [<old id>]`; no entry is superseded twice. Gate: `scripts/check_decisions_entries.py`.
Chain head: `grep -rl 'supersedes: \[.*<id>' docs/decisions/`, repeated on each hit's id.
