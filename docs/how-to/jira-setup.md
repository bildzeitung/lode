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

`lode config` shows `jira_enabled = true` in the knob table, and — since `lode-dx4r` —
also shows a **presence indicator** for all four credential keys (`jira_email`,
`jira_token`, `confluence_email`, `confluence_token`): `[REDACTED]` if a value resolves
from *either* the env var or `config.toml`, `[unset]` if neither does. That confirms
lode found *something*, but not whether it's the *right* something, or whether the
tenant is actually reachable. `lode verify` (read-only, writes nothing) checks both in
one shot:

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

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| JIRA link tombstones / looks like a login page | Connector **inactive** — flag off, or token/email not resolving from either source. Re-check `jira_enabled` and that the env vars are exported. |
| `Settings()` fails on load with a base-URL error | `jira_base_url` / `confluence_base_url` is non-empty but malformed — must be a well-formed `http(s)` URL, or leave it empty to infer. |
| `lode config` won't show my token or email value | Working as designed — all four credential fields are `secret=True` and never echoed; the row shows `[REDACTED]` (resolved) or `[unset]` (not resolved) instead. |
| Two URL forms of the same issue made two nodes | They shouldn't — a browser permalink and an API URL of the same issue parse to the same key and dedup onto one row. If they didn't, the link shape may not carry the issue key/page id; check the URL includes `/browse/{KEY}` (JIRA) or `/pages/{id}/` (Confluence). |

**Confluence-specific gate:** the Confluence *dispatch leg* needs `lode-mfts` landed on
`trunk` to route through `refresh_external`'s dispatcher; JIRA's dispatch has been wired
since `lode-gpzn.3`. If Confluence links aren't drawing down even when configured, verify
that ticket's state (`bd show lode-mfts`). See
[`externals.md`](../externals.md#atlassian-connectors-jira--confluence-cloud-lode-gpzn).
