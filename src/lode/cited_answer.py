"""Gate the Q&A answer before display, then render its cited claims (lode-az0.3).

This is the **integration seam** between the landed pieces of the E6 Q&A loop and
the terminal (``docs/retrieval.md`` "The faithfulness gate ... runs app-side,
after the Q&A LLM returns and **before display**"; ``docs/design.md`` the cited-Q&A
bet -- *abstain rather than emit an unsupported claim*). It owns the **before
display** step, not the rendering primitives it composes:

- the structured-claims call -- :func:`lode.qa.answer_question` (lode-az0.2), which
  returns the verifiable :class:`lode.answer.Answer` plus the present-but-withheld
  citations;
- the faithfulness gate -- :func:`lode.gate.apply_gate` (lode-1k3.5 over
  lode-1k3.2), which drops every claim whose cited spans are not verbatim-present
  and **abstains** when none survive.

It reuses both verbatim -- it never re-implements the NLI/entailment scoring, the
drop/abstain policy, or the egress precondition. What it adds is the wiring those
two ends could not assume:

**The ``ContextItem`` -> ``QaPassage`` adaptation that lode-az0.2 deferred.** The
read side hands back a trust-ranked :class:`lode.retrieval.TrustRankedContext` of
:class:`lode.retrieval.ContextItem`; the Q&A send consumes
:class:`lode.qa.QaPassage`. :func:`ask` bridges them, and -- because no_egress
lives on the ``notes`` / ``externals`` row, not on the retrieved passage -- it
**resolves no_egress from the store** per cited target so a withheld note is
actually kept off-cloud by the egress gate (``docs/externals.md`` "No-egress
tier"). Small-to-big (``docs/retrieval.md``): the model is given each hit's
``parent_block`` for synthesis while the citation stays pinned to the precise
``target_version`` + span.

**The gate's ``bodies`` map, store-resolved.** The verbatim-span check verifies
each ``quoted_span`` against the **stored bytes** of its cited version/snapshot
(``versions.body`` / ``snapshots.body``), which ``docs/storage.md`` /
``faithfulness.py`` name as the caller's job. Bodies are resolved **only for the
egress-cleared targets** -- a no_egress target's body is withheld from the map, so
a claim that cites content the model was never shown fails closed and is dropped,
the same fail-closed posture as a fabricated quote.

The result is a :class:`CitedAnswer`: the surviving claims (each already carrying
its verbatim-verified citations) plus the present-but-withheld citations, and the
**abstention** verdict when nothing survives. Laying that out for the terminal --
printing the abstention line or the cited claims -- belongs to the ``lode ask`` CLI
(lode-y42.2); this module returns *what that layer renders*, not the rendering.
"""

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from lode.answer import Claim, Support
from lode.chunking import parse_char_range
from lode.config import Settings
from lode.egress import WithheldCitation
from lode.faithfulness import EntailmentScorer, locate_span
from lode.gate import apply_gate
from lode.llm_provider import LLMProvider
from lode.no_egress_scope import NoEgressScopeRule, is_no_egress_scoped
from lode.qa import QaPassage, QaResult, answer_question
from lode.retrieval import ContextItem, TrustTier

_EXTERNAL_TIERS = (TrustTier.CURRENT_EXTERNAL, TrustTier.STALE_EXTERNAL)
"""Trust tiers whose ``target_version`` is an external ``snapshot_id`` (cite via
``snapshot_id``); every other reachable tier is an owned note (cite via
``version_id``). See :class:`lode.retrieval.TrustTier`."""


@dataclass(frozen=True)
class CitedAnswer:
    """The gate-applied answer ready for display: cited survivors, or abstention.

    ``claims`` are the claims that passed the faithfulness gate, in order, each
    still carrying its ``support`` -- and because the gate verified every support's
    span verbatim, that ``support`` *is* the claim's citations (the
    version_id/snapshot_id + span the CLI renders). ``withheld_citations`` are the
    no_egress items surfaced as "present, withheld from cloud synthesis" rather than
    dropped, so the user knows relevant local material exists. The answer
    **abstains** exactly when no claim survived (:attr:`abstained`) -- the honest
    failure mode; the withheld citations still surface in that case.
    """

    claims: tuple[Claim, ...]
    withheld_citations: tuple[WithheldCitation, ...]

    @property
    def abstained(self) -> bool:
        """Whether the gate abstained -- true iff no claim survived."""
        return not self.claims


