"""lode eval harness (``docs/design.md`` §7).

The eval harness is a first-class step-1 deliverable: a small held-out Q&A set
scored on retrieval recall@k, citation/faithfulness accuracy, and abstention
correctness. This package currently holds the deterministic **seed corpus**
fixture (``lode.eval.seed``, lode-5y8.4) the golden Q&A set (lode-5y8.3) and the
scorer (lode-5y8.1) build on.
"""
