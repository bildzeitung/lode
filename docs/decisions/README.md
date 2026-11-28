# Decision entries

One file per entry: `docs/decisions/<id>-<slug>.md`, `<id>` a random 5-char lowercase base36
string, cited as `d-<id>`. Frontmatter:

    id: <id>                          # must equal the filename's id
    date: YYYY-MM-DD
    status: open | decided | superseded
    supersedes: [<id>, ...]           # ids only, never slugs
    related: [<id>, ...]

Edges are declared on the NEW entry only; `superseded_by` is derived at index time, never stored.
A committed entry file is never edited or deleted. A correction, whole or partial, is a new entry
that is a full copy of the old one with the corrected portion changed and `supersedes: [<old id>]`.
`nox -t everything-else` runs `scripts/check_decisions_entries.py` over these rules.