def gate_cited_answer(
    result: QaResult,
    bodies: Mapping[str, str],
    *,
    scorer: EntailmentScorer | None = None,
    settings: Settings | None = None,
) -> CitedAnswer:
    """Run the faithfulness gate over a Q&A result, before display.

    Drops every claim whose cited spans are not verbatim-present in ``bodies``
    (:func:`lode.gate.apply_gate`) and carries the survivors -- each with its
    verified citations -- into a :class:`CitedAnswer` alongside the result's
    present-but-withheld citations. ``bodies`` maps each cited ``target_id`` to its
    resolved body text (the caller's job; :func:`ask` resolves it from the store).
    When nothing survives, :attr:`CitedAnswer.abstained` is true.

    ``settings`` and the optional ``scorer`` are the gate-tuning seam threaded
    through to :func:`lode.gate.apply_gate`, so the configured
    ``entailment_threshold`` (step 3) is honored on the ask path rather than
    silently falling back to the :class:`Settings` default; both default to
    :func:`apply_gate`'s own defaults when not passed.
    """
    gate = apply_gate(result.answer, bodies, scorer=scorer, settings=settings)
    return CitedAnswer(
        claims=gate.surviving_claims,
        withheld_citations=result.withheld_citations,
    )


def ask(
    conn: sqlite3.Connection,
    question: str,
    context: Sequence[ContextItem],
    *,
    think_harder: bool = False,
    provider: LLMProvider | None = None,
    scorer: EntailmentScorer | None = None,
    settings: Settings | None = None,
) -> CitedAnswer:
    """Answer ``question`` over the trust-ranked ``context``, gated before display.

    The full E6 loop the read and synthesis ends could not assume on their own:

    1. **Adapt** each trust-ranked :class:`lode.retrieval.ContextItem` into a
       :class:`lode.qa.QaPassage` -- carrying the ``parent_block`` for synthesis
       (small-to-big), the ``target_version`` as the citation target, the note-vs-
       external discriminator from the trust tier, and the **store-resolved
       no_egress** flag so a withheld note is kept off-cloud by the egress gate.
    2. **Synthesize** structured, cited claims (:func:`lode.qa.answer_question`),
       which runs the cloud-egress precondition before any byte reaches Claude.
    3. **Gate** the claims before display (:func:`gate_cited_answer`) against the
       stored bodies of the **egress-cleared** targets, abstaining if none survive.

    ``provider`` defaults to a credential-resolved
    :class:`~lode.llm_provider.LLMProvider` inside
    :func:`lode.qa.answer_question`; tests pass a mock so the loop stays offline.
    ``settings`` is threaded into both the synthesis send and the gate, so the
    configured ``entailment_threshold`` is honored at step 3; ``scorer`` is the
    same step-3 entailment seam :func:`lode.gate.apply_gate` exposes, which tests
    inject to keep the gate offline.
    """
    settings = settings or Settings()
    passages: list[QaPassage] = []
    # Verify spans only against bodies the model was eligible to see: a no_egress
    # body (withheld from the send) and an unresolved target are kept out of the
    # map, so a claim citing content the model never received fails closed, just
    # like a fabricated quote.
    bodies: dict[str, str] = {}
    resolved = _resolve_targets(conn, context, settings.no_egress_scopes)
    for item in context:
        is_external = item.tier in _EXTERNAL_TIERS
        body, no_egress = resolved.get(item.target_version, (None, False))
        passages.append(
            QaPassage(
                target_id=item.target_version,
                text=item.parent_block,
                no_egress=no_egress,
                is_external=is_external,
            )
        )
        if body is not None and not no_egress:
            bodies[item.target_version] = body

    result = answer_question(
        conn,
        question,
        passages,
        think_harder=think_harder,
        provider=provider,
        settings=settings,
    )
    cited = gate_cited_answer(result, bodies, scorer=scorer, settings=settings)
    return replace(cited, claims=_stamp_body_offsets(cited.claims, context, bodies))


