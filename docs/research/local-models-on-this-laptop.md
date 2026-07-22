# Local open-weight models for lode on a Ryzen AI laptop (research)

_Researched 2026-07-22 · deep-research harness · 28 sources fetched · 112 claims
extracted → 25 adversarially verified (3-vote) → 22 confirmed, 3 refuted._

This is **reference research**, not a lode design fiat. It captures a
**hypothetical**: could lode's two cloud-LLM surfaces run on locally-hosted
open-weight models on one specific laptop, at roughly ≥75% of Claude quality?
No ticket was filed or changed off the back of it, and it commits lode to
nothing. Settled design decisions still belong in [`docs/`](../design.md); the
vendor-abstraction work is tracked separately on epic **lode-568v** (Anthropic ↔
OpenAI/Azure), which this exercise deliberately did **not** touch.

The bar — "within ~75% of Claude" — was **never measured head-to-head** on
lode's own tasks by any source. Every verdict below is an inference from
current benchmarks and citation-faithfulness research, not a local-vs-Claude
run on lode's corpus. Read it as a feasibility sketch, not a result.

---

## The laptop (the binding constraint)

- **CPU:** AMD Ryzen AI 9 HX 370 (Strix Point) — 24 threads (12 Zen 5 cores).
- **NPU:** integrated XDNA 2, ~50 TOPS.
- **iGPU:** Radeon 890M (RDNA 3.5), shares system RAM. No discrete GPU.
- **RAM:** 31 GB visible inside WSL2 (host likely 64 GB); ~28 GB realistically
  usable for weights + KV cache.
- **OS reality:** lode runs in **WSL2 Linux**, where the iGPU appears only as
  the Microsoft basic render driver — so ROCm/Vulkan GPU compute **and** the NPU
  are unreachable from inside WSL2. Any accelerated path is a **Windows-host
  server** reached over localhost.

## Summary

The ~75% bar splits cleanly along lode's two surfaces:

- **Enrichment (forced structured extraction): likely reachable.** A local
  model under token-level grammar-constrained decoding (llama.cpp GBNF) can
  produce reliably schema-valid annotations — *if the schema stays flat*.
- **Cited Q&A (citation faithfulness): the limiting factor, likely NOT
  reachable out of the box.** Grounded citation faithfulness in 8B-class open
  models is not fixable by prompting; it needs task-specific alignment
  (Trust-Align-style DPO) or internals-based attribution (MIRAGE) — real
  training/engineering, not configuration. This is lode's core bet, so it is the
  surface you cannot cheaply localize.

The pragmatic shape is therefore a **hybrid: local enrichment, cloud Q&A** — and
that shape has a governance consequence noted at the end.

## The NPU: belief vs. reality

The prompting belief under test was that **Ollama uses the XDNA2 NPU**. It does
not (verified 3-0).

