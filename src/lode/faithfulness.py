"""The faithfulness gate, steps 1-3: span check, extractive coupling, NLI entailment.

``docs/retrieval.md`` ("The faithfulness gate"), steps 1-3:

    1. Verbatim-span check (deterministic, v1). Every ``quoted_span`` must occur
       (exact, or normalized-whitespace) in the body of its cited
       ``version_id``/``snapshot_id``. No model, no latency.
    2. Extractive coupling (deterministic, v1). A fast path: if the claim's
       load-bearing payload lies inside the quoted span, the claim is verified
       outright -- stopping an inverted quote paired with a contradicting claim,
       or a drifted number that is both quoted-verbatim and wrong.
    3. NLI entailment (local cross-encoder, v1). A claim that passes step 1 but
       is *not* extractively coupled -- genuine multi-note synthesis, or
       paraphrase sitting outside any single span -- is scored by a local NLI /
       cross-encoder for whether its cited spans **jointly entail** it; it
       survives only above a deliberately conservative threshold, else drops
       (fail-closed).

Steps 1-2 are **deterministic** -- no model is ever invoked. Step 1 (lode-1k3.2)
operates over a ``Support``/``Claim`` plus the already-resolved **body text** of
the cited target; resolving a ``version_id``/``snapshot_id`` to its bytes is the
caller's job (the storage core, ``docs/storage.md``), so this module deliberately
never touches a store. Step 2 (lode-1k3.3) is purely ``claim.text`` against its
own ``quoted_span`` -- step 1 already proved the span is in the body, so coupling
needs no body and resolves nothing. Step 3 (lode-1k3.4) reaches a **local model**
behind the :class:`EntailmentScorer` seam (a Protocol + a lazily-loaded
FastEmbed-backed default + an injectable seam so tests stay offline), mirroring
the rerank cross-encoder seam (``lode.retrieval``); it too needs no body, because
step 1 already proved every span present.

The actual drop / flag / abstain orchestration (step 4-5) is ``lode-1k3.5``. This
module supplies the *verdicts* those stages consume -- whether a span is
verbatim-present, whether a claim is extractively coupled, and whether its spans
entail it; it does not itself drop claims, sequence the stages, or decide
abstention (that staging lives in ``gate.py``).
"""

import math
import re
from collections.abc import Mapping
from typing import Protocol

from lode.answer import Claim, Support
from lode.config import Settings, model_cache_dir

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


def locate_span(
    span: str, body: str, *, hint: int | None = None
) -> tuple[int, int] | None:
    """``(start, end)`` offsets of ``span`` within ``body``, or ``None`` if absent.

    This is the single definition of "occurs verbatim" for the whole codebase --
    :func:`span_occurs` is derived from it, and the ask screen's context renderer
    uses the offsets to show surrounding body text. Offsets index ``body`` as
    given, never a normalized copy.

    Exact substring is tried first (the common case, and a strict subset of the
    flexible match); failing that, the span's whitespace-separated tokens are
    searched joined by ``\\s+``, so a span differing from the body only by
    reflowed whitespace still locates. That second pass accepts exactly what
    ``normalize_whitespace(span) in normalize_whitespace(body)`` accepts, but
    unlike normalizing both sides it preserves the mapping back to ``body``'s own
    offsets -- which is why the locator, not the boolean, is the primitive. No
    model is involved: this is a pure string search.

    When ``span`` occurs more than once, ``hint`` -- a caller-supplied char offset
    into ``body`` (e.g. the retrieved passage a citation actually came from,
    lode-hruz) -- picks the occurrence, exact OR whitespace-flexible, whose start
    is nearest to it, rather than always the leftmost exact match. ``hint=None``
    (the default) keeps the original leftmost-exact-else-leftmost-flexible
    behavior, so every existing caller is unaffected.
    """
    tokens = span.split()
    flexible = r"\s+".join(re.escape(token) for token in tokens) if tokens else None

    if hint is None:
        start = body.find(span)
        if start != -1:
            return start, start + len(span)
        if flexible is None:
            return None
        found = re.search(flexible, body)
        return found.span() if found else None

    candidates = [
        (match.start(), match.start() + len(span))
        for match in re.finditer(re.escape(span), body)
    ]
    if flexible is not None:
        candidates += [match.span() for match in re.finditer(flexible, body)]
    if not candidates:
        return None
    return min(candidates, key=lambda span_range: abs(span_range[0] - hint))


