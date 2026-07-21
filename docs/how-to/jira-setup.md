# How to set up the JIRA integration

> Full knob table (base-URL overrides, secrecy guarantees, resolution order) is in
> [`configuration.md`](../configuration.md#atlassian-connectors-jira--confluence-cloud-lode-gpzn).
> The design rationale is in [`externals.md`](../externals.md#atlassian-connectors-jira--confluence-cloud-lode-gpzn).
> This guide is the end-to-end setup recipe.

With the connector **on and credentialed**, a pasted JIRA link is drawn down through
JIRA's authenticated REST API into a structured snapshot. Without it, the same link falls
through to the generic web connector — which hits JIRA's login wall and tombstones. The
degrade is quiet: an unconfigured connector is never an error, just inactive.

**Cloud only.** JIRA Data Center / Server is explicitly out of scope.

## What you need first

- A **JIRA Cloud** account with an **API token** —
  create one at <https://id.atlassian.com/manage-profile/security/api-tokens>.
- The **account email** the token was created under (Basic auth is `email:token`).
- A real, readable **issue key** to test with, e.g. `PROJ-123`.

## 1. Provide credentials (env-var primary)

Credentials resolve **env var first, `config.toml` second**. The recommended path keeps
secrets out of the config file, in your environment:

```bash
export LODE_JIRA_TOKEN=<api-token>
export LODE_JIRA_EMAIL=<account-email>
```

<details>
<summary>Alternative: put them in <code>config.toml</code> (plaintext on disk)</summary>

No secret is *required* to live on disk, but it *may* — in plaintext; there is no keyring
integration (a deliberate deferral, see [`decisions.md`](../decisions.md)). Add to
`$LODE_HOME/config.toml`:

```toml
jira_email = "you@example.com"
jira_token = "your-atlassian-api-token"
```

An env var, if also set, wins over the `config.toml` value.
</details>

## 2. Turn the connector on

The flag is **off by default**. In `$LODE_HOME/config.toml` (flat table, no `[section]`
headers — see [config-change.md](config-change.md)):

```toml
jira_enabled = true
```

### Optional: base-URL override

Leave `jira_base_url` empty (the default) and lode **infers** the API base from the
`*.atlassian.net` host of the link you paste. Set it only if your Cloud site's API host
differs from the link itself:

```toml
jira_base_url = "https://acme.atlassian.net"
```

A non-empty value must be a well-formed `http(s)` URL, or `Settings()` construction fails
at load with a clear message.

## 3. Confirm the connector is active

A connector is **active** only when its flag is on **and** credentials resolve. Either
missing → inactive (the link quietly uses the web path instead).

`lode config` shows `jira_enabled = true` in the knob table, but that's just the flag —
it can't tell you whether credentials actually resolved or whether the tenant is
reachable. `lode verify` (read-only, writes nothing) does both in one shot:

```bash
lode verify --jira
```

It prints whether the flag is on, whether credentials resolved (and from which source —
env var or `config.toml`; the token is **never** shown), and whether the base URL is
configured or will be inferred — then, if active, makes one authenticated GET to confirm
the credentials actually reach the tenant, printing the authenticated account's display
name on success. Pass a real issue key to also dry-run fetching real content:

```bash
lode verify --jira PROJ-123
```

Exit code `0` means verified reachable; non-zero names exactly what's wrong (inactive
flag/credentials, unresolved base URL, bad credentials, bad base URL, or an unreachable
tenant) — fix that before moving on to step 4.

## 4. Try it end to end

A JIRA permalink has the shape `/browse/{KEY}`. Pasting one into a note enqueues an async
refresh job; `lode work` drains the queue and fetches the snapshot.

```bash
lode add "Debugging https://<site>.atlassian.net/browse/PROJ-123"
lode work                       # drain the async job queue → fetch the snapshot
lode dump-html <note-id>        # print the fetched snapshot's raw JSON payload
```

`lode jobs` shows the queue state if you want to watch the refresh land.

## Confluence

Confluence Cloud is the **same shape, a separate flag** — `confluence_enabled = true`
plus `LODE_CONFLUENCE_TOKEN` / `LODE_CONFLUENCE_EMAIL` (or the `confluence_token` /
`confluence_email` config fallbacks), with an optional `confluence_base_url`. A Confluence
link is the id-bearing page URL:
`https://<site>.atlassian.net/wiki/spaces/<SPACE>/pages/<id>/<slug>`.

## Recovering a link pasted before the connector was active

A link pasted **while the connector was inactive** (flag off, or credentials not resolving in
the shell that ran `lode add`) is stamped `source_type='web'` at paste time — and stays there.
It keeps refreshing through the generic web leg forever, which for JIRA/Confluence hits the
product's login wall and returns the SPA app shell, not ticket/page text (`lode dump-html`
prints JS, no real content). Fixing the config afterwards only changes routing for **new**
pastes — it does nothing for a link you already pasted.

