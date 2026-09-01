# Purse

**One purse. Every agent opens it.**

An open-source, portable vault for agent **memory**, **skills**, and **API access** — exposed through a single MCP URL that any compatible agent tool can connect to.

> **Status: pre-alpha, building in public.** Nothing is installable yet. The roadmap is [TASKS.md](TASKS.md).

---

## The problem

If you build with AI, you live in two or more agent tools: Claude (chat + Code), Cursor, ChatGPT, Codex, VS Code, orchestrators like OpenClaw. Each one keeps its own copy of:

- **Memory** — the facts, preferences, and decisions it learned about you
- **Skills** — the playbooks and instructions that shape how it works
- **Keys** — API credentials pasted separately into every tool's config

Those copies drift. Export/import is a one-time paste that goes stale the moment you make it. The same preference gets re-taught to every tool. Secrets end up scattered across N config files with no audit trail and no revocation story.

**The password-manager analogy is the whole product.** Chrome does not "sync passwords with Safari" — both ask the same vault. Purse does that for memory, skills, and API access. Tools never hold an authoritative copy and never sync with each other; they reach into Purse when they need something.

Purse is only the bag. OpenClaw orchestrates. Cursor writes code. Claude chats. Purse stores and serves.

## What Purse holds

**Memory — with provenance.** Every write is a verbatim, append-only canonical record carrying who wrote it: which connection, which agent, user- or agent-initiated, and when. Updates supersede rather than mutate; deletes tombstone. "Current memory" is a view over the log, so you get history, last-write-wins, and audit for free. A semantic index (Mem0 OSS, behind a `MemoryEngine` interface) sits on top as a *derived*, droppable, rebuildable layer — never the source of truth.

**Skills — versioned markdown.** Playbooks as markdown + frontmatter (`name`, `description`, semver `version`, `updated_at`), content-addressed, name resolves to latest, full history fetchable. Write a skill once in the web UI; every connected agent can fetch it.

**API access — keys that never enter model context.** Store a key once with a base URL, auth style, and host allowlist. Agents get `list_apis` and `get_api_ref` (names, usage hints, **zero key material**) and `use_api`, which executes the request **server-side** with credentials injected by Purse. The key never appears in a tool result, a context window, a log, or an export. Raw key retrieval is not an API — it does not exist.

**Identity.** One user, one vault, workspaces (Personal / Work), and per-connection scopes on every call.

## Product principles

1. **Bag, not brain.** Purse stores and retrieves. It does not decide, summarize your conversations, or orchestrate agents. Any intelligence is a derived index, never truth.
2. **The user's record is canonical.** What you or your agent wrote is stored verbatim with provenance. Anything an ML layer derives can be rebuilt or thrown away.
3. **Keys never enter model context.** No MCP tool ever returns raw secret material. If an agent needs an API, Purse makes the call.
4. **Every write is attributable.** Origin is recorded on every row, from day one.
5. **Exit is a feature.** Full vault export as JSON in a documented open format, always available, never gated.
6. **Open by default.** The self-hosted build is the *full* single-user product — no feature caps, no document limits, no "MCP is cloud-only." A vault you can't fully own is the disease Purse treats; it cannot also have it.

## Architecture

```
Claude web/Desktop · Claude Code · Cursor · ChatGPT · VS Code · Codex · OpenClaw
        │  (Streamable HTTP MCP · OAuth2.1 DCR/CIMD/static · PAT)
        ▼
┌─────────────────────────────────────────────┐
│  MCP Gateway + OAuth 2.1 AS (FastMCP)       │
│  6 auth modes · scopes · rate limits · audit│
├──────────────┬──────────────┬───────────────┤
│ Memory       │ Skills       │ Secrets/Proxy │
│  ├ canonical │  Postgres    │  envelope enc │
│  │ store (PG)│              │  KMS/keyfile  │
│  └ Mem0 OSS  │              │  outbound     │
│    (derived) │              │  executor     │
└──────────────┴──────────────┴───────────────┘
   Postgres (+pgvector) · KMS or local keyfile · audit
   Deploy: hosted ≡ self-host (compose) — same build artifact
```

- Modular monolith; the gateway is the only public surface.
- **Write path:** auth → scope check → canonical insert (sync) → index ingest (async) → audit.
- **`use_api` path:** scope → host allowlist → DEK decrypt in-process → outbound HTTPS → scrub → return.
- **Encryption honesty:** secrets get true envelope encryption; memory and skills get at-rest encryption + TLS (server-side semantic search requires readable content). Purse will never claim end-to-end encrypted memory it does not have.

