# Feedback on current work

This document presents a list of feedback items. 

Create a P0 epic to track the items in this spec.

Evaluate each item and,

- ask to disambiguate any concerns / uncertainties
- file tickets as needed under the new epic

## Browse screen

- selecting a note, then returning to the browse screen puts the selection back to the top of the list. The selection should remain selected upon return

- I have discovered that I rarely want to view a note from the browse screen -- I'm using it to get to notes I want to edit. To that end, selecting a note should go the edit pane (what is now invoked by 'e') instead of the view-only. The view-only path can be removed. 

- summaries are too long; they need to be capped at two lines

- I would like a progressive search feature. When I press forward-slash, a one-line input box appears at the bottom of the screen, and as I type, the note selection will jump to the closest fit (descending from current location). Pressing Shift-/ should do the same, but ascending the list.

- note time is presented in UTC, but should be displayed in local time

## Tag view

- there is no way to see / select / search / use the tag information
- create a Tags screen that splits into top and bottom panels. The top panel compactly lists the tags, allows for a multi-select of them. The bottom panel is the notes list, where only notes that have the selected tags appear. That is, the tag selection is a filter against the list of notes.

## Surfacing more retrieved data

- the jobs claim to have retrieved HTML pages, but how can I know?
- propose different ways the note edit screen can surface viewing the content retrieved
- also, propose a CLI option so that the HTML content can be dumped out for a given item for a given note

## Edit / New Note screen

- the related notes feature is interesting, but needs to be more interactive
- consider a control that allows stepping through the related notes and bringing up a modal overlay that shows the related note with the context highlighted

## lode work

- the verbosity on the logging is different between `lode work` and `lode work --wait`, it should be the same; the `--wait` log is more verbose, and identifies a weird loop situation that I am encountering (details TBD)

