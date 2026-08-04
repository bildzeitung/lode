"""Golden Q&A set for the eval harness (lode-5y8.3).

``docs/design.md`` §7 makes a small held-out Q&A set a first-class step-1
deliverable, scored on three things: **retrieval recall@k**, **citation /
faithfulness accuracy**, and **abstention correctness**. This module is that
held-out set -- the regression surface the scorer (lode-5y8.1) and every tuning
knob (rerank, the entailment threshold, chunk size) measure against.

Each :class:`GoldenItem` carries exactly what the three metrics need:

* **recall@k** -- ``relevant_version_ids``: the set of seed-note versions a
  correct retrieval must surface for this question.
* **citation / faithfulness** -- ``citations``: the *known-good* citations, each a
  ``version_id`` plus a **verbatim span** copied from that version's body. They
  are real, schema-valid :class:`lode.answer.Support` evidence (the same shape a
  Q&A answer returns) and every span passes the verbatim-span check
  (:func:`lode.faithfulness.span_occurs`) -- the tests enforce both, so a golden
  citation can never silently go stale or fabricate a quote.
* **abstention** -- an item with no relevant versions is an **out-of-corpus**
  question: the seed notes do not answer it, so the only correct behaviour is to
  abstain ("your notes don't answer this", ``docs/retrieval.md``). ``abstain``
  reports this; the scorer checks the system actually abstained.

Reproducibility: the set is authored against the seed fixture's stable, human-
readable ``note_id``s and resolves each to the fixture's reproducible
``version_id`` at load via :func:`lode.eval.seed.seed_notes` -- it never hardcodes
a hash. So the golden citations reference exactly the ids the real version-save
path produces for the committed bodies, and a corpus change that moves an id
surfaces as a loud test failure rather than a stale citation.

Note on curation: this is an *initial* seed. The exact metric weighting and the
curation methodology remain an open sub-question (``docs/decisions.md``); this
module only supplies the data, it does not settle how the scorer weights it.
"""

from dataclasses import dataclass

from lode.eval.seed import seed_notes


@dataclass(frozen=True)
class GoldenCitation:
    """One known-good citation: a verbatim span of a specific seed-note version.

    ``version_id`` is a seed fixture version id (resolved from a ``note_id`` at
    load); ``quoted_span`` is text copied verbatim from that version's body and is
    guaranteed verbatim-present by the tests. Shapes 1:1 onto a
    :class:`lode.answer.Support`.
    """

    version_id: str
    quoted_span: str


@dataclass(frozen=True)
class GoldenItem:
    """One golden question with its expected retrieval + citation evidence.

    An *answerable* item has one or more ``relevant_version_ids`` (the notes a
    correct retrieval must surface) and one or more ``citations`` drawn from them.
    An *out-of-corpus* item has neither -- the correct behaviour is to abstain.
    """

    question: str
    relevant_version_ids: frozenset[str]
    citations: tuple[GoldenCitation, ...]

    @property
    def abstain(self) -> bool:
        """Whether the only correct answer is abstention (no note answers this)."""
        return not self.relevant_version_ids


