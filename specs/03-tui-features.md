# Feedback on current work

This document presents a list of feedback items. 

Create a P0 epic to track the items in this spec.

Evaluate each item and,

- ask to disambiguate any concerns / uncertainties
- file tickets as needed under the new epic

## Expose note id for purge

- to use the `purge` command, the note id must be known
- there is no mechanism to retrieve the note id via the CLI or the TUI
- in the TUI, the `Browse` screen should have the id
- in the CLI, there should be a command that lists notes with their IDs and summary

## Introspect enrichment

- while the embed and enrich jobs seem to run (via `work`), it's not clear what exactly happened
- investigate some way of making the outcomes of the enrichment jobs visible to the end user
- suggest the best alternatives (use examples) and let me choose which to have implemented

## `work` to completion

- add an option to the CLI `work` command such that it will poll (up to some reasonable time) and block until the submitted job(s) are available for processing, and are then processed. In other words, this option sees the jobs all the way through (so that the end user doesn't have to re-run `work`)

## `work` and `ask` do not need to mirror httpx logs to stdout / stderr

- the end user does not need to see the 200's come back
- only show the end user errors and what they need to do instead

## Use shorter, human-readable dates in `Browse`

- right now, it's a full timestamp
- instead, present the user with a human readable (abbreviated) datetime

