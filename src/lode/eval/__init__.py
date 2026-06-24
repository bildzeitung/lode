"""lode eval harness (``docs/design.md`` §7).

The eval harness is a first-class step-1 deliverable: a small held-out Q&A set
scored on retrieval recall@k, citation/faithfulness accuracy, and abstention
correctness. This package holds the deterministic **seed corpus** fixture
(``lode.eval.seed``, lode-5y8.4) and the **golden Q&A set** (``lode.eval.golden``,
lode-5y8.3) built on it; the scorer (lode-5y8.1) consumes both.
"""
