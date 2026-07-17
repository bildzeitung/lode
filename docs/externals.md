# lode — External sources & the knowledge graph

*(§6)* The AI annotation layer gains read access to external sources — **tickets, source repos,
wikis, email** — and **draws down explicitly linked web pages**, integrating everything into the
knowledge graph. This is added **incrementally, after the core loop works**
([design.md](design.md) §7). The retrieval that runs over this graph lives in
[retrieval.md](retrieval.md).

---

## Externals fit the *same* model

A fetched ticket / wiki page / email / web page is structurally **a note version**: an immutable,
point-in-time snapshot. The whole store collapses to one shape:

> immutable content nodes (some **owned** = your notes, some **mirrored** = externals)
> + a derived annotation/link layer + head pointers.

Axes on a content node: `origin: owned | mirrored`; on derived items: `source: ai | user`.

```mermaid
flowchart TD
    subgraph NODES["Content nodes (heads indexed for retrieval)"]
        N1["owned note A<br>origin: owned"]
        N2["owned note B<br>origin: owned"]
        EXT["JIRA-1234 snapshot<br>origin: mirrored<br>as of fetched_at"]
        WEB["drawn-down web page<br>origin: mirrored"]
    end

    N1 -->|explicit · user-asserted<br>source: user| EXT
    N2 -->|explicit · pasted URL<br>source: user| WEB
    N1 -.->|inferred · suggestion<br>source: ai · confidence| N2

    subgraph TRUST["Trust gradient — orders the final Q&A context (trust_rank)"]
        direction TB
        G1["1 · your note"]
        G2["2 · your annotation"]
        G3["3 · current external snapshot"]
        G4["4 · stale external snapshot"]
        G5["5 · AI-inferred edge"]
        G1 --> G2 --> G3 --> G4 --> G5
    end

    classDef owned fill:#dff0d8,stroke:#3c763d,color:#1b1b1b;
    classDef mirrored fill:#d9edf7,stroke:#31708f,color:#1b1b1b;
    class N1,N2 owned;
    class EXT,WEB mirrored;
```

---

## The broken assumption: external staleness is NOT topological

For owned notes, staleness is free (head moved → you know instantly). For externals, **the true
head lives on someone else's server and changes without telling you.** Consequences:

