# On-call escalation policy

The primary on-call engineer is paged first and has 15 minutes to acknowledge an
alert. If the page is not acknowledged within that window, it escalates
automatically to the secondary on-call, and after another 15 minutes to the
engineering manager.

Sev-1 incidents, defined as a full customer-facing outage or any data-loss risk,
skip the timed escalation: the incident commander pulls in the secondary and the
manager immediately and opens a dedicated incident channel. The goal for Sev-1
acknowledgement is under five minutes.

Whoever acknowledges the page owns the incident until they explicitly hand it
off. Handoff is never implicit at the end of a shift; the outgoing engineer
states the current status and names the person taking over in the incident
channel.