# Answerable questions, authored as ``(question, ((note_id, verbatim_span), ...))``.
# A span must occur verbatim (exact or normalized-whitespace) in the cited note's
# body; the gold-relevant set for recall@k is exactly the notes the citations are
# drawn from. ``test_eval_golden`` enforces both.
_ANSWERABLE: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Why doesn't a Postgres UPDATE overwrite a row in place?",
        (
            (
                "01-postgres-autovacuum",
                (
                    "an UPDATE does not overwrite a row in place: it writes a new row "
                    "version and marks the old one as a dead tuple"
                ),
            ),
        ),
    ),
    (
        "What is the default Postgres autovacuum scale factor and what does it imply?",
        (
            (
                "01-postgres-autovacuum",
                (
                    "The default scale factor is 0.2, which means a large table is "
                    "vacuumed only after 20 percent of its rows are dead"
                ),
            ),
        ),
    ),
    (
        "How can Postgres transaction-ID wraparound be monitored?",
        (
            (
                "01-postgres-autovacuum",
                "Monitoring age(datfrozenxid) is the early warning for that",
            ),
        ),
    ),
    (
        "Why are Python threads useful for I/O-bound but not CPU-bound work?",
        (
            (
                "02-python-gil",
                (
                    "Threads are therefore useful for I/O-bound work, where they "
                    "overlap waiting on the network or disk, but they do not speed up "
                    "CPU-bound pure Python code"
                ),
            ),
        ),
    ),
    (
        "What is the standard approach for CPU-bound work in CPython?",
        (
            (
                "02-python-gil",
                (
                    "For CPU-bound work the standard answer is multiprocessing, which "
                    "runs separate interpreter processes each with its own GIL"
                ),
            ),
        ),
    ),
    (
        "How does NumPy avoid the GIL bottleneck?",
        (
            (
                "02-python-gil",
                "their heavy loops release the GIL while running in C",
            ),
        ),
    ),
    (
        "What triggered the checkout latency spike on 2026-03-14?",
        (
            (
                "03-incident-checkout-latency",
                (
                    "a deploy that added a synchronous call to the fraud-scoring "
                    "service inside the request path, with no timeout configured"
                ),
            ),
        ),
    ),
    (
        "Why did the checkout thread pool become exhausted during the incident?",
        (
            (
                "03-incident-checkout-latency",
                (
                    "Because the HTTP client had no timeout, threads were held until "
                    "the upstream eventually closed the connection, exhausting the "
                    "checkout thread pool"
                ),
            ),
        ),
    ),
    (
        "What three-part fix resolved the checkout latency incident?",
        (
            (
                "03-incident-checkout-latency",
                (
                    "set a 250ms timeout on the fraud call, turn on the async-scoring "
                    "fallback flag, and add a circuit breaker that trips after five "
                    "consecutive timeouts"
                ),
            ),
        ),
    ),
    (
        "What is the difference between a Kubernetes readiness and liveness probe?",
        (
            (
                "04-k8s-probes",
                "A readiness probe decides whether a pod should receive traffic",
            ),
            (
                "04-k8s-probes",
                "A liveness probe decides whether a pod should be restarted",
            ),
        ),
    ),
    (
        "Why shouldn't a liveness probe check downstream dependencies?",
        (
            (
                "04-k8s-probes",
                (
                    "If the database is down, every pod fails liveness and restarts in "
                    "a storm, which makes the outage worse"
                ),
            ),
        ),
    ),
    (
        "How does git bisect locate the commit that introduced a bug?",
        (
            (
                "05-git-bisect",
                (
                    "git bisect does a binary search through commit history to find "
                    "the commit that introduced a bug"
                ),
            ),
        ),
    ),
    (
        "How can git bisect be fully automated?",
        (
            (
                "05-git-bisect",
                "git bisect run ./test.sh",
            ),
        ),
    ),
    (
        "How long are our Let's Encrypt certificates valid and when are they renewed?",
        (
            (
                "06-tls-cert-renewal",
                "issued for 90 days",
            ),
            (
                "06-tls-cert-renewal",
                (
                    "cert-manager controller in the cluster requests renewal "
                    "automatically once a certificate is within 30 days of expiry"
                ),
            ),
        ),
    ),
    (
        "Why does cert-manager use the DNS-01 challenge instead of HTTP-01?",
        (
            (
                "06-tls-cert-renewal",
                (
                    "DNS-01 proves control of the domain by creating a TXT record, "
                    "which is why it works for wildcard certificates where HTTP-01 "
                    "does not"
                ),
            ),
        ),
    ),
    (
        "Which Redis eviction policy do we use for a pure cache and why?",
        (
            (
                "07-redis-eviction",
                (
                    "For a pure cache we use allkeys-lru, which evicts the "
                    "least-recently-used key regardless of whether it has a TTL"
                ),
            ),
        ),
    ),
    (
        "Why is noeviction a dangerous default for a Redis cache?",
        (
            (
                "07-redis-eviction",
                "noeviction, which makes writes fail with an error once memory is full",
            ),
            (
                "07-redis-eviction",
                "a full cache that refuses writes looks like an outage",
            ),
        ),
    ),
    (
        "How do idempotency keys prevent double-charging a card?",
        (
            (
                "08-http-idempotency",
                (
                    "an Idempotency-Key header, a client-generated unique value that "
                    "the client reuses on every retry of the same logical request"
                ),
            ),
            (
                "08-http-idempotency",
                "the server returns the stored result instead of charging again",
            ),
        ),
    ),
    (
        "What happens if a client reuses an idempotency key with a different amount?",
        (
            (
                "08-http-idempotency",
                (
                    "If a client reuses a key with a different amount, that is a client "
                    "bug, and the server returns a 422"
                ),
            ),
        ),
    ),
    (
        "How long does the primary on-call have to acknowledge a page before it escalates?",
        (
            (
                "09-oncall-escalation",
                "15 minutes to acknowledge an alert",
            ),
            (
                "09-oncall-escalation",
                "it escalates automatically to the secondary on-call",
            ),
        ),
    ),
    (
        "How are Sev-1 incidents escalated differently from normal pages?",
        (
            (
                "09-oncall-escalation",
                (
                    "skip the timed escalation: the incident commander pulls in the "
                    "secondary and the manager immediately"
                ),
            ),
        ),
    ),
    (
        "What did the team decide about feature flag expiry dates?",
        (
            (
                "10-feature-flag-cleanup",
                (
                    "every new feature flag must carry an expiry date in its "
                    "definition, defaulting to 90 days out"
                ),
            ),
        ),
    ),
    (
        "Which feature flags are exempt from the expiry rule?",
        (
            (
                "10-feature-flag-cleanup",
                (
                    "Permanent operational toggles, such as a kill switch for an "
                    "external dependency, are exempt"
                ),
            ),
        ),
    ),
    # Multi-note synthesis: a correct answer must retrieve and cite both notes.
    (
        (
            "Across our notes, how does a single slow dependency without a timeout "
            "cascade into a wider outage?"
        ),
        (
            (
                "03-incident-checkout-latency",
                (
                    "Because the HTTP client had no timeout, threads were held until "
                    "the upstream eventually closed the connection, exhausting the "
                    "checkout thread pool"
                ),
            ),
            (
                "04-k8s-probes",
                (
                    "If the database is down, every pod fails liveness and restarts in "
                    "a storm, which makes the outage worse"
                ),
            ),
        ),
    ),
    (
        "Which two of our policies both use a 90-day window?",
        (
            (
                "06-tls-cert-renewal",
                "issued for 90 days",
            ),
            (
                "10-feature-flag-cleanup",
                "defaulting to 90 days out",
            ),
        ),
    ),
)