- **Externals need a refresh policy** (TTL / on-access revalidation / webhook) — there is no
  structural staleness signal. (Per-connector judgment; see [decisions.md](decisions.md). Decided
  for the web connector: a scheduled TTL sweep — see [Refresh policy](#refresh-policy-ttl-based-revalidation-decided-for-web-lode-w0h6) below.)
- **Every AI claim from an external must cite "as of `fetched_at`."** "The ticket is open" is a
  lie; "the ticket was open as of last sync, 3 days ago" is honest.
- **Retrieval uses an explicit trust gradient**, in both ranking and citation display:
  **your note > your annotation > current external snapshot > stale external snapshot >
  AI-inferred edge.** The user's own words are highest-trust; externals corroborate, they do
  not override. This is the `trust_rank` step in [retrieval.md](retrieval.md).

---

## External identity — same two-id split

- `external_id` — stable logical identity: `JIRA-1234`, `repo@path@commit`, email `Message-ID`,
  normalized URL.
- `snapshot_id` — immutable fetched version (content hash).
- One canonical node per `external_id` with many edges — never five copies of a ticket linked
  from five notes. Dedup on `external_id`; version on `snapshot_id`.

### URL canonicalization (decided, `lode-w0h.3`; userinfo stripped, `lode-0as`)

For a web source, `external_id` **is** its canonical URL string — not a hash — so this
canonicalization *is* the dedup correctness "same URL in two notes = one node" depends on, and the
same join key [`lode-w0h.6`](decisions.md)'s refresh policy reuses to find "the same source"
across refetches. Applied in order (`lode.drawdown.canonicalize_url`):

1. Lowercase the scheme and host (path/query stay as-cased — some servers treat them
   case-sensitively).
2. **Drop userinfo (`user[:pass]@`) entirely.** Credentials in a pasted URL are transport
   secrets, not source identity — the same reasoning that strips `utm_*` below, applied to the
   strongest instance of that class. `https://user:pass@host/p`, `https://user@host/p`, and
   `https://host/p` all canonicalize to one `external_id`.
3. Strip the port when it equals the scheme's default (`:80` for `http`, `:443` for `https`).
4. Drop the fragment (`#...`) entirely — never part of server-side identity.
5. Strip query params matching the tracking blocklist (`url_tracking_param_blocklist`,
   [configuration.md](configuration.md); default `utm_*`, `fbclid`, `gclid`).
6. Sort the remaining query params.
7. Normalize the trailing slash: an empty or bare `/` path becomes `/`; any other path loses a
   trailing `/`.

**Why userinfo is a privacy fix, not a dedup nit** (`lode-0as`): `external_id` is a durable
identifier — it is a primary key, it is replicated into `edges.to_id` on every citing note, and
it is reachable from the retrieval candidate set. [Hard delete](#hard-delete-the-deliberate-immutability-break-corrective-half)
scrubs `versions.body`; it does **not** scrub an identifier already copied into edges and
indexes. A credential that reaches `external_id` therefore outlives the content it guarded and
survives a delete that looks complete. Stripping it at canonicalization time is the only point
where the secret can be kept out of storage entirely, rather than redacted after the fact.

**Two adjacent gaps, decided out of scope here (`lode-0as`):** path percent-encoding
(`%7E` vs `~`) and IDN hosts (unicode vs punycode) are **not** normalized, so two URLs that are
"the same page" in those respects still canonicalize to distinct `external_id`s and dedup as two
nodes. This is a deliberate decision, not an oversight: unlike userinfo, neither is a privacy
surface — both are ordinary dedup-correctness gaps, and normalizing either safely needs care
(percent-decoding a path requires respecting RFC 3986 reserved-vs-unreserved characters so a
literal `%2F` isn't mistaken for a path separator; IDN punycoding needs a real IDNA
implementation) that this ticket did not need to take on to close the privacy hole. Left for a
future ticket if a real corpus ever shows it matters.

**Migration note (`lode-0as`, 2026-07-09):** changing `canonicalize_url` changes `external_id`
for any web source already stored under the old (userinfo-preserving) form — a re-key +
edge-repoint would be required for existing rows (the same shape as `_repoint_edges`, used today
for the redirect wrinkle below). Checked at the time of this fix: no `externals` rows and no
`edges.to_id` containing userinfo existed anywhere, so no migration was run. If this is ever
revisited against a store with real web draw-down history, check first.

**The redirect wrinkle:** a note's edge is created against the *pasted* URL's canonical form
before any fetch runs (fetching is the queued async job). If the fetch follows a 3xx chain to a
different final URL, the snapshot is ingested under the *final* URL's canonical `external_id`
instead, and every `source='user'` edge asserted against the pasted-canonical id is re-pointed
onto the final-canonical id — so the note's edge always resolves to the node that actually holds
content, and a persistently-redirecting URL still dedups onto one node.

### Snapshot churn: decouple new snapshot from re-enrich

`snapshot_id = H(external_id ‖ body)` makes an *identical* refetch free (same hash, no new row). But
a chatty external — an active PR refreshed hourly, one new comment each time — produces a **new
snapshot every refresh**, and naively each one would trigger a paid Claude **re-enrichment**
([stack.md](stack.md) Batches). Owned notes have a no-op-save guard; externals need the analogous
cost control, so the two operations are gated separately:

- **Re-embed on any change** — local, cheap, keeps retrieval current.
- **Re-enrich only on *material* change** — gate the expensive enrichment on a local delta between
  the new snapshot and its predecessor; below the bar, **carry the prior enrichment forward**,
  re-anchored to the new snapshot. A one-comment PR update re-embeds but does not re-enrich.

**Materiality signal (`lode-w0h.5`, pinned after debate):** embedding-similarity delta alone, not
size — cosine similarity between the new and predecessor snapshot's mean-pooled passage vectors,
computed **post-embed** (the signal needs the new snapshot's own vectors, so the gate runs from
`lode.worker._embed_handler` right after `lode.embedding.embed` writes them, not from
`ingest_snapshot` at write time). `delta = 1 - similarity`; material iff `delta >=
reenrichment_materiality_threshold`. No predecessor to compare against — the external's first-ever
snapshot, or a predecessor that was never embedded (e.g. a tombstone) — is unconditionally material:
there is nothing to carry forward either. Below the threshold, `lode.externals.gate_reenrich` enqueues
no `enrich` job and instead **carries the prior enrichment forward by re-anchoring it**
(`lode.staleness.reanchor_annotations` / `reanchor_edges`, targeting the external_id) — the identical
quoted-text mechanism `Repository.save` already runs for a note update, not a hand-rolled copy of the
rows. At/above the threshold, one `enrich` job is enqueued for the new `snapshot_id`.

The materiality threshold is a tunable knob ([configuration.md](configuration.md)); it caps cloud
spend on noisy sources without letting enrichment rot.

The `enrich` job the gate enqueues resolves polymorphically (`lode-7qi`): `lode.enrich`'s three entry
points (`enrich_version`, and the Batches API route `submit_enrich_batch`/`collect_enrich_batch` that
actually claims a pending `enrich` job first in production) all resolve `target_version` against
`versions`/`notes` first, falling back to `snapshots`/`externals` — the same blind resolution
`lode.embedding._version_body` already used for the `embed` leg. A material change therefore runs real
Haiku extraction over the snapshot body and writes annotations/edges against the `external_id` (the
same polymorphic `annotations.target` / `edges.from_id` a note's `note_id` writes against).

**The write path** (`lode.externals.ingest_snapshot`, `lode-w0h.2`) is the mirrored analogue of the
note save path: dedup on `external_id` (one `externals` row per source, created on first sight),
compute `snapshot_id` and skip the write entirely when it equals the current head (the identical-
refetch-is-free case above), otherwise insert the new snapshot and move `head_snapshot_id`. An **`ok`**
snapshot then enqueues **`embed`** (never `enrich` — that gate is `lode-w0h.5`'s, decided post-embed)
and drives the **synchronous FTS leg itself** (`lode-c5l`, rebuild of the bounced `lode-w0h.8`) — the
same cache-after-commit shape `Repository.save` uses for owned notes, so an ingested snapshot is
keyword-findable the instant `ingest_snapshot` returns, before the async embed worker ever runs. A
fetch failure ([Draw-down rules](#draw-down-rules) below) writes a *tombstone* snapshot whose body is
the stable, inspectable marker `"[tombstone: <reason>]"` — itself content-addressed, so a source that
keeps failing the same way dedups its tombstones too, rather than growing one row per retry — but
enqueues **no** `embed` job and writes **no** FTS row: a failed fetch must not become a
retrievable/citable hit on either leg (decision, `lode-w0h.2`, 2026-07-08, extended to the FTS leg by
`lode-c5l`; mirrors the owned-note delete path, which likewise is never indexed).

### Refresh policy: TTL-based revalidation (decided-for-web, `lode-w0h.6`)

[decisions.md](decisions.md)'s "External refresh" entry leaves each connector to choose between
**on-access revalidation** and **scheduled background refresh**, both with an eye toward a TTL.
For the web connector: **scheduled TTL sweep, not a true on-access hook.**

**Why not on-access.** A true on-access hook would trigger revalidation the instant a citation
reads a stale external — but every synchronous read path in this codebase is deliberately
network-free (the same rule `Repository.save` follows on the write side: no network I/O inline).
Bolting a blocking HTTP fetch onto an interactive Q&A call would trade a bounded, predictable
citation latency for an unbounded one (a slow or hanging origin server would stall the answer that
happens to cite it). No such hook exists on the retrieval/Q&A read path today, and building one is
out of this ticket's "staleness detection + scheduling only, no second fetch path" scope.

**The policy: ride the reconciliation scan.** `lode.reconcile`'s `refresh_stale` step (`lode-w0h.6`)
runs alongside `embed_gap`/`enrich_gap` — at worker startup and every `--loop`/`--wait` poll tick
(`docs/storage.md` "Reconciliation scan on startup + periodically"). Each pass: find every external
whose current head snapshot is non-tombstone and older than `refresh_ttl_s`
([configuration.md](configuration.md), default 1h) with no `refresh` job already
`pending`/`running`, and enqueue one via the same `lode.jobs.enqueue_derive_jobs` path every other
step uses (`ON CONFLICT DO NOTHING` against `idx_jobs_live` — idempotent, safe to run any time or
frequency). The enqueued job is handled by the *exact same* `lode.drawdown.refresh_external`
(`lode-w0h.3`) the paste-triggered first draw-down uses — this ticket adds no second fetch path, only
the staleness check and the enqueue. A changed body re-embeds and re-enriches-if-material exactly as
any other new snapshot does ([Snapshot churn](#snapshot-churn-decouple-new-snapshot-from-re-enrich)
above); an unchanged refetch is free (identical `snapshot_id`, no new row).

In practice this reads as "revalidate the next time anything drains the queue, if the TTL has
elapsed" rather than "revalidate the instant a citation reads it" — a coarser cadence than true
on-access, but one that never adds latency to a read, and matches
[decisions.md](decisions.md)'s own leaning ("on-access with a short TTL cache ... for a single
instance with finite API quota") in spirit: bounded, cheap, TTL-gated re-fetching, without the
network-on-the-read-path cost on-access would actually require.

**Tombstones are excluded from the sweep, deliberately.** A tombstone head means the source already
failed *permanently* — either a genuine 4xx/empty-extract, or a transient failure that exhausted
every retry and dead-lettered ([Fetch-outcome taxonomy](#fetch-outcome-taxonomy-decided-lode-w0h1)
above; `lode-at8`'s dead-letter hook is what writes that tombstone). Re-fetching it on every TTL
sweep would burn queue churn on a source the draw-down machinery has already classified as
unfetchable-for-now — the same reasoning `lode.reconcile`'s `embed_gap` step already applies (a
tombstone has no body worth re-embedding either). If a dead link recovering later ever matters in
practice, nothing structural prevents it — a user re-pasting the same URL still triggers a fresh
paste-time draw-down (`lode-w0h.3`) regardless of the sweep. Revisit if this proves too conservative.

**Join key: `canonicalize_url`'s narrow canonical form, confirmed correct, not silently inherited.**
This step's staleness/scheduling join key is `externals.external_id` — the *same* canonical URL
string [URL canonicalization](#url-canonicalization-decided-lode-w0h3-userinfo-stripped-lode-0as)
above defines. That section already settles, explicitly and by design, that path percent-encoding
(`%7E` vs `~`) and IDN hosts (unicode vs punycode) are **not** normalized — two "same page" URLs
differing only in those respects are, and remain, two distinct `external_id`s that this policy
schedules and refreshes *independently*, never recognizing them as one source. `lode-w0h.6`
confirms this narrow canonical is correct for the refresh policy too, for the same reason
`lode-0as` gave it: normalizing either safely needs care (RFC 3986 reserved-vs-unreserved handling
for percent-decoding; a real IDNA implementation for punycoding) this connector does not need to
take on, and neither gap is a privacy surface the way userinfo was (userinfo *is* stripped —
`lode-0as` — so no credential ever becomes part of this policy's join key or a refresh job's
`target_version`). Widening the canonical form remains a live option for a future ticket, but would
require a re-key + edge-repoint migration (`lode-0as`'s migration note) — deferred, not silently
assumed away.

**A single default TTL, not yet per-source.** `refresh_ttl_s` is one knob for every external
(only web sources exist today). [decisions.md](decisions.md)'s "a closed ticket rarely changes, an
active PR changes hourly" framing is a real per-source judgment call a future connector (or a
future per-`source_type` override) may want to make — deferred, not built, since there is exactly
one connector and one judgment to make today.

### Externals are directly retrievable

A snapshot's current head is a **direct** lexical/vector candidate on its own content, not only
reachable via graph-expansion from a citing note: `lode.retrieval.live_head_versions` unions each
external's non-tombstone `head_snapshot_id` alongside note heads, so both `lexical_search` and
`vector_search` admit it (`lode-c5l`). A *stale* (non-head) snapshot stays excluded from both direct
legs by construction — only head pointers are read; `trust_rank` still tiers current-vs-stale for a
snapshot reached via graph expansion instead. `lode.embedding.embed`'s body resolution is polymorphic
(versions, then snapshots) so the `embed` job enqueued above runs to completion instead of raising.

**Redact-before-index applies to both legs identically.** The FTS leg chunks
`redact_before_index(body, settings)`, exactly what the vector leg's `embed` independently redacts —
so a secret in a fetched page never lands in `passages_fts`/`passages`/LanceDB on either leg, and both
legs chunk the *same* redacted text (needed for the embed worker's deterministic `passage_id`
`INSERT OR REPLACE` to land on the same rows rather than orphan a stale one). `snapshots.body` itself
is left untouched, exactly as `versions.body` is — see [Two redactions, aimed at the right
legs](#two-redactions-aimed-at-the-right-legs) below.

---

## Edges: explicit vs inferred

- **Explicit** (a note cites `JIRA-1234` or pastes a URL): high confidence, user-asserted edge.
  For a pasted web URL, this is `lode.drawdown.detect_and_enqueue_drawdown` (`lode-w0h.3`),
  called from `Repository.save`: it creates the `source='user'` edge and enqueues the
  `refresh` job that draws the page down — no network I/O in `save` itself, only the edge
  INSERT and the job enqueue, atomically with the version write.
- **Inferred** (AI decides "the auth migration" *is* PR #42): a **suggestion** (`source: ai`,
  confidence-scored), **never an asserted fact**. Surface for confirmation; a user nod promotes
  it. This is where a hallucinated link would silently corrupt the graph — keep it gated.

---

## Draw-down rules

- **Follow explicit links one hop, then stop.** Pull the linked page, extract *its* entities,
  but do not follow that page's links outward. Recursion = unbounded web crawler, not a notes app.
  (This hop limit governs a fetched page's own *outbound links*; it is a separate knob from the
  HTTP redirect cap a single fetch follows — see the fetch-outcome taxonomy below.) Structurally
  enforced, not counted: only the note-save trigger ever scans note text for URLs
  (`lode.drawdown.extract_urls`); the `refresh` job handler
  (`lode.drawdown.refresh_external`, `lode-w0h.3`) never scans a fetched snapshot's body for
  further links.
- **Readability extraction + graceful failure.** Many pages (JS-rendered, paywalled, 403) return
  scaffolding to a naive GET; strip nav/ads, snapshot cleaned text (+ optional raw HTML), and on
  failure write a tombstone snapshot rather than garbage.
- **Shared job type.** The draw-down job reuses the `refresh` value already reserved on
  `jobs.type` — no schema migration. `lode-w0h.3` introduces the fetch→ingest handler
  (`lode.drawdown.refresh_external`, registered in `lode.worker`); the paste-triggered initial
  draw-down is just the first `refresh` of a source, riding the same attempts/backoff/dead-letter
  machinery any later refetch would. `lode-w0h.6`'s refresh policy reuses this handler unchanged
  and adds only staleness detection + scheduling.

### Fetch-outcome taxonomy (decided, `lode-w0h.1`)

One fetch of one URL resolves to exactly one of these outcomes:

| Outcome | Trigger | Result |
|---|---|---|
| **OK** | 2xx response, extractor returns text at/above the length floor | snapshot with `status='ok'` |
| **PERMANENT failure** | 401/403 and any other 4xx *except* 408/429; **or** a 2xx response whose extracted text is empty/`None`/shorter than the length floor (covers JS-rendered scaffolding, paywalled teasers, and empty pages with one signal); **or** a redirect chain longer than the configured cap (loop or merely-too-long — a retry hits the same cap) | snapshot with `status='tombstone'` — retrying will not help |
| **TRANSIENT failure** | 408 or 429 (the two 4xx codes HTTP itself flags "try again later"), any 5xx, or a network/timeout error | **not** written as a snapshot by the fetch unit itself — the caller raises into the async work queue's existing attempts/backoff/dead-letter machinery (`failed` → `pending` retry, → `dead` at max attempts, PINNED `lode-i05.6`); on `dead`, the caller writes a tombstone snapshot so the note edge still resolves |
| **3xx** | one or more redirects, within the configured cap | followed transparently; the *final* resolved URL is what gets canonicalized into `external_id` — a note edge created on the originally-pasted URL may need re-pointing to the final URL's canonical form |

The testable detection signal for "PERMANENT — 2xx but not real content" is the readability
extractor returning `None`/empty **or** text below a configured length floor
([configuration.md](configuration.md)) — no separate paywall- or JS-shell-specific heuristic is
needed. **JS-rendered pages are a permanent tombstone by this rule, deliberately** — actually
rendering them (headless browser / JS execution) is an explicit deferred follow-on (`lode-oni`),
not first-connector scope.

**The HTTP-status half of this table is a shared, connector-neutral classifier (`lode-gpzn.13`).**
`lode.fetch_outcome.classify_http_status` is the single place the OK / TOMBSTONE / TRANSIENT
status-code mapping lives — the web path (`lode.webfetch`) and the Atlassian connectors (JIRA
`lode-gpzn.3`, Confluence `lode-gpzn.4`) all call it rather than each copying the 401/403/404 →
tombstone, 408/429/5xx → transient rule. The extractor-driven "2xx but empty/short content" signal
above stays connector-specific (trafilatura, in `lode.webfetch`) since it has no HTTP-status
analogue. `lode.worker._refresh_dead_letter_hook` (the "on `dead`, the caller writes a tombstone"
step just below) is generic over `source_type` too: it reuses an already-existing `externals` row's
`source_type` rather than assuming `web`, falling back to `web` only when no row exists yet (today,
only reachable for a web target whose first-ever fetch never succeeds).

**A `dead` job's tombstone write must not beat an already-succeeded fetch (closed, `lode-uda1` +
`lode-elc8`).** The TRANSIENT row's "on `dead`, the caller writes a tombstone" is unconditional *only*
with respect to content that predates the dead-lettering job's own claim. `lode.worker._reclaim_stale_running`'s
crash-reclaim gate can dead-letter a `refresh` job that is not actually crashed, merely stalled past
`stale_running_timeout_s` — and if its handler's own fetch then succeeds and commits a real snapshot
before (or racing) the reclaim's dead-letter hook, the hook must not overwrite that real, current
content with a tombstone. The hook is guarded on the job's `claimed_at`: it skips the tombstone write
when the external's head is already a non-tombstone snapshot fetched at or after that claim (see
`docs/storage.md` "A dead-letter hook's write can race a late success too" for the full race and
rationale). This guard is orthogonal to — and neither depends on nor prejudges — the separate question
of whether a late `status='done'` job-row write should itself be guarded (`docs/storage.md` "Crash
reclaim: a job stuck in `running`"). The check and the tombstone write are now atomic (`lode-elc8`):
the head read happens *inside* `ingest_snapshot`'s own transaction, after it has already taken
SQLite's write lock, rather than as a separate caller-side read beforehand — closing the race outright
rather than merely narrowing its window. See `docs/storage.md`'s "A dead-letter hook's write can race
a late success too" for the mechanism and its empirical verification.

---

## Link-rot immunity (the payoff that justifies draw-down)

Because we **snapshot** externals instead of storing bare URLs, the knowledge graph is **immune
to link rot**: when the ticket is deleted, the wiki reorganised, the page taken down, the
mirrored snapshot — and everything the AI derived from it — survives. The opposite of bookmarks.
**Principle: always snapshot, never store a bare URL.**

---

## Privacy (consequence of aggregation)

Single-user does not mean low-stakes. Once this box holds email + internal tickets + repo contents,
it is a concentrated high-value target — and the precise privacy claim matters.

### What leaves the box, and what doesn't

> **Content never leaves the box *for indexing*. Enrichment and Q&A are explicit, governed egress.**

Not "content never leaves the machine" — that's false and the headline must not say it:

- **Local, never leaves:** chunking, embeddings, reranking, NLI entailment. **Indexing and
  retrieval are fully on-box.**
- **Leaves the box to Anthropic:** **enrichment** (Haiku, *every note*, background) and **Q&A**
  (Sonnet/Opus, the *retrieved passages* — which can include mirrored ticket/email/repo snapshots —
  *per question*). The aggregation that makes this box valuable is exactly what Q&A ships into the
  cloud prompt, often invisibly.

### Two redactions, aimed at the right legs

Redaction is not one control. Because embedding is **local**, redacting before it only affects local
*retrievability* — it does **nothing** about egress, since the secret still sits in `versions.body`
and is still sent to Claude at enrichment/Q&A time:

- **Redact-before-index** — a pasted `.env` / API key doesn't become locally *retrievable* (vector
  + FTS). Local-at-rest concern.
- **Redact-before-egress** — strip known secret patterns from the **enrichment payload and the Q&A
  context** before they're sent to Claude. This is the control that actually limits cloud exposure,
  and it's the one §6 originally omitted.
- **`purge`** (the [corrective half](#hard-delete-the-deliberate-immutability-break-corrective-half))
  remains the only thing that removes the durable copy from `versions.body`.

### No-egress tier (for genuinely sensitive notes/sources)

A note — or an external source (a specific repo / ticket project) — can be marked **`no_egress`**:

- still **captured, chunked, embedded, and locally retrievable** (keyword + vector);
- **never sent to Claude** — no enrichment, and **excluded from cloud Q&A context**;
- in an answer it is **cited as "present, withheld from cloud synthesis"** rather than silently
  dropped, so the user knows relevant material exists but was kept local. (A local-LLM fallback that
  could synthesize over withheld notes is a future option — see [decisions.md](decisions.md).)

This keeps work secrets *in* the KB and retrievable while guaranteeing they never reach the cloud.

The control surface for an external source is `lode no-egress <external_id>` (`--clear` to undo it),
which flips `externals.no_egress`; every send path (enrichment, Q&A) reads the flag generically off
the row, so setting it is the only step needed (lode-w0h.7).

### Egress log (auditability)

Every time content leaves the box it is **logged**: timestamp, purpose (`enrich` | `qa`), model,
the `version_id`/`passage_id`s sent, and which redactions were applied. This extends the provenance
already on annotations into a straight answer to *"what of mine has gone to the cloud, and when?"*
Cheap to keep, high-trust, and the natural audit surface if a sensitive note is ever suspected of
having leaked.

### Local-at-rest

Care for the on-disk store itself — the SQLite file and LanceDB sit on the machine holding all of
the above.

---

## Hard delete: the deliberate immutability break (corrective half)

Append-only + content-addressing means a pasted secret otherwise lives in `versions.body`
**forever**, and a normal delete only writes a tombstone — the bytes survive. Because this box
aggregates sensitive data, there must be an escape hatch that **violates immutability on purpose**:

- **`purge` operates at version or whole-note granularity** (v1 — substring/span redaction is
  deferred, see [decisions.md](decisions.md)). It overwrites the body of the targeted version(s)
  with a redaction marker (`[purged YYYY-MM-DD]`) and sets `purged_at`. Node identity,
  `parent_version_id`, `op`, and `created` are **kept**, so lineage and undo structure survive —
  only the sensitive bytes die.
- **It sweeps the note's whole chain**, including soft-deleted (tombstoned) notes — a secret pasted
  then edited-around persists in older versions.
- **It cascades to the cache:** drop every derived entry referencing the purged versions (LanceDB
  vectors, FTS rows, `source: ai` annotations), then re-derive cheaply/locally so nothing leaks
  through the index. `source: user` annotations stay (metadata, not content).
- **Hash consequence (accepted):** a purged body no longer hashes to its `version_id`; that id stays
  as the historical identifier, flagged `purged`, and is no longer recomputable. This is the cost of
  an explicit immutability break, taken knowingly.
