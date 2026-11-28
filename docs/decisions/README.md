# Decision entries

One file per entry: `docs/decisions/<id>-<slug>.md`, `<id>` a random 5-char lowercase base36
string, cited as `d-<id>`. It opens with this frontmatter, between two `---` lines:

    id: <id>
    date: YYYY-MM-DD
    status: open | decided | superseded
    supersedes: [<id>, ...]
    related: [<id>, ...]

`id` is the filename's id; edges are inline lists of ids, never slugs, on the NEW entry only, and
`superseded_by` is derived, never stored. A committed entry file is never edited or
deleted. A correction, whole or partial, is a new entry that is a full copy of the old one with the
corrected portion changed and `supersedes: [<old id>]`. Gate: `scripts/check_decisions_entries.py`.