def span_occurs(span: str, body: str) -> bool:
    """Whether ``span`` occurs verbatim in ``body`` -- exact, or normalized-whitespace.

    The boolean face of :func:`locate_span`; see there for the matching rule. Kept
    as its own name because the gate reads better as a yes/no question.
    """
    return locate_span(span, body) is not None


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


#: Standalone words that carry negation on their own. Matched as whole tokens
#: (via :func:`_word_tokens`), so no substring can raise a false cue.
_NEGATION_WORDS = frozenset(
    {
        "not",
        "no",
        "never",
        "cannot",
        "nothing",
        "nobody",
        "none",
        "neither",
        "nor",
        "without",
    }
)

#: The other negation form: an ``n't`` contraction, matched whole on the **raw
#: text** rather than as a token, because ``_WORD`` splits on the apostrophe and
#: leaves only a stem ("isn't" -> "isn" + "t") -- a stem list would have to
#: either collide with real words ("don", "won") or miss ``don't``/``won't``.
#: Matching whole also keeps this independent of where ``_WORD`` draws its
#: boundaries. The typographic apostrophe is accepted alongside the ASCII one.
_CONTRACTED_NOT = re.compile(r"\w+n['’]t\b")


def _negation_cues(text: str) -> frozenset[str]:
    """The negation cues ``text`` carries (lode-w2y7).

    Used for an **asymmetry** test: a cue in a ``quoted_span`` but absent from
    the claim means the span negates something the claim does not, so the two
    must not couple (see :func:`claim_extractively_coupled`). A lexical
    heuristic, not a polarity parser -- affixal negation ("unchanged"), hedges
    ("fails to"), and negation *scope* are out of reach, and each miss simply
    leaves the pre-fix fail-open behaviour for that input. Full rationale and
    the residual exposure: ``docs/retrieval.md``.
    """
    folded = text.casefold()
    return (_word_tokens(folded) & _NEGATION_WORDS) | frozenset(
        _CONTRACTED_NOT.findall(folded)
    )


def claim_extractively_coupled(claim: Claim) -> bool:
    """Whether ``claim``'s load-bearing payload lies inside one of its cited spans.

    The **extractive-coupling fast path** (``docs/retrieval.md`` step 2): the
    claim's load-bearing payload is its word tokens minus grammatical glue
    (:data:`_STOPWORDS`); the claim is coupled iff **some single** ``quoted_span``
    contains every one of those tokens **and** carries no negation cue
    (:func:`_negation_cues`) that the claim itself lacks -- otherwise containment
    alone would couple a claim to a span that negates it (lode-w2y7). A claim
    whose payload is split *across* spans is genuine **synthesis**, not
    extractive -- it is deliberately not coupled here and falls through to the
    NLI stage (lode-1k3.4); so is a claim blocked by the negation check, which
    is the correct fail-closed outcome, not a drop.

    This is a pure ``claim.text``-vs-``quoted_span`` check; it takes no bodies,
    because step 1 (:func:`claim_spans_verified`) already proved each span is
    present in its cited target. A claim with no load-bearing token (all glue)
    couples with nothing -- there is no payload to find inside a span -- so it
    fails closed and is left for a later stage rather than vacuously verified.
    """
    payload = _word_tokens(claim.text) - _STOPWORDS
    if not payload:
        return False
    claim_cues = _negation_cues(claim.text)
    for support in claim.support:
        if not payload <= _word_tokens(support.quoted_span):
            continue
        if _negation_cues(support.quoted_span) - claim_cues:
            continue  # span negates something the claim doesn't -- not coupled
        return True
    return False


