# Team decision: feature flags get an expiry date

In the 2026-04 architecture review the team decided that every new feature flag
must carry an expiry date in its definition, defaulting to 90 days out. A weekly
job opens a cleanup ticket for any flag past its expiry so stale flags do not
accumulate forever.

The motivation was a flag that had been fully rolled out for over a year but was
still being evaluated on every request, and a separate near-incident where a
long-dead flag was flipped by mistake during an unrelated change. Both are
symptoms of flags outliving their purpose.

Permanent operational toggles, such as a kill switch for an external dependency,
are exempt but must be tagged "permanent" explicitly. The expiry rule applies
only to rollout flags, which are meant to be temporary by definition.