To repair an already-pasted link, re-run the connector's migration for its already-processed
links under **current** (now-fixed) routing — the `lode backfill` command
([`lode.backfill`](../../src/lode/backfill.py) framework,
[`lode.jira_backfill`](../../src/lode/jira_backfill.py) for JIRA):

```bash
lode verify --jira                 # confirm the connector is actually active now
lode backfill jira --dry-run       # preview: what would migrate, nothing written
lode backfill jira                 # mint the JIRA identity, repoint the edge, enqueue a refresh
lode work                          # drain the queue -- fetches the real snapshot via the REST API
```

Confluence is the same shape: `lode backfill confluence --dry-run` / `lode backfill confluence`.

**When you need `--retry-tombstoned`.** A first backfill pass always mints a brand-new,
never-tombstoned identity, so `--retry-tombstoned` is never needed the first time. It only
matters on a **re-run**: if that fresh identity's own head snapshot then tombstoned (e.g. the
token was still wrong, or expired mid-run — a 401), a plain re-run treats that as a permanent
failure and skips it, same as the periodic refresh sweep would. Once you've actually fixed the
cause (rotated the token, fixed the base URL), `lode backfill jira --retry-tombstoned` is the
explicit opt-in to retry that specific target now instead of waiting on a schedule.

**Idempotent.** Re-running `lode backfill jira` after a link already migrated is a no-op for
that link — reclassification runs from the edge's *original* pasted URL every time, so an edge
already repointed onto its JIRA identity is simply revisited and its current head snapshot
re-checked, not re-migrated from scratch. Safe to run repeatedly, and safe to run even when
nothing needs it.

Full design: [`externals.md`](../externals.md#backfill-per-connector-re-draw-down-lode-gpzn9).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| JIRA link tombstones / looks like a login page | Connector **inactive** — flag off, or token/email not resolving from either source. Re-check `jira_enabled` and that the env vars are exported. |
| A JIRA/Confluence link I pasted earlier still shows the web/JS shell after I fixed my config | Edge frozen as `source_type='web'` at paste time — fixing config only changes routing for future pastes. Recover it with `lode backfill jira` (or `confluence`), then `lode work` — see [Recovering a link pasted before the connector was active](#recovering-a-link-pasted-before-the-connector-was-active) above. |
| `Settings()` fails on load with a base-URL error | `jira_base_url` / `confluence_base_url` is non-empty but malformed — must be a well-formed `http(s)` URL, or leave it empty to infer. |
| `lode config` won't show my token | Working as designed — tokens are `secret=True` and never echoed. |
| Two URL forms of the same issue made two nodes | They shouldn't — a browser permalink and an API URL of the same issue parse to the same key and dedup onto one row. If they didn't, the link shape may not carry the issue key/page id; check the URL includes `/browse/{KEY}` (JIRA) or `/pages/{id}/` (Confluence). |

**Confluence-specific gate:** the Confluence *dispatch leg* needs `lode-mfts` landed on
`trunk` to route through `refresh_external`'s dispatcher; JIRA's dispatch has been wired
since `lode-gpzn.3`. If Confluence links aren't drawing down even when configured, verify
that ticket's state (`bd show lode-mfts`). See
[`externals.md`](../externals.md#atlassian-connectors-jira--confluence-cloud-lode-gpzn).