class EntailmentScorer(Protocol):
    """Scores whether a premise entails a hypothesis, in ``[0, 1]`` (higher = stronger).

    The one seam between :func:`claim_entailed` and the NLI model -- the
    faithfulness twin of :class:`lode.retrieval.CrossEncoder`. Production uses
    :class:`FastEmbedEntailmentScorer` (the pinned ``entailment_model`` on the
    shared ONNX runtime); tests pass a stub so the gate never downloads a model.
    """

    def entailment(self, premise: str, hypothesis: str) -> float:
        """Return an entailment score in ``[0, 1]`` for ``premise`` entailing ``hypothesis``."""
        ...


def _sigmoid(logit: float) -> float:
    """Squash a raw cross-encoder logit into a ``[0, 1]`` entailment probability.

    The numerically stable two-branch form, so a large-magnitude logit never
    overflows ``math.exp`` (the reranker logits sit in a small range, but the
    guard costs nothing and keeps the score a clean probability).
    """
    if logit < 0.0:
        exp = math.exp(logit)
        return exp / (1.0 + exp)
    return 1.0 / (1.0 + math.exp(-logit))


class FastEmbedEntailmentScorer:
    """Default :class:`EntailmentScorer`: the pinned cross-encoder repurposed as NLI.

    ``fastembed`` ships no dedicated NLI model, so the local cross-encoder
    (``settings.entailment_model`` -- ``BAAI/bge-reranker-base``, lode-txh.6) is
    repurposed as the entailment scorer via ``fastembed``'s ``TextCrossEncoder``,
    on the **same ONNX runtime** as the embedder and reranker -- on-box, no
    separate loader, no egress (``docs/stack.md`` "Faithfulness NLI"). The model
    is loaded lazily on first :meth:`entailment` call (mirroring
    :class:`lode.retrieval.FastEmbedCrossEncoder`), so a gate run in which no
    claim reaches step 3 never downloads or loads it. Weights are cached under
    :func:`lode.config.model_cache_dir` (``$LODE_HOME/models/``), same as the
    embedder and reranker, so the download survives a reboot (lode-gmo). The
    cross-encoder's raw relevance logit for the (claim, spans) pair is squashed
    to a ``[0, 1]`` entailment probability (:func:`_sigmoid`). The model +
    threshold ship untuned, revisited against the eval harness
    (``docs/decisions.md``).
    """

    def __init__(self, settings: Settings) -> None:
        self._model_name = settings.entailment_model
        self._model: object | None = None

    def _load(self) -> object:
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._model = TextCrossEncoder(
                model_name=self._model_name, cache_dir=str(model_cache_dir())
            )
        return self._model

    def warm(self) -> None:
        """Force the weights download/load now, ahead of any entailment call.

        The public seam ``lode models pull`` (lode-6qh) warms the cache through,
        so the CLI does not depend on the private :meth:`_load`.
        """
        self._load()

    def entailment(self, premise: str, hypothesis: str) -> float:
        model = self._load()
        # The cross-encoder scores a document's relevance to a query; framing the
        # claim as the query and the cited spans as the document turns that into
        # an entailment proxy. One pair in, one logit out, sigmoid'd to [0, 1].
        (logit,) = model.rerank(hypothesis, [premise])
        return _sigmoid(float(logit))


def claim_entailed(claim: Claim, scorer: EntailmentScorer, *, threshold: float) -> bool:
    """Whether ``claim``'s cited spans **jointly entail** it, at or above ``threshold``.

    The **entailment check** (``docs/retrieval.md`` step 3): a claim that passed
    the verbatim-span check but is *not* extractively coupled -- genuine
    multi-note synthesis, or legitimate paraphrase that sits outside any single
    span -- is judged here. Its cited ``quoted_span``s are joined into one premise
    (so the spans are weighed *jointly*, as the design requires) and scored
    against the claim text by ``scorer``; the claim is entailed iff the score
    reaches ``threshold``.

    The check is **conservative and fail-closed**: ``threshold`` ships high and
    untuned (``Settings.entailment_threshold``), so a claim the model does not
    confidently support is dropped -- the same posture as a fabricated quote.
    Step 1 (:func:`claim_spans_verified`) already proved every span present, so
    this needs no bodies; the spans are verbatim text from the cited targets.
    """
    premise = " ".join(support.quoted_span for support in claim.support)
    return scorer.entailment(premise, claim.text) >= threshold
