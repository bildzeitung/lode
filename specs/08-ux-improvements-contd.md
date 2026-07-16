# Feedback on current work

This document presents a list of feedback items. 

Create a P0 epic to track the items in this spec.

Evaluate each item and,

- ask to disambiguate any concerns / uncertainties
- file tickets as needed under the new epic

## UX keybindings

- I do not like the use of function keys; the laptops I'm on make this a chord and it's annoying
- set a policy to *not* use function keys
- re-map all function key uses to key or Ctrl-key, depending on whether it's on browse or edit

## Browse Search bar

- (bug) the search bar is not visible if the list of notes is longer than a screen

## Browse screen

- summaries are still too long; cap them at 1 line
- consider an 'expand' keybind that toggles displaying the full summary
- consider the effects on summary generation -- that the lede needs to be in the first line

## Config screen

- I want to widen the contract for the config screen
- right now, it shows file locations
- what I would like it to do is show _all possible user runtime knobs_ and their current values, so even if the user doesn't have a `config.yaml` created, they have visibility into what key/value pairs can be set
- since this is a longer list, present it in a table

