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

```bash
lode config
```

You'll see `jira_enabled = true` in the knob table. **The credential rows are *not* a
reliable "am I credentialed?" check today**, so don't read too much into them:

- `jira_token` / `confluence_token` are `secret=True` and excluded from the table
  entirely — you'll see no row for them at all, whether set or not.
- `jira_email` / `confluence_email` reflect only the `config.toml` value: an email you
  put in `config.toml` is shown verbatim, but one supplied via `LODE_JIRA_EMAIL` (the
  recommended path) won't appear here, because env-var credentials resolve separately and
  don't flow into this table.

So `lode config` confirming the **flag** is really the most it reliably tells you right
now. The trustworthy check is the end-to-end draw-down in step 4: if the link fetches a
real snapshot, credentials resolved; if it tombstones or looks like a login page, they
didn't — re-check the env vars are exported in the shell you run lode from.

> Being improved (`lode-dx4r`): `lode config` will show a presence indicator
> (`[REDACTED]`) for all four credential keys when a value resolves from *any* source —
> env var or `config.toml` — so you can confirm lode has them without echoing the values.
> That same change also stops `jira_email`/`confluence_email` from printing a real address
> out of `config.toml`. Until it lands, use the step-4 check above.

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
| `lode config` won't show my token | Working as designed — tokens are `secret=True` and never echoed. |
| Two URL forms of the same issue made two nodes | They shouldn't — a browser permalink and an API URL of the same issue parse to the same key and dedup onto one row. If they didn't, the link shape may not carry the issue key/page id; check the URL includes `/browse/{KEY}` (JIRA) or `/pages/{id}/` (Confluence). |

**Confluence-specific gate:** the Confluence *dispatch leg* needs `lode-mfts` landed on
`trunk` to route through `refresh_external`'s dispatcher; JIRA's dispatch has been wired
since `lode-gpzn.3`. If Confluence links aren't drawing down even when configured, verify
that ticket's state (`bd show lode-mfts`). See
[`externals.md`](../externals.md#atlassian-connectors-jira--confluence-cloud-lode-gpzn).
