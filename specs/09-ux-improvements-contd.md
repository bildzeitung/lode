# Feedback on current work

This document presents a list of feedback items. 

Create a P0 epic to track the items in this spec.

Evaluate each item and,

- ask to disambiguate any concerns / uncertainties
- file tickets as needed under the new epic

## Browse help bar

- the bar is too long and needs a more compact representation
- consider, e.g. [d]elete [i]nspect E[x]pand

## Config screen

- (bug) list of scolling config items scrolls beyond the bottom of the window

## CLI config

- (bug) there is too much whitespace in the table; I suspect it's not being geared towards the terminal width and using tabs. It should use spaces instead and be sized according to term width.

## CLI notes

- it is a little difficult to read. Can you colour the ID and date? And add a blank line between notes?

## CLI status

- consider adding some colour coding
- the `status` does note indicate if there are any actions to be taken (e.g. need to run `lode work`). Beneath the table, if there *are* user-actionable steps that should be taken, say so.

## CLI --help

- the user does not need to see the epic a feature came from
- referring to a doc is ok, but workflow text is superfluous
- go through each command help text and edit for brevity and clarity

## CLI dump-html

- i would like an --all option that does the dump for each note
- i would like a --file option that creates and <id>.dmp file (if there is something to dump) instead
- to be clear, `lode dump-html --all --file` would create a series of N files where N is the number of notes in the system

## Tags screen

- the tags are all in one column; we could use space more efficiently if they were in columns
- since there are a lot of tags, add the abiltity to page through them

