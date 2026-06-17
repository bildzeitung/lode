# lode — Retrieval

The pipeline behind the primary bet (cited Q&A — [design.md](design.md) §2). It runs over the
**heterogeneous graph** of owned notes + mirrored externals; the node model and the trust gradient
it ends on are defined in [externals.md](externals.md).

---

## Index heads only

For notes, index the current head version; for externals, the latest snapshot, marked with its
age. **Never index full history** — a note edited 5× would return 5 near-duplicate hits and cite a
stale copy. History exists for audit, undo, and annotation migration ([storage.md](storage.md)) —
not retrieval.

---

## The v1 retrieval pipeline is hybrid, fused app-side

```
retrieve(q):
  L     = FTS5.search(q)          # lexical / BM25 — sync, always fresh
  V     = lancedb.search(emb(q))  # dense vector — async cache
  fused = RRF(L, V)               # app-side Reciprocal Rank Fusion (~20 lines)
  top   = rerank(q, fused)        # local cross-encoder stage (toggleable)
  ctx   = graph_expand(top)       # GraphRAG: traverse edges from the seeds
  return trust_rank(ctx)          # trust gradient orders the final context
```

```mermaid
flowchart TD
    Q["query q"] --> L["FTS5.search(q)<br>lexical / BM25<br>sync · always fresh"]
    Q --> EMB["emb(q)<br>local ONNX embedder"]
    EMB --> V["lancedb.search<br>dense vector<br>async cache"]

    L --> RRF["RRF(L, V)<br>app-side Reciprocal Rank Fusion<br>(~20 lines)"]
    V --> RRF

    RRF --> RR["rerank(q, fused)<br>local cross-encoder<br>(toggleable seam)"]
    RR --> GE["graph_expand(top)<br>traverse edges from seeds<br>(GraphRAG)"]
    GE --> TR["trust_rank(ctx)<br>order by trust gradient"]
    TR --> OUT["grounded context<br>→ Q&A LLM (cited answer)"]

    classDef fresh fill:#dff0d8,stroke:#3c763d,color:#1b1b1b;
    classDef cache fill:#d9edf7,stroke:#31708f,color:#1b1b1b;
    class L fresh;
    class V,EMB cache;
```

- **Hybrid, never vector-only.** Pure dense retrieval underperforms on keyword-heavy technical
  queries ("rotate the staging certs"); the **lexical leg carries those** — and, because FTS5 is
  the synchronous index ([design.md](design.md) save path), it also covers a just-written note
  before its vector lands.
- **Fusion is app-side RRF.** One lexical index (FTS5, fresh) + LanceDB for vectors only. LanceDB's
  *own* native hybrid goes unused — its lexical leg would lag (it's the async cache), and RRF over
  our two lists is the same fusion family with zero quality loss and no duplicate index. (So
  LanceDB earns its place on columnar vectors / ANN / metadata filtering — **not** native hybrid.)
- **Reranking is a first-class stage, wired in v1 behind a toggle.** A **local cross-encoder**
  (e.g. `bge-reranker-v2-m3` via the ONNX runtime already shipped for embeddings — no new stack,
  no content leaving the box per [externals.md](externals.md#privacy)) re-scores the fused top-N.
  It's the biggest quality lever and matters most in lode's regime (small corpus, short queries),
  and cited Q&A lives or dies on ranking. The *seam* is non-negotiable (painful to retrofit); the
  *model* is swappable/disableable so it can be A/B'd once there's a real corpus. Don't tune rerank
  models/thresholds pre-data ([decisions.md](decisions.md)).

The final `trust_rank` step applies the **trust gradient** — your note > your annotation > current
external snapshot > stale external snapshot > AI-inferred edge — defined in
[externals.md](externals.md#the-broken-assumption-external-staleness-is-not-topological). The
user's own words are highest-trust; externals corroborate, they do not override.

---

## Faithfulness: verify citations, don't just require them

The primary bet is *cited* Q&A, and the stated value is "hallucinated synthesis is worse than none"
([design.md](design.md) §2). Requiring a citation **field in the response schema** does not deliver
that — an LLM will emit a well-formed citation that doesn't support its claim. A confident answer
with a plausible-but-wrong citation is the **worst** outcome: a hallucination wearing the uniform of
a verified fact, which the user trusts *more* because it's cited. So citation **faithfulness** is
enforced as a pipeline stage, not assumed from the schema.

### Failure modes

1. **Citation–claim mismatch** — cites a note that says the opposite of the claim.
2. **Quote fabrication** — emits a "quoted" span that appears in no version.
3. **Paraphrase drift** — the note is topically right but a specific payload (number, date,
   decision) is wrong.
4. **Unsupported synthesis** — fuses notes A + B into a claim neither supports.

### Make the answer schema verifiable

The Q&A LLM does not return prose + `[note_id]`. It returns a list of **claims**, each carrying the
exact evidence it rests on — pinned to the **version**, not the logical note (bytes drift across
versions), and to a **span**, not the whole note:

```
answer = [
  { text: "<one factual claim>",
    support: [ { version_id | snapshot_id,
                 quoted_span: "<verbatim text from that version>" } ] },
  ...
]
```

### The faithfulness gate (a stage, like rerank)

Runs app-side, after the Q&A LLM returns and before display:

1. **Verbatim-span check (deterministic, v1).** Every `quoted_span` must occur (exact, or
   normalized-whitespace) in the body of its cited `version_id`/`snapshot_id`. No model, no latency.
2. **Extractive coupling (deterministic, v1).** The claim's load-bearing payload must lie **inside**
   the quoted span — not merely sit beside a free-form claim. This stops a model pairing a real but
   inverted quote with a contradicting claim, and stops a drifted number being both quoted-verbatim
   and wrong.
3. **Drop or flag** claims that fail; never silently display them.
4. **Abstain.** If nothing survives the gate, the system says **"your notes don't answer this"** —
   the honest failure mode. Fidelity over fluency means a *willingness to return nothing* rather than
   a confident hallucination.

### What v1 catches — and what it doesn't

The deterministic gate is cheap and free of network/$, but its limits are on the record:

| Failure mode | Deterministic gate (v1) | Needs entailment layer |
|---|---|---|
| 2 · Quote fabrication | **Fully** — the quote isn't in the version | — |
| 1 · Citation–claim mismatch | **Mostly** (with extractive coupling) | residual inversion |
| 3 · Paraphrase drift | **Mostly** (with extractive coupling) | paraphrase outside the span |
| 4 · Unsupported synthesis | **No** — each span exists; the combination isn't checked | yes |

The semantic residue (genuine multi-note synthesis, legitimate paraphrase) needs an **entailment
check**: a **local NLI / cross-encoder** scoring whether each span actually *entails* its claim,
running on the **same ONNX runtime** as the reranker — on-box, no $, no Anthropic round-trip. It is
**deferred behind the gate's seam**, not shipped blind: like rerank, the *stage* is the commitment
and the *model/threshold* waits for the eval harness ([decisions.md](decisions.md)) so it's tuned
against real data, not guessed. An optional LLM-judge second pass can serve as a "high-assurance"
toggle, but it costs a round-trip + dollars per answer and re-ships content off-box, so it is not
the default.
