# Connecting clients to Purse

Purse exposes one MCP endpoint. Every compatible client points at the same URL
and opens the same vault — that is the whole product.

Throughout, replace `https://vault.example.com` with your instance's public URL
(for the staging instance, `https://purse-staging.fly.dev`). The MCP endpoint is
that URL plus `/mcp`.

| Surface | URL |
|---|---|
| MCP endpoint | `https://vault.example.com/mcp` |
| OAuth discovery | `https://vault.example.com/.well-known/oauth-authorization-server` |
| REST (smoke/debug) | `https://vault.example.com/v1/memories` |

Two ways to authenticate:

- **OAuth** — the client walks you through a browser sign-in and a one-time
  **approve page** where you grant the connection its scopes. Used by the Claude
  surfaces, Cursor, VS Code, ChatGPT.
- **PAT** — a personal access token you mint once and paste into config. Best for
  headless/CLI clients and anywhere OAuth is fragile (Codex). Mint one with
  `python -m purse.auth.bootstrap` (self-host) or from the web UI (C7, later).

The first connection you approve is granted **write** scopes with a visible
"writes on" note; later connections default to read-only. Scopes:
`memory:read`, `memory:write`, `skills:read`, `skills:write`, `apis:use`,
`apis:manage`.

> **Verification status:** the connection recipes below are config-accurate.
> End-to-end "verified green" against the staging instance — with screenshots and
> a scripted connect→auth→call CI flow — is tracked as C8.1–C8.7 and lands once
> staging is deployed. Browser-flow menu paths (Claude web, ChatGPT) may shift as
> those apps change; treat them as a guide and see each vendor's current UI.

---

## Claude Code (C8.1)

Native CLI; connects over Streamable HTTP with DCR/CIMD + PKCE and a
loopback redirect on an ephemeral port (Purse accepts any localhost port).

```sh
claude mcp add --transport http purse https://vault.example.com/mcp
# then, in Claude Code:
/mcp
# choose "purse" and complete the browser OAuth + approve page.
```

Verify: ask Claude Code to "save that I prefer TypeScript", then in a fresh
session "what language do I prefer?".

## Cursor (C8.3)

Uses a static pre-registered client. Register a client id in Purse (a
`StaticClient`; admin UI arrives with C7), then:

```jsonc
// .cursor/mcp.json
{
  "mcpServers": {
    "purse": {
      "url": "https://vault.example.com/mcp",
      "auth": { "type": "oauth" }
    }
  }
}
```

Cursor is the client public server matrices flag as rejecting DCR-only servers —
the static-client path is the known-good one.

## Codex (C8.4) — PAT-first

Codex's OAuth path has open bugs (`oauth_resource` hand-editing, expired-token
reuse, pre-init hangs); a PAT sidesteps all of it. Store the token in an env var
and reference it:

```toml
# ~/.codex/config.toml
[mcp_servers.purse]
url = "https://vault.example.com/mcp"
bearer_token_env_var = "PURSE_TOKEN"
```

```sh
export PURSE_TOKEN=purse_pat_…      # the token from bootstrap / the web UI
```

CLI and IDE share this config.

## Claude web / Desktop (C8.2)

Settings → **Connectors** → add a custom connector with the URL
`https://vault.example.com/mcp`. Claude runs DCR (CIMD also works); complete the
OAuth sign-in and the approve page.

Notes to document once verified against staging:
- Mobile can only use connectors added on web — it can't add new ones.
- claude.ai sometimes caches a negative connector verdict with no user-facing
  reset; the fix is to remove and re-add the connector. (Troubleshooting section
  to be expanded with screenshots at verification time.)

## ChatGPT (C8.5) · VS Code (C8.6) · OpenClaw (C8.7)

Land in M3 alongside CIMD verification and the OpenClaw PAT guide. Stubs:

- **ChatGPT** — Settings → Connectors, Developer Mode (paid plans); CIMD; write
  actions are in beta on some plans, so `add_memory` may not work for all paid
  users (read path still works).
- **VS Code** — `.vscode/mcp.json`, CIMD.
- **OpenClaw / headless** — framework MCP client config, PAT (scoped read-only
  recommended for autonomous agents).