# Out-of-corpus questions: plausible, but none of the seed notes answer them, so
# the only correct behaviour is to abstain. These exercise abstention correctness.
_ABSTAIN: tuple[str, ...] = (
    "What is our company's parental leave policy?",
    "How do I configure Kafka consumer group rebalancing?",
    "What is the recommended JVM heap size for our Elasticsearch nodes?",
    "Which cloud region hosts our primary Postgres database?",
    "How do we rotate our database user passwords?",
    "What is the monthly cost of our Redis cluster?",
    "How does our mobile app handle offline sync?",
    "What is the retention period for our application logs?",
)


def golden_set() -> tuple[GoldenItem, ...]:
    """Load the golden Q&A set, resolving each ``note_id`` to a fixture version id.

    Deterministic by construction: the authored questions and spans are constant
    and ``note_id``s resolve through :func:`lode.eval.seed.seed_notes`, which is
    itself deterministic. A ``note_id`` that is not in the fixture raises
    ``KeyError`` -- a loud guard against a citation that references a note the
    corpus no longer contains.
    """
    version_by_note = {note.note_id: note.version_id for note in seed_notes()}
    items: list[GoldenItem] = []
    for question, raw_citations in _ANSWERABLE:
        citations = tuple(
            GoldenCitation(version_by_note[note_id], span)
            for note_id, span in raw_citations
        )
        relevant = frozenset(citation.version_id for citation in citations)
        items.append(GoldenItem(question, relevant, citations))
    for question in _ABSTAIN:
        items.append(GoldenItem(question, frozenset(), ()))
    return tuple(items)