- **Ollama** offloads only to GPU (ROCm/Vulkan); on this chip it runs
  **CPU-only**. Its open issues (#11199, #5186, #15878) show NPU support
  unimplemented as of Ollama 0.22.0.
- The NPU **is** reachable — by purpose-built runtimes, both of which expose
  **OpenAI-compatible localhost endpoints** (i.e. they would drop into lode's
  provider seam as "just another vendor URL"):
  - **FastFlowLM (FLM)** — NPU-only, "think Ollama but for NPUs." Runs quantized
    LLMs fully on the XDNA2 NPU, supports Strix (the HX 370), Server Mode on
    **port 52625** with `/v1/chat/completions`. Caveat: NPU-first with CPU
    fallback for some high-precision layers ("no CPU load" is a mild
    simplification).
  - **AMD Lemonade Server** — NPU+iGPU **hybrid** (NPU does prefill/TTFT, iGPU
    does decode/token-gen), OpenAI-compatible on **port 13305**, LiteLLM ships a
    `lemonade` provider. Caveat: the hybrid path wants **ONNX/OGA-format**
    models, not arbitrary GGUF. GAIA sits on top of this same service.

**Windows-host, not WSL2.** Both NPU runtimes run host-side. A "Windows-only"
framing was actually *refuted* (0-3) — FastFlowLM/Lemonade have added a Linux
NPU-only path — but that path is still **host-side**, not WSL2-iGPU/ROCm. So the
architecture is unchanged: Windows-host server, called from WSL over localhost
(the Docker-as-routing-bridge topology fits here — Docker bridges the network,
it does **not** attach the NPU/iGPU to a WSL2 container; no passthrough path for
AMD's NPU/iGPU into WSL2 Docker exists in mid-2026).

## Throughput reality (this exact chip)

From a LocalScore run on the "AMD Ryzen AI 9 HX 370 w/ Radeon 890M":

| Model                 | Quant  | Gen tok/s | Notes                    |
|-----------------------|--------|-----------|--------------------------|
| Llama 3.2 1B          | Q4_K_M | ~67       | tiny                     |
| Llama 3.1 8B          | Q4_K_M | ~12.9     | 99 tok/s prompt          |
| Qwen2.5 14B           | Q4_K_M | ~7.1      | ~26 s time-to-first-token |

NPU via FastFlowLM reaches ~22–37 tok/s on "standard" small models and ~50+
tok/s only on **1–3B** models (Llama 3.2 1B ≈ 53 tok/s at 1k ctx) — too small
for the harder Q&A task. **Memory bandwidth (LPDDR5X) is the ceiling**; the
accelerators do not rescue large models. Realistic split: CPU llama.cpp inside
WSL for correctness/simplicity, or a Windows-host NPU/iGPU server for speed on
small models.

_Caveat: the LocalScore run does not disclose CPU vs iGPU-Vulkan backend, and
the NPU numbers lean on an enthusiast blog + vendor-derived DeepWiki. No source
benchmarked a 20B model on the NPU._

## Candidate models (fit the ~28 GB budget)

- **Enrichment / tool-use:** `gpt-oss-20b` (Apache-2.0, 128K ctx, MXFP4 4-bit,
  ~12–13 GiB weights → fits with KV headroom), or a small tool-use specialist
  like `Llama-3-Groq-8B-Tool-Use` (~6–8 GB at Q4; 89.06% BFCL — but that is
  **v1/2024**, not the harder v2/v3). Don't over-index on any single pick:
  a claim that Qwen3-32B is the best modern tool-caller was **refuted** (0-3).
- **Q&A:** the frontier open-weight models that would close the faithfulness gap
  (DeepSeek V4 Flash ~284B/13B-active, MiniMax M3, Nemotron 3 Ultra) are **far
  too large** for ~28 GB even quantized.

## Structured output: achievable, within limits

The enrichment hard requirement — strict schema adherence — is locally
enforceable. **llama.cpp GBNF** sets logits of grammar-violating tokens to −∞
before sampling, forcing valid JSON, and auto-converts a **subset** of JSON
Schema to GBNF. But coverage varies widely: on **JSONSchemaBench**, the best
framework (Guidance) hit only ~41% empirical coverage on *hard* real-world
schemas (llama.cpp 39%, XGrammar 28%, Outlines 3%) versus ~96% on *easy* ones.
**Implication: keep lode's enrichment schema flat/simple** to stay inside the
reliably-coverable subset. (Coverage ≠ end-to-end extraction accuracy, which no
source measured.)

## Citation faithfulness: the wall

Verified 3-0: prompting alone does **not** reliably improve groundedness —
models become oversensitive, swinging between exaggerated refusals and
over-responsiveness. Faithful citation in an 8B model *is* improvable, but only
via:

- **Trust-Align (ICLR 2025)** — DPO-style alignment; beat prompting in 26/27
  configs, raising grounded-citation F1 on LLaMA-3-8B by +22 (ASQA) / +38
  (QAMPARI) / +5.5 (ELI5) points over the FRONT baseline.
- **MIRAGE (EMNLP 2024)** — attributes answers via model-internal saliency
  rather than self-citation; reaches self-citation-comparable quality, but
  **requires internals access** — so it applies only to a local open model,
  never the Claude API.

Both are engineering/training efforts, not config changes. That is why cited
Q&A is the surface that does not cheaply localize.

## The governance consequence (why this is more than a benchmark)

The only sensible local play is the **hybrid**: enrichment local, Q&A cloud.
But that is **per-surface provider selection** — which lode-568v's current
resolved decision explicitly **forecloses** ("to set a provider is to set it for
all of lode; no granularity beyond whole-app for the vendor"). Under whole-app
provider, all-local is all-or-nothing, and "all" fails on Q&A.

So this research is not just a feasibility note — it is the concrete case the
whole-app decision rules out. If local models are ever wanted, the prerequisite
is reopening the **provider-granularity** decision to allow per-surface vendors
(local enrichment endpoint + cloud Q&A). Recorded here as a pointer, not a
recommendation; no ticket was changed.

## Sources (primary)

- FastFlowLM — https://github.com/FastFlowLM/FastFlowLM
- AMD Lemonade Server — https://www.amd.com/en/developer/resources/technical-articles/unlocking-a-wave-of-llm-apps-on-ryzen-ai-through-lemonade-server.html
- AMD GAIA — https://www.amd.com/en/developer/resources/technical-articles/gaia-an-open-source-project-from-amd-for-running-local-llms-on-ryzen-ai.html
- LocalScore (this chip) — https://www.localscore.ai/accelerator/721
- JSONSchemaBench — https://arxiv.org/pdf/2501.10868
- llama.cpp GBNF grammars — https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md
- Trust-Align — https://arxiv.org/html/2409.11242v4
- MIRAGE — https://arxiv.org/pdf/2406.13663
