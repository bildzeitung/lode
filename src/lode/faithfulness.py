"""The faithfulness gate, steps 1-2: verbatim-span check + extractive coupling.

``docs/retrieval.md`` ("The faithfulness gate"), steps 1-2:

    1. Verbatim-span check (deterministic, v1). Every ``quoted_span`` must occur
       (exact, or normalized-whitespace) in the body of its cited
       ``version_id``/``snapshot_id``. No model, no latency.
    2. Extractive coupling (deterministic, v1). A fast path: if the claim's
       load-bearing payload lies inside the quoted span, the claim is verified
       outright -- stopping an inverted quote paired with a contradicting claim,
       or a drifted number that is both quoted-verbatim and wrong.

Both stages are **deterministic** -- no model is ever invoked. Step 1
(lode-1k3.2) operates over a ``Support``/``Claim`` plus the already-resolved
**body text** of the cited target; resolving a ``version_id``/``snapshot_id`` to
its bytes is the caller's job (the storage core, ``docs/storage.md``), so this
module deliberately never touches a store. Step 2 (lode-1k3.3) is purely
``claim.text`` against its own ``quoted_span`` -- step 1 already proved the span
is in the body, so coupling needs no body and resolves nothing.

NLI entailment (step 3, ``lode-1k3.4``) is a later ticket; the actual drop / flag
/ abstain orchestration (step 4-5) is ``lode-1k3.5``. This module supplies the
*verdicts* those stages consume -- it reports whether a span is verbatim-present
and whether a claim is extractively coupled; it does not itself drop claims,
sequence the stages, or decide abstention (that staging lives in ``gate.py``).
"""

import re
from collections.abc import Mapping

from lode.answer import Claim, Support

_WHITESPACE = re.compile(r"\s+")

#: Word tokens for extractive coupling: maximal runs of letters/digits, excluding
#: underscore and punctuation, over case-folded text. Unicode letters are kept so
#: non-ASCII entities tokenize, not just ASCII words.
_WORD = re.compile(r"[^\W_]+")

#: Grammatical glue stripped from a claim before coupling: articles and the
#: copula/auxiliary "to be". These carry neither *quantity* nor *polarity*, so
#: dropping them lets a faithful extract ("rerank is off" vs the span "rerank
#: OFF") still couple. Numbers, entities, and polarity/quantity words ("off",
#: "no", "not", "all") are deliberately **never** stopwords -- they are exactly
#: what a drifted-number or inverted-quote claim turns on, so they must remain
#: load-bearing for coupling to catch the drift. The set is kept minimal on
#: purpose: missing words only make coupling *stricter* (a faithful claim falls
#: through to NLI instead of fast-pathing), which is the fail-closed direction.
_STOPWORDS = frozenset(
    {"a", "an", "the", "is", "are", "was", "were", "be", "been", "being", "am"}
)


def normalize_whitespace(text: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends.

    This is the normalization behind "differing only by whitespace is accepted":
    newlines, tabs, and runs of spaces in either the span or the body are flattened
    so that a quote that matches except for reflowed whitespace still verifies.
    """
    return _WHITESPACE.sub(" ", text).strip()


def span_occurs(span: str, body: str) -> bool:
    """Whether ``span`` occurs verbatim in ``body`` -- exact, or normalized-whitespace.

    Exact substring is tried first (the common case and a strict subset of the
    normalized match); failing that, both sides are whitespace-normalized so a span
    that differs from the body only by reflowed whitespace is still accepted. No
    model is involved -- this is a pure string check.
    """
    if span in body:
        return True
    return normalize_whitespace(span) in normalize_whitespace(body)


def support_verified(support: Support, body: str) -> bool:
    """Whether one support's ``quoted_span`` is verbatim-present in its cited ``body``.

    ``body`` is the resolved text of ``support.target_id`` (caller-resolved; see the
    module docstring). A support whose span does not occur is a fabricated quote.
    """
    return span_occurs(support.quoted_span, body)


def claim_spans_verified(claim: Claim, bodies: Mapping[str, str]) -> bool:
    """Whether **every** support of ``claim`` is verbatim-present in its cited body.

    ``bodies`` maps each cited ``target_id`` (a ``version_id`` or ``snapshot_id``) to
    its resolved body text; the caller resolves the bytes. Per ``docs/retrieval.md``
    step 1, *every* ``quoted_span`` must occur, so a single fabricated span makes the
    claim fail the check -- which is what causes it to be dropped downstream
    (``lode-1k3.5``). A target absent from ``bodies`` is treated as an unverifiable
    (hence failing) citation rather than an error, so a missing body never crashes
    the gate.
    """
    return all(
        support_verified(support, bodies.get(support.target_id, ""))
        for support in claim.support
    )


def _word_tokens(text: str) -> frozenset[str]:
    """The case-folded word-token set of ``text`` (letters/digits, no punctuation).

    Set, not sequence: coupling asks "are these tokens present", and word-level
    matching (vs raw substring) is what stops a drifted ``5000`` from matching
    inside an unrelated ``150000`` -- ``5000`` is its own token only when it
    stands alone.
    """
    return frozenset(_WORD.findall(text.casefold()))


def claim_extractively_coupled(claim: Claim) -> bool:
    """Whether ``claim``'s load-bearing payload lies inside one of its cited spans.

    The **extractive-coupling fast path** (``docs/retrieval.md`` step 2): the
    claim's load-bearing payload is its word tokens minus grammatical glue
    (:data:`_STOPWORDS`); the claim is coupled iff **some single** ``quoted_span``
    contains every one of those tokens. A claim whose payload is split *across*
    spans is genuine **synthesis**, not extractive -- it is deliberately not
    coupled here and falls through to the NLI stage (lode-1k3.4).

    This is a pure ``claim.text``-vs-``quoted_span`` check; it takes no bodies,
    because step 1 (:func:`claim_spans_verified`) already proved each span is
    present in its cited target. A claim with no load-bearing token (all glue)
    couples with nothing -- there is no payload to find inside a span -- so it
    fails closed and is left for a later stage rather than vacuously verified.
    """
    payload = _word_tokens(claim.text) - _STOPWORDS
    if not payload:
        return False
    return any(
        payload <= _word_tokens(support.quoted_span) for support in claim.support
    )
