"""The faithfulness gate, steps 4-5: drop failures + abstain (lode-1k3.5).

``docs/retrieval.md`` ("The faithfulness gate"), steps 4-5:

    4. Drop or flag claims that fail; never silently display them.
    5. Abstain. If nothing survives the gate, the system says "your notes don't
       answer this" -- the honest failure mode. Fidelity over fluency means a
       willingness to return nothing rather than a confident hallucination.

This module is the **orchestration** the per-stage verdicts feed into: it runs
each claim through the gate's checks, **drops** the ones that fail, and decides
**abstention** when nothing survives. ``faithfulness.py`` supplies the verdict
(it reports whether a span is verbatim-present); this module turns those verdicts
into a survivor set or an abstention.

Scope is the walking skeleton: the only gate stage that exists today is the
deterministic verbatim-span check (step 1, ``faithfulness.claim_spans_verified``,
lode-1k3.2). Extractive coupling (step 2) and NLI entailment (step 3) are later
``lode-1k3`` tickets; when they land they compose into the same survivor test
here without changing this module's contract.

The decision is a **pure function** over an ``Answer`` and the caller-resolved
``bodies`` map (a ``target_id`` -> body-text mapping; resolving a
``version_id``/``snapshot_id`` to bytes is the storage core's job, so this module
never touches a store). Terminal rendering -- printing the abstention message or
laying out surviving claims with their citations -- belongs to the CLI/Q&A layer
(lode-y42.2); this module returns *what that layer renders*, not the rendering.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from lode.answer import Answer, Claim
from lode.faithfulness import claim_spans_verified

ABSTENTION_MESSAGE = "your notes don't answer this"
"""The honest failure mode shown when no claim survives the gate (step 5)."""


@dataclass(frozen=True)
class GateResult:
    """Outcome of the gate's drop/abstain orchestration over one answer.

    ``surviving_claims`` are the claims that passed every gate check, in their
    original order, each still carrying its ``support`` (the citation data the
    CLI/Q&A layer renders). The gate **abstains** exactly when this is empty:
    that is the "your notes don't answer this" path, and the policy "nothing
    survived means abstain" lives here, not in the caller.
    """

    surviving_claims: tuple[Claim, ...]

    @property
    def abstained(self) -> bool:
        """Whether the gate abstained -- true iff no claim survived."""
        return not self.surviving_claims


def apply_gate(answer: Answer, bodies: Mapping[str, str]) -> GateResult:
    """Run ``answer`` through the gate: drop failing claims, decide abstention.

    Every claim is checked against the verbatim-span verdict
    (``claim_spans_verified``) over the caller-resolved ``bodies``; claims that
    fail are dropped (step 4 -- never silently displayed). The survivors are
    returned in order. When none survive, ``GateResult.abstained`` is true and
    the caller shows ``ABSTENTION_MESSAGE`` (step 5). An empty ``answer`` (the
    model asserted nothing) abstains the same way -- there is nothing to verify.
    """
    survivors = tuple(
        claim for claim in answer.claims if claim_spans_verified(claim, bodies)
    )
    return GateResult(surviving_claims=survivors)
