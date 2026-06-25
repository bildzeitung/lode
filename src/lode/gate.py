"""The faithfulness gate, steps 4-5: drop failures + abstain (lode-1k3.5).

``docs/retrieval.md`` ("The faithfulness gate"), steps 4-5:

    4. Drop or flag claims that fail; never silently display them.
    5. Abstain. If nothing survives the gate, the system says "your notes don't
       answer this" -- the honest failure mode. Fidelity over fluency means a
       willingness to return nothing rather than a confident hallucination.

This module is the **orchestration** the per-stage verdicts feed into: it runs
each claim through the gate's staged checks, **drops** the ones that fail, and
decides **abstention** when nothing survives. ``faithfulness.py`` supplies the
verdicts (whether a span is verbatim-present, whether a claim is extractively
coupled); this module sequences them and turns the result into a survivor set or
an abstention.

The per-claim decision is **staged** (``docs/retrieval.md`` faithfulness gate):
the deterministic verbatim-span check (step 1,
``faithfulness.claim_spans_verified``, lode-1k3.2) runs first, then the
deterministic extractive-coupling fast path (step 2,
``faithfulness.claim_extractively_coupled``, lode-1k3.3), then **NLI entailment**
(step 3, ``faithfulness.claim_entailed``, lode-1k3.4): a claim that passes the
span check but is *not* extractively coupled (genuine synthesis / out-of-span
paraphrase) is scored by a local NLI / cross-encoder for whether its cited spans
**jointly entail** it, and survives only above the conservative, untuned
``Settings.entailment_threshold`` -- else dropped (**fail closed**, the same
posture as a fabricated quote). The scorer is an injectable seam
(``faithfulness.EntailmentScorer``), so a gate run never downloads a model unless
a claim actually reaches step 3, and tests pass a stub to stay offline.

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
from lode.config import Settings
from lode.faithfulness import (
    EntailmentScorer,
    FastEmbedEntailmentScorer,
    claim_entailed,
    claim_extractively_coupled,
    claim_spans_verified,
)

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


def _claim_survives(
    claim: Claim,
    bodies: Mapping[str, str],
    scorer: EntailmentScorer,
    threshold: float,
) -> bool:
    """Whether ``claim`` passes the gate's staged per-claim decision.

    The stages run cheapest-first and short-circuit:

    1. **Verbatim-span check** (``claim_spans_verified``). A fabricated quote --
       a span not present in its cited body -- drops the claim outright.
    2. **Extractive-coupling fast path** (``claim_extractively_coupled``). With
       the span proven present, a claim whose load-bearing payload lies inside a
       cited span is verified outright, with no model invoked.
    3. **NLI entailment** (``claim_entailed``, step 3, lode-1k3.4). A claim that
       passes step 1 but not step 2 is genuine synthesis / out-of-span
       paraphrase; ``scorer`` judges whether its cited spans jointly entail it,
       and it survives only at or above ``threshold``. Below threshold (or a
       claim nothing supports) the gate **fails closed** and drops it.
    """
    if not claim_spans_verified(claim, bodies):
        return False
    if claim_extractively_coupled(claim):
        return True
    return claim_entailed(claim, scorer, threshold=threshold)


def apply_gate(
    answer: Answer,
    bodies: Mapping[str, str],
    *,
    scorer: EntailmentScorer | None = None,
    settings: Settings | None = None,
) -> GateResult:
    """Run ``answer`` through the gate: drop failing claims, decide abstention.

    Every claim is run through the staged per-claim decision
    (:func:`_claim_survives`: verbatim-span check, extractive-coupling fast path,
    then NLI entailment) over the caller-resolved ``bodies``; claims that fail are
    dropped (step 4 -- never silently displayed). The survivors are returned in
    order. When none survive, ``GateResult.abstained`` is true and the caller
    shows ``ABSTENTION_MESSAGE`` (step 5). An empty ``answer`` (the model asserted
    nothing) abstains the same way -- there is nothing to verify.

    ``scorer`` is the step-3 entailment seam; it defaults to
    :class:`~lode.faithfulness.FastEmbedEntailmentScorer` (the pinned local model
    on the shared ONNX runtime), which loads lazily, so the default never
    downloads a model unless a claim actually reaches step 3. Tests inject a stub
    to stay offline. The acceptance threshold is ``settings.entailment_threshold``
    (conservative + untuned); ``settings`` defaults to :class:`Settings`.
    """
    settings = settings or Settings()
    scorer = scorer or FastEmbedEntailmentScorer(settings)
    threshold = settings.entailment_threshold
    survivors = tuple(
        claim
        for claim in answer.claims
        if _claim_survives(claim, bodies, scorer, threshold)
    )
    return GateResult(surviving_claims=survivors)
