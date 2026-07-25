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

This module also owns the **Q&A egress gate** (``lode-az0.4``). :func:`gate_qa_egress`
runs the full cloud-egress precondition for a Q&A send -- exclude no_egress
passages, redact secret spans from what remains (:mod:`lode.redact`), and write one
``egress_log`` audit row (``docs/storage.md`` §8, ``docs/externals.md`` "Egress
log") recording purpose, model, the target ids sent, and which redactions were
applied. It returns the redacted payloads to send plus the present-but-withheld
citations, leaving the live Claude call and terminal rendering to the Q&A/CLI
layer. :func:`log_egress` is the lower-level audit write the enrichment send (E7)
will reuse with ``purpose='enrich'``.
"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from lode.config import Settings
from lode.redact import redact_before_egress_counting

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
class EgressDecision[T]:
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


def partition_egress[T](items: Iterable[T]) -> EgressDecision[T]:
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


QA_PURPOSE = "qa"
"""``egress_log.purpose`` for a Q&A send (the schema CHECK allows ``enrich``/``qa``)."""


class EgressPassage(Withholdable, Protocol):
    """A retrieved passage the Q&A send considers: a :class:`Withholdable` with text.

    The Q&A gate needs the actual ``text`` to redact before egress, on top of the
    ``target_id``/``no_egress`` every :class:`Withholdable` carries. Any retrieval
    passage qualifies structurally; :class:`PassageItem` is the minimal concrete
    stand-in for callers/tests without the (not-yet-built) retrieval passage type.
    """

    text: str


@dataclass(frozen=True)
class PassageItem:
    """Minimal concrete :class:`EgressPassage` -- a target id, its text, no_egress."""

    target_id: str
    text: str
    no_egress: bool = False


@dataclass(frozen=True)
class RedactedSend:
    """One passage cleared for egress, with secret spans already stripped.

    ``text`` is the redact-before-egress output (:mod:`lode.redact`) -- exactly the
    bytes the live Q&A call sends for ``target_id``. ``redactions`` is how many
    secret spans were stripped from it, recorded in the ``egress_log`` audit row.
    """

    target_id: str
    text: str
    redactions: int


@dataclass(frozen=True)
class QaEgress:
    """Outcome of the Q&A egress gate: what to send, what was withheld, audit id.

    ``sent`` are the redacted passages cleared for the live Claude call (in order);
    ``withheld_citations`` are the no_egress items to surface as "present, withheld
    from cloud synthesis" instead; ``egress_log_id`` is the audit row just written.
    """

    sent: tuple[RedactedSend, ...]
    withheld_citations: tuple[WithheldCitation, ...]
    egress_log_id: int


def log_egress(
    conn: sqlite3.Connection,
    purpose: str,
    model: str,
    sent_targets: Iterable[str],
    redactions: object | None = None,
    *,
    provider: str | None = None,
) -> int:
    """Write one ``egress_log`` row and return its id (``docs/storage.md`` §8).

    One row per time content leaves the box, so cloud exposure is auditable.
    ``sent_targets`` (the version/snapshot/passage ids sent) and ``redactions``
    (which redactions were applied) are stored as JSON summaries; ``redactions``
    may be ``None`` when nothing was stripped. ``purpose`` is ``qa`` here; the
    enrichment send (E7) reuses this with ``enrich``. ``provider`` is the LLM
    vendor identity (lode-568v.4) -- ``None`` means "anthropic" by convention
    (:func:`lode.llm_provider.provider_identity`); the Q&A send does not thread
    it through yet (out of this ticket's scope, ``docs/decisions.md``
    lode-568v.1), so it defaults to ``None`` here, which is also the correct
    value today since Q&A is Anthropic-only regardless. Commits before
    returning.
    """
    cur = conn.execute(
        "INSERT INTO egress_log (purpose, model, provider, sent_targets, redactions) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            purpose,
            model,
            provider,
            json.dumps(list(sent_targets)),
            None if redactions is None else json.dumps(redactions),
        ),
    )
    conn.commit()
    return cur.lastrowid


def gate_qa_egress(
    conn: sqlite3.Connection,
    model: str,
    passages: Iterable[EgressPassage],
    settings: Settings | None = None,
) -> QaEgress:
    """Run the full cloud-egress gate for a Q&A send and audit it.

    The single entry point the Q&A send calls before reaching Claude:

    1. **Exclude no_egress** passages (:func:`partition_egress`) -- they are never
       sent and come back as :attr:`QaEgress.withheld_citations`.
    2. **Redact-before-egress** the surviving passages (:mod:`lode.redact`),
       counting the secret spans stripped from each.
    3. **Write one** ``egress_log`` row (purpose ``qa``, the model, the sent target
       ids, and a per-target redaction summary) so the send is auditable.

    Returns the redacted payloads to send plus the withheld citations; it does not
    make the live Claude call or render anything -- that is the Q&A/CLI layer's job.
    """
    decision = partition_egress(passages)
    sent: list[RedactedSend] = []
    for passage in decision.sendable:
        redacted, count = redact_before_egress_counting(passage.text, settings)
        sent.append(RedactedSend(passage.target_id, redacted, count))
    redactions = {s.target_id: s.redactions for s in sent if s.redactions}
    log_id = log_egress(
        conn,
        QA_PURPOSE,
        model,
        [s.target_id for s in sent],
        redactions or None,
    )
    return QaEgress(
        sent=tuple(sent),
        withheld_citations=decision.withheld_citations,
        egress_log_id=log_id,
    )