def _stamp_body_offsets(
    claims: tuple[Claim, ...],
    context: Sequence[ContextItem],
    bodies: Mapping[str, str],
) -> tuple[Claim, ...]:
    """Disambiguate a repeated ``quoted_span`` by locating it in its own retrieved passage (lode-hruz).

    A claim's ``Support`` carries no offset from the LLM -- it only echoes back
    ``version_id``/``snapshot_id`` + verbatim text -- so when the same span text
    occurs more than once in the cited body, which occurrence the model actually
    saw is otherwise lost. Each retrieved :class:`~lode.retrieval.ContextItem`
    DOES know its own precise char offset (``body[start:end] == item.passage_text``,
    ``chunking.py``), so this searches each retrieved passage's own slice of the
    body and stamps ``Support.body_offset`` with the first hit -- via
    :func:`~lode.faithfulness.locate_span`, so a whitespace-reflowed quote (which
    the gate accepts, and which is the common case off a multi-line body) is
    disambiguated too, not just an exact one.

    Every surviving support is rewritten, so a ``body_offset`` the model invented
    (``Support`` is also the response schema) can never survive to a renderer.
    ``body_offset`` is left ``None`` -- renderer falls back to the first
    occurrence, as before lode-hruz -- when no retrieved passage for the target
    contains the span, e.g. it only appears in the larger ``parent_block``.
    """
    if not claims:
        return claims

    passage_ranges: dict[str, list[tuple[int, int]]] = {}
    for item in context:
        bounds = parse_char_range(item.char_range)
        if bounds is not None:
            passage_ranges.setdefault(item.target_version, []).append(bounds)

    def offset_for(support: Support) -> int | None:
        body = bodies.get(support.target_id)
        if body is None:
            return None
        for start, end in passage_ranges.get(support.target_id, ()):
            located = locate_span(support.quoted_span, body[start:end])
            if located is not None:
                return start + located[0]
        return None

    return tuple(
        claim.model_copy(
            update={
                "support": [
                    support.model_copy(update={"body_offset": offset_for(support)})
                    for support in claim.support
                ]
            }
        )
        for claim in claims
    )


def _resolve_targets(
    conn: sqlite3.Connection,
    context: Sequence[ContextItem],
    no_egress_scopes: Sequence[NoEgressScopeRule] = (),
) -> dict[str, tuple[str | None, bool]]:
    """Resolve every cited target's stored body and no_egress flag, batched.

    ``context`` may cite the same target more than once (repeated passages, or a
    top-k spanning several notes/snapshots); this resolves every **distinct**
    target in at most two round trips regardless of context size, splitting on
    the trust tier (:data:`_EXTERNAL_TIERS`) -- the same batched-``IN(...)``
    split :func:`lode.retrieval.trust_rank` already makes over the same
    polymorphic ``target_version`` shape.

    Returns a ``{target_version: (body, no_egress)}`` map. A target absent from
    the store is simply absent from the map -- the caller (:func:`ask`) treats a
    missing key as ``(None, False)``, the same safe default a single-row lookup
    would have returned: a ``None`` body (the gate then fails any claim citing it
    closed) and a safe ``no_egress`` default.

    For an external target, the returned ``no_egress`` composes the per-row
    ``externals.no_egress`` flag with ``no_egress_scopes`` (lode-35nu.11.8) --
    either denying is a denial. Note-side targets have no ``external_id`` and no
    scope concept, so they are unaffected.
    """
    note_ids = sorted(
        {item.target_version for item in context if item.tier not in _EXTERNAL_TIERS}
    )
    external_ids = sorted(
        {item.target_version for item in context if item.tier in _EXTERNAL_TIERS}
    )

    resolved: dict[str, tuple[str | None, bool]] = {}
    if note_ids:
        placeholders = ", ".join("?" for _ in note_ids)
        for version_id, body, no_egress in conn.execute(
            "SELECT v.version_id, v.body, n.no_egress FROM versions v "
            "JOIN notes n ON n.note_id = v.note_id "
            f"WHERE v.version_id IN ({placeholders})",
            note_ids,
        ):
            resolved[version_id] = (body, bool(no_egress))
    if external_ids:
        placeholders = ", ".join("?" for _ in external_ids)
        for snapshot_id, body, no_egress, external_id, source_type in conn.execute(
            "SELECT s.snapshot_id, s.body, e.no_egress, e.external_id, e.source_type "
            "FROM snapshots s JOIN externals e ON e.external_id = s.external_id "
            f"WHERE s.snapshot_id IN ({placeholders})",
            external_ids,
        ):
            scoped = is_no_egress_scoped(external_id, source_type, no_egress_scopes)
            resolved[snapshot_id] = (body, bool(no_egress) or scoped)
    return resolved