**Stack:** Python + FastMCP · Postgres + pgvector · Mem0 OSS embedded behind a `MemoryEngine` interface · one build artifact for hosted and self-host.

## Planned MCP tools

All tools are workspace-scoped by the authenticated connection. Errors are structured `{error: {code, message}}` with codes `UNAUTHORIZED_SCOPE`, `NOT_FOUND`, `RATE_LIMITED`, `HOST_NOT_ALLOWED`, `PAYLOAD_TOO_LARGE`.

| Tool | Params | Returns | Scope |
|---|---|---|---|
| `search_memory` | `query`, `limit` (default 8) | ranked facts: `id`, `content`, `created_at`, `provenance` | `memory:read` |
| `add_memory` | `content` (≤ 4 KB), `kind` (`fact\|preference\|decision`), `initiated_by` (`user\|agent`) | `id`, `created_at` | `memory:write` |
| `list_memories` | `cursor`, `limit` | page of current-view facts | `memory:read` |
| `update_memory` | `id`, `content` | new `id` (supersedes the old one) | `memory:write` |
| `delete_memory` | `id` | ok (tombstone) | `memory:write` |
| `list_skills` | — | `name`, `description`, `version` | `skills:read` |
| `get_skill` | `name`, `version?` | frontmatter + body | `skills:read` |
| `upsert_skill` | `name`, `content` | new `version` | `skills:write` |
| `list_apis` | — | `name`, `description` | `apis:use` |
| `get_api_ref` | `name` | base URL, auth style, endpoint hints — **no key material** | `apis:use` |
| `use_api` | `name`, `request` `{method, path, query?, body?, headers?}` | `status`, `body` (≤ 256 KB), `duration_ms` | `apis:use` |

Notes: `initiated_by` is a self-reported provenance *claim* — the connection ID is the trustworthy part. `use_api` strips client-supplied auth headers, resolves `path` against the stored base URL only, enforces the host allowlist, times out at 30 s, and does not follow off-allowlist redirects.

## Planned client support

The launch gate is seven clients verified green, each with its own tested doc page and a scripted connect → auth → call flow in CI.

| Client | How it connects | Recommended auth |
|---|---|---|
| Claude web / Desktop | Settings → Connectors, Streamable HTTP | DCR (CIMD also works) |
| Claude Code | `claude mcp add --transport http <url>` then `/mcp` | DCR + PKCE, loopback on ephemeral ports |
| Cursor | `.cursor/mcp.json` | Static pre-registered client |
| ChatGPT | Settings → Connectors, Developer Mode | CIMD |
| VS Code | `.vscode/mcp.json` | CIMD |
| Codex (CLI / IDE / Desktop) | `codex mcp add` or `~/.codex/config.toml` | **PAT-first** |
| OpenClaw / headless | Framework MCP client config | PAT (scoped read-only recommended) |

Per-client caveats — ChatGPT's write beta, claude.ai's cached connector verdicts, Codex's OAuth bugs — get documented on each client's page rather than glossed over.

## Quickstart

**Coming soon — `docker compose up`.**

Self-host is the full single-user product: same gateway, memory, skills, secrets proxy, and UI as hosted, with no caps. One command, credentials printed on first boot, config by environment variables only, and secrets never written into compose files.

Until then, the development compose file lives at [`docker-compose.dev.yml`](docker-compose.dev.yml) and the local setup steps are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Status and roadmap

Pre-alpha. Building in public, week by week, from an internal PRD.

The full plan lives in **[TASKS.md](TASKS.md)** — ten workstream clusters (C0–C9) mapped to five weekly milestones:

- **M1 — Spine:** accounts, PAT, workspaces, canonical memory, Mem0 adapter
- **M2 — Cross-tool recall:** MCP memory tools, OAuth AS (PKCE + discovery + DCR + loopback), four clients verified
- **M3 — Seven clients green:** skills, web UI, onboarding, CIMD
- **M4 — Shippable checkpoint:** secrets store, compose self-host, keyfile encryption
- **M5 — Launch:** `use_api` proxy execution, red-team CI, hardening

Launch gates: first write under 10 minutes, cross-tool recall the same day, seven-client matrix green in CI, **zero key material in any MCP response (CI-enforced)**, self-host parity under 15 minutes, and a working documented vault export.

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and conventions, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Entry points will be labelled `good-first-issue` as clusters open up.

## Security

Please report vulnerabilities privately — see [SECURITY.md](SECURITY.md), which also carries the threat model and the residual risks we accept and document rather than hide.

## License

[Apache License 2.0](LICENSE). Explicit patent grant, no adoption friction. See [NOTICE](NOTICE).
