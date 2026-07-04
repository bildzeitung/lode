# Feedback on current work (trunk at: 3c2f365dd3e16b79d17386b9e4a1a4001fa25fbe)

This document presents a list of feedback items. 

Create a P0 epic to track the items in this spec.

Evaluate each item and,

- ask to disambiguate any concerns / uncertainties
- file tickets as needed under the new epic

## Config-affecting items not exposed to docs or CLI

- `LODE_LOG_LEVEL` is not present in `./docs/*`, but should be

## Debug feature switches

- There should be a `--debug` flag for the CLI that enables debugging features (like this one)

## Debug logging writes to console

- while the debug logging *does* write to the proper place, it also dumps to the console,
  which should either be *explicitly enabled* (as it confuses the TUI) or simply *not do this*
  as logging to the final is sufficient -- especially for the current exercise, which is to
  collect latency telemetry data in the TUI.

## Save on exit guard

- when the user presses `esc` to exit, the save prompt is on a blank screen
- instead, the prompt should be a pop-up window overtop of the editor and disappear if the user presses 'cancel'
- this is less jarring for the user and is more like how UIs normally work

