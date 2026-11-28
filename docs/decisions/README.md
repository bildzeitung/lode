# Decision entries

One file per entry: `docs/decisions/<id>-<slug>.md`, `<id>` a random 5-char lowercase base36
string, cited as `d-<id>`. It opens with this frontmatter, between two `---` lines:

    id: <id>
    date: YYYY-MM-DD
    status: open | decided | superseded
    supersedes: [<id>, ...]
    related: [<id>, ...]

`id` is the filename's id; edges are inline lists of ids, never slugs, on the NEW entry only, and
`superseded_by` is derived at index time, never stored. A committed entry file is never edited or
deleted. A correction, whole or partial, is a new entry that is a full copy of the old one with the
corrected portion changed and `supersedes: [<old id>]`. Gate: `scripts/check_decisions_entries.py`.

## Deriving `status` in the migration

`scripts/migrate_decisions.py` derives `status` from the entry's bold title and its first paragraph
(maintainer ruling on lode-m4rzt, option A). Deferred, leaning, revisit-later, "must be revisited",
"tune when" and "decide whether" wording makes an entry `open`: a wrong `decided` cannot be corrected
after cutover (a committed entry is never edited), a wrong `open` costs one later close. Wording
anywhere in the title counts; wording only in the first paragraph is overridden by a decided title
(`DECIDED`, `settled`, `accepted`...). "Left open" mid-title reports a past state and is ignored. An
Update that reads as a ruling turns an open entry `decided`. The dry-run report lists every entry
whose status differs from what its title alone gives (TITLE DISAGREEMENTS) for a hand check.
