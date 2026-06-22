"""no_egress tier -- content withheld from cloud synthesis (lode-fk8.1).

``docs/externals.md`` ("No-egress tier"): a note -- or an external source -- can be
marked **no_egress**. It is still **captured, chunked, embedded, and locally
retrievable** (keyword + vector), but **never sent to Claude** (no enrichment, and
excluded from the cloud Q&A context), and in an answer it is **cited as "present,
withheld from cloud synthesis"** rather than silently dropped -- so the user knows
relevant material exists but was kept local.

This is the **sibling precondition** to redact-before-egress (:mod:`lode.redact`):
both are preconditions for any live Claude call, honored at **both** the
enrichment send (E7, ``lode-npx.1``/``.2``) and the Q&A send (E6, ``lode-az0.4``).
Where redact strips secret *spans* from a payload that is still sent, no_egress
withholds whole *items* from the send entirely. The flag lives on
``notes.no_egress`` / ``externals.no_egress`` (``docs/storage.md`` data shape; new
notes/sources default to ``Settings.no_egress_default``); this module is the
store-agnostic precondition the two send paths consume once they exist -- it never
reaches into the store, retrieval, enrichment, or Q&A, exactly like
:mod:`lode.redact`. Those callers wire it in; it is implemented here once.

**no_egress gates egress only.** Indexing and retrieval never consult it, so a
no_egress note stays keyword- and vector-retrievable locally -- the whole point of
the tier (work secrets stay *in* the KB, they just never reach the cloud). A
withheld item is **routed to** :attr:`EgressDecision.withheld`, never dropped, so
the Q&A layer can cite it as present-but-withheld.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

WITHHELD_CITATION = "present, withheld from cloud synthesis"
"""How a no_egress item is cited in an answer (``docs/externals.md``)."""


class Withholdable(Protocol):
    """A content item the send paths consider for cloud egress.

    Any note version / external snapshot / retrieved passage qualifies: it exposes
    a stable ``target_id`` (the cited ``version_id`` or ``snapshot_id``) and the
    ``no_egress`` flag read from its ``notes`` / ``externals`` row.
    """

    target_id: str
    no_egress: bool


@dataclass(frozen=True)
class EgressItem:
    """Minimal concrete :class:`Withholdable` -- a target id plus its no_egress flag.

    Carries the no_egress flag (``notes.no_egress`` / ``externals.no_egress`` in
    the ``docs/storage.md`` data shape) store-agnostically, so a caller without the
    storage core -- and the tests -- can still exercise the precondition.
    """

    target_id: str
    no_egress: bool = False


@dataclass(frozen=True)
class WithheldCitation:
    """How a withheld item surfaces in an answer: present locally, kept off-cloud.

    This is the citation *content* -- the target that exists locally plus the fixed
    "present, withheld from cloud synthesis" marker. Laying it out for the terminal
    belongs to the CLI/Q&A layer (``lode-y42.2``), as with the faithfulness gate.
    """

    target_id: str
    note: str = WITHHELD_CITATION


T = TypeVar("T", bound=Withholdable)


@dataclass(frozen=True)
class EgressDecision(Generic[T]):
    """Partition of egress candidates into what may be sent vs what is withheld.

    ``sendable`` cleared the no_egress precondition and may go to Claude (still
    subject to redact-before-egress); ``withheld`` are no_egress and never leave the
    box. :attr:`withheld_citations` turns the withheld set into the "present,
    withheld from cloud synthesis" citations an answer surfaces instead of dropping
    them. Caller object identity and type are preserved on both sides.
    """

    sendable: tuple[T, ...]
    withheld: tuple[T, ...]

    @property
    def withheld_citations(self) -> tuple[WithheldCitation, ...]:
        """Withheld items as present-but-withheld citations, in order."""
        return tuple(WithheldCitation(item.target_id) for item in self.withheld)


def partition_egress(items: Iterable[T]) -> EgressDecision[T]:
    """Split egress candidates by ``no_egress``: withhold the never-sent ones.

    The single precondition both send paths consume -- enrichment (E7) and Q&A (E6)
    call this identically, so a no_egress item is withheld at **both** sends, not
    just one. Order is preserved within each side. This gates egress only: callers
    keep indexing and retrieving every item regardless of the verdict, so no_egress
    content stays locally retrievable, and withheld items are routed aside (citable)
    rather than discarded.
    """
    sendable: list[T] = []
    withheld: list[T] = []
    for item in items:
        (withheld if item.no_egress else sendable).append(item)
    return EgressDecision(sendable=tuple(sendable), withheld=tuple(withheld))
