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
from dataclasses import dataclass

import anthropic

from lode.answer import Claim
from lode.config import Settings
from lode.egress import WithheldCitation
from lode.gate import apply_gate
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


def gate_cited_answer(result: QaResult, bodies: Mapping[str, str]) -> CitedAnswer:
    """Run the faithfulness gate over a Q&A result, before display.

    Drops every claim whose cited spans are not verbatim-present in ``bodies``
    (:func:`lode.gate.apply_gate`) and carries the survivors -- each with its
    verified citations -- into a :class:`CitedAnswer` alongside the result's
    present-but-withheld citations. ``bodies`` maps each cited ``target_id`` to its
    resolved body text (the caller's job; :func:`ask` resolves it from the store).
    When nothing survives, :attr:`CitedAnswer.abstained` is true.
    """
    gate = apply_gate(result.answer, bodies)
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
    client: anthropic.Anthropic | None = None,
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

    ``client`` defaults to a credential-resolved SDK client inside
    :func:`lode.qa.answer_question`; tests pass a mock so the loop stays offline.
    """
    resolved = [_resolve_target(conn, item) for item in context]
    passages = [
        QaPassage(
            target_id=r.item.target_version,
            text=r.item.parent_block,
            no_egress=r.no_egress,
            is_external=r.is_external,
        )
        for r in resolved
    ]
    result = answer_question(
        conn,
        question,
        passages,
        think_harder=think_harder,
        client=client,
        settings=settings,
    )
    # Verify spans only against bodies the model was eligible to see: drop no_egress
    # (withheld from the send) and unresolved targets, so a claim citing content the
    # model never received fails closed, just like a fabricated quote.
    bodies = {
        r.item.target_version: r.body
        for r in resolved
        if r.body is not None and not r.no_egress
    }
    return gate_cited_answer(result, bodies)


@dataclass(frozen=True)
class _ResolvedTarget:
    """A context item paired with its store-resolved body and no_egress flag."""

    item: ContextItem
    is_external: bool
    body: str | None
    no_egress: bool


def _resolve_target(conn: sqlite3.Connection, item: ContextItem) -> _ResolvedTarget:
    """Resolve one context item's stored body and no_egress flag from the store.

    The polymorphic ``target_version`` is a note ``version_id`` for an owned-note
    tier (body on ``versions``, no_egress on its ``notes`` row) or an external
    ``snapshot_id`` otherwise (body on ``snapshots``, no_egress on its ``externals``
    row); the trust tier discriminates which. A target absent from the store
    resolves to ``None`` body (the gate then fails any claim citing it closed) and a
    safe ``no_egress`` default.
    """
    is_external = item.tier in _EXTERNAL_TIERS
    if is_external:
        row = conn.execute(
            "SELECT s.body, e.no_egress FROM snapshots s "
            "JOIN externals e ON e.external_id = s.external_id "
            "WHERE s.snapshot_id = ?",
            (item.target_version,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT v.body, n.no_egress FROM versions v "
            "JOIN notes n ON n.note_id = v.note_id "
            "WHERE v.version_id = ?",
            (item.target_version,),
        ).fetchone()
    if row is None:
        return _ResolvedTarget(item, is_external, body=None, no_egress=False)
    body, no_egress = row
    return _ResolvedTarget(item, is_external, body=body, no_egress=bool(no_egress))
