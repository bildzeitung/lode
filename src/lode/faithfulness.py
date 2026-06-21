"""The faithfulness gate, step 1: verbatim-span check (lode-1k3.2).

``docs/retrieval.md`` ("The faithfulness gate"), step 1:

    Verbatim-span check (deterministic, v1). Every ``quoted_span`` must occur
    (exact, or normalized-whitespace) in the body of its cited
    ``version_id``/``snapshot_id``. No model, no latency.

This is the cheapest gate stage and catches quote *fabrication*: a span the Q&A
model put in quotes that is simply not present in the cited target. It is
**deterministic** -- no model is ever invoked -- and operates over a
``Support``/``Claim`` plus the already-resolved **body text** of the cited
target. Resolving a ``version_id``/``snapshot_id`` to its bytes is the caller's
job (the storage core, ``docs/storage.md``); this module deliberately never
touches a store, so it has nothing to couple to before that core exists.

Scope is step 1 only. Extractive coupling (step 2) and NLI entailment (step 3)
are later ``lode-1k3`` tickets; the actual drop / flag / abstain orchestration
(step 4-5) is ``lode-1k3.5``. This module supplies the *verdict* those stages
consume -- it reports whether a span is verbatim-present; it does not itself
drop claims or decide abstention.
"""

import re
from collections.abc import Mapping

from lode.answer import Claim, Support

_WHITESPACE = re.compile(r"\s+")


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
