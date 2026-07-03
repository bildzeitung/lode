# Feedback on work up to E11

This document presents a list of feedback items. 

Create a P0 epic to track the items in this spec.

Evaluate each item and,

- ask to disambiguate any concerns / uncertainties
- file tickets as needed under the new epic

## Too easy to lose work

- exiting quickly via `esc` results in losing work (my muscle memory is for `vi`, so this happens too often)
- perhaps a more traditional <ctrl>-q would be better
- a confirm-if-not-saved pop-up would also be good

## TUI is laggy

- I'm not sure what the app is doing behind the scenes that causes it to lag user input
- add instrumentation to indicate what might be happening
- consider possible reasons why the interface would be blocking basic text input
- consider possible optimisations (better async? need to defer tasks? manual instead of auto task run?)

## Browsing interface

- Opening to the edit screen is the right move -- no mental overhead, just go -- that's great
- I would like the option to switch to a browse screen where I see a list of notes
- perhaps:

+----+-------+-------+
|Date|Version|Summary|
+----+-------+-------+
| .. |  ...  |  ...  |
+----+-------+-------+

- selecting a note opens it; `esc` takes you back to the list
- TBD: expose capability to view previous versions, too (file for later)

