# Feedback on current work

This document presents a list of feedback items. 

Create a P0 epic to track the items in this spec.

Evaluate each item and,

- ask to disambiguate any concerns / uncertainties
- file tickets as needed under the new epic

## Ask screen

Now that end-to-end is proven out, it's time to tune the output of the ask
screen. The goal is to make the results easier to read by the user, since at
the moment it's more a less just a dump coming back from the external LLM.

One consideration is whether the prompt should include a requirement for a
structured, annotated JSON output so that visual indicators can be more easily
reasoned about.

Specifically, I want the screen to:
- provide navigation to notes, if they are surfaced; right now, notes have a
  version hash, but *not even an id* so it's impossible to review context
- group by note, where the same note is referenced in the reply multiple times
- provide surrounding context and highlight the relevant section of the note

## Non-AI quick search

The goal of this feature is to provide an offline full-text search of notes for
quick lookups where no summarization or semantic query is needed. In other
words, the traditional kind of full-text search that we see in non-AI forward
solutions. This should be available from the Browse screen only, and narrow the
list of notes shown.

## Read this note and answer my question

While flipping to the Ask screen allows for queries across the entire note
corpus, I think it may be useful to ask something like: "Can you find any JIRA
tickets related to this discussion and append them to this note?" The app
should expose the ability to call JIRA as an available tool that the LLM can
interact with for query purposes.

Another question might be: "Can you look up some suggestions for the missing
capability identified in this note?" In this case, the LLM will need to be able
or allowed to make the web requests.

## Exposing tools to Ask

Given the examples for "Read this note and answer my question," it begs the
question over whether existing integrations (currently JIRA and Confluence) are
available to "Ask". I think that there is significant potential for benefit
where search can be extended beyond the scope of what is captured and drawn
down automatically. This is scoped to exposing read-only query capabilities in
the Ask tooling exclusively.

## Spinner / Visual indicator for ask

As sits at "Thinking", which doesn't tell the user anything. While I have not
had it be _stuck_ on anything, I think that if a spinner and any status info on
the request while it is in-flight could be presented, it would help.

## BUG: Tags have no associated notes

Selecting some tags on the Tag screen filter out **all notes**. Find where a
tag can be created with no note or where the link is not being populated
correctly.

## Word boundaries in faithfulness.py

The word boundary regex is somewhat primitive. If parsing on word boundaries is
genuinely important from a quality perspective, then it's better to tokenize
with either a more sophisticated regex or a library that specializes in this.
Investigate alternatives and whether this is worth pursuing.

## Is backfill needed anymore?

The newer `reembed` and `reenrich` commands seem to cover the backfill
function from a semantic standpoint. Let's remove the backfill command in lieu
of these other commands.

## `cli.py` has too much code

The CLI should focus on dispatch, not logic. There is bare SQL, helper
functions, and sophisticated logic that all should live in their own modules.
Re-factor into src/lode/cli/**.

