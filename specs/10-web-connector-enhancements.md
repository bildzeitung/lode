# Feedback on current work

This document presents a list of feedback items. 

Create a P0 epic to track the items in this spec.

Evaluate each item and,

- ask to disambiguate any concerns / uncertainties
- file tickets as needed under the new epic

## Goal

Extend the web connector to disambiguate between different domains with
the ability to filter for web application ingestion logic. Specically, this
spec targets JIRA and Confluence integration.

## Discussion

Fetching public web documents is well-supported by the current platform. For
this product to be useful in a business context, however, there are instances
where the content is in a web application fronted by authentication, where
those fetch operations return a login page or some other useless noise.

Additionally, some of these web links are human-facing (i.e. a JIRA link takes
me to the company instance of Atlassian's product and shows me a ticket), but
given an API key, a structured response far more friendly for AI use could
be retrieved as a substitute for the original URL.

With respect to other connectors, the differentiator is mechanism. For example,
email would not be web-based, but rather via an IMAP library. Repository lookups
are likely a function of pulling files via git tooling.

## Systems to target

Both Atlassian products:

- JIRA
- Confluence

## Additional configuration

lode will need at a minimum, a way to fetch the API key. Storage can be in lode's `config.yaml` (define a key), with a fallback to a documented env var.

Since not everyone has these integrations, they should be feature flagged in `config.yaml`, defaulting to `off`.

The API endpoints should be inferrable from the links in the notes, but sometimes they are not. There should be an override configurable in `config.yaml` for each individually. If there is an error reaching the API endpoint, it should be logged and be visible to the user running a work pass.

## Live Testing

There is no live testing that is really going to be possible. Therefore, it's up to the developer to validate that this functionality works. Define a process for performing a test and the outputs desired so that the developer can present work to an agent to validate dev work.

