# Purse — Task Plan (from PRD v0.3)

**Purpose:** workstream clusters → GitHub epics/issues/labels once the repo exists.
**Scope:** full PRD MVP as written (§15, 5 weeks, seven-client gate).
**Decided — stack: Python + FastMCP.** Mem0 OSS embedded in-process behind `MemoryEngine` (its native language — no sidecar needed). Builder fluency wins for a solo 5-week build; the tradeoffs accepted: web UI is a second stack (or server-rendered via FastAPI + templates/HTMX), and secret-hygiene relies on runtime discipline (pydantic `SecretStr`, redacted `__repr__`) plus the red-team CI rather than compile-time types. PRD §17.1 resolved.

**Spike C0.4 verdict (2026-09-02, full report: `docs/spikes/fastmcp-auth-spike.md`): GO on FastMCP 4.x.**
- **Pins:** `fastmcp==4.0.0` (exact — released 2026-08-31), **Python 3.12 runtime** (FastMCP CI and mem0's dependency chain don't cover 3.14 yet), `mem0ai==2.0.19` (2.x is current; 0.1.x is legacy).
- **Auth coverage:** 4 of 6 modes native (PKCE+discovery, DCR, static clients, PAT via `MultiAuth`). Two wiring gaps, both fillable from in-tree FastMCP components: **CIMD** (wire `CIMDClientManager` + `PrivateKeyJWTClientAuthenticator`, patch AS metadata to advertise `client_id_metadata_document_supported: true` AND `"none"` in `token_endpoint_auth_methods_supported` — Anthropic gates CIMD on both; a vanilla provider silently downgrades to DCR) and **port-agnostic loopback** (~25 lines ported from FastMCP's `oauth_proxy/models.py`). Do NOT build parallel authlib routes; subclass `OAuthProvider` using in-tree `InMemoryOAuthProvider` as reference.
- **Spec shift:** MCP spec 2026-07-28 **deprecates DCR in favor of CIMD** — CIMD is now the primary OAuth path, DCR kept for client compatibility. The "401 invalid_client re-registration signal" is NOT documented Anthropic behavior (only open bug reports) — dropped as an acceptance criterion.
- **Architecture directive:** spec 2026-07-28 removes MCP sessions entirely (no `Mcp-Session-Id`, no resumability; `ctx.sample()`/`ctx.list_roots()` gone in FastMCP 4). **Design stateless from day one**; key all state off authenticated identity.
- **Fallbacks (in order):** `OAuthProxy` + self-hosted Keycloak as optional `PURSE_AUTH_MODE=proxy` (all six modes, zero gap work); hard fallback Starlette + MCP SDK `StreamableHTTPSessionManager` + authlib AS (~2–3 wks).

---

## Cluster map

| ID | Cluster | PRD | Milestone | Depends on |
|----|---------|-----|-----------|------------|
| C0 | Repo & infrastructure | §6, §15-W1 | M1 | — |
| C1 | Data layer | §11 | M1 | C0 |
| C2 | Auth & Connect | §8.5 | M1 (PAT) → M3 (all modes) | C1 |
| C3 | Memory | §8.2 | M1–M2 | C1 |
| C4 | MCP gateway & tools | §10, §12 | M2 | C1, C2 (PAT), C3 |
| C5 | Skills | §8.3 | M3 | C1, C4 |
| C6 | Secrets & proxy | §8.4, §13 | M4 (store) → M5 (executor) | C1, C4 |
| C7 | Web UI | §8.7 | M3 | C1–C3 APIs |
| C8 | Client compatibility | §9 | M2 (4 clients) → M3 (7) | C2, C4 |
| C9 | Self-hosting & distribution | §8.6 | M4 | C0, C2 (PAT), C6 (keyfile) |

**Milestones** (= PRD weekly checkpoints, used as GitHub milestones):

- **M1 — Spine** (W1): accounts, PAT, workspaces, canonical memory, REST add/search, Mem0 adapter. Repo public.
- **M2 — Cross-tool recall demo** (W2): MCP memory tools; PKCE + discovery + DCR + loopback; Claude Desktop, Claude Code, Cursor, Codex verified.
- **M3 — Seven clients green** (W3): skills, web UI, onboarding, CIMD, ChatGPT + VS Code + OpenClaw verified, CI connect-flow scripts.
- **M4 — Shippable checkpoint** (W4): secrets store (names + refs, no execution), compose self-host, keyfile encryption, self-host docs.
- **M5 — Launch** (W5): `use_api` proxy execution, red-team CI, hardening + docs pass. OSS repo + hosted beta together.

**Label scheme:** `area:<cluster>` (repo, data, auth, memory, gateway, skills, secrets, ui, clients, selfhost), `type:feat|bug|docs|ci|security`, `good-first-issue`, `launch-gate`.

---

## C0 — Repo & infrastructure (M1)

- [x] C0.1 Init public repo: Apache 2.0 LICENSE, NOTICE, README with positioning ("One purse. Every agent opens it."), architecture diagram from §12
- [x] C0.2 CONTRIBUTING.md + code of conduct + issue/PR templates
- [x] C0.3 SECURITY.md — threat model summary (§13), private disclosure channel
- [x] C0.4 Spike done — **GO on FastMCP 4.0.0** (see verdict above; report in `docs/spikes/fastmcp-auth-spike.md`)
- [x] C0.5 Project scaffold: `purse/` package (`gateway`, `auth`, `memory`, `skills`, `secrets`, `db`), `uv` + lockfile, ruff + mypy (strict on `auth`/`secrets`), pytest — all green on 3.12/3.14 locally
- [x] C0.6 docker-compose.dev.yml: Postgres + pgvector, app (Mem0 embedded); env-var-only config, fail-fast on missing `POSTGRES_PASSWORD`. *Caveat: `docker build` not yet exercised locally (daemon down) — first CI run is the real test*
- [x] C0.7 CI skeleton: lint, typecheck, unit tests, compose config validation + docker build on PR
- [ ] C0.8 "Purse" trademark check; fallback names shortlisted (§16)

## C1 — Data layer (M1)

- [x] C1.1 Migration tooling (Alembic, env-var-only URL) + baseline schema: `users`, `workspaces`
- [x] C1.2 `connections` (auth_mode, scopes[], writes_enabled, token_hash, revoked_at) + `oauth_clients` (dcr|cimd|static, jsonb metadata)
- [x] C1.3 `memories` append-only — enforced by DB **triggers** (whole-row jsonb diff; future columns immutable by default): only tombstone false→true and `embedding` (derived, rebuildable) may change; DELETE/TRUNCATE rejected; hard erase = documented operator-only compliance path
- [x] C1.4 "Current memory" as plain VIEW (deviation from "materialized" — correctness first, no refresh staleness; revisit when write volume justifies). Chains die at a tombstoned head — no resurrection of superseded rows
- [x] C1.5 `skills` + `skill_heads` (unique workspace+name+version, content_hash)
- [x] C1.6 `apis` (base_url, auth_style, allowed_hosts[], key_ciphertext, dek_wrapped, rotated_at)
- [x] C1.7 `audit_log` — names/IDs only for secret-touching actions
- [x] C1.8 Workspace isolation: workspace-bound `Repo` layer (no public method takes workspace_id — enforced by reflection test); cross-workspace supersession structurally impossible via composite FK; leak tests across two workspaces
- [x] C1.9 Vault export: full JSON (memories + provenance incl. superseded/tombstoned history, skills all versions, API *names* only), documented schema in `docs/export-schema.md`; import-time guard forces exported/never-exported classification of new columns — *(all verified in CI: 113 tests vs real pgvector Postgres, 0 skipped; PR #1)*

## C2 — Auth & Connect (M1: PAT → M3: all six modes)

Order chosen by which client each mode unblocks (§9), not by spec elegance.

- [x] C2.1 **PAT** — `purse_pat_` tokens, 256-bit, sha256-at-rest, redacting token type, shown once; revocable; no failure-mode oracle (revoked == unknown); constant-time compare. Bootstrap prints credentials once *(PR #2)*
- [x] C2.2 Scope model: six scopes, onboarding/default grant sets, `AuthContext` + `require_scope`; `writes_enabled=False` hard-cuts write scopes even when granted *(PR #2)*
- [x] C2.3 OAuth 2.1 core: authorization code + PKCE (S256, constant-time), opaque access+refresh tokens on the connections table *(PR #3)*
- [x] C2.4 Discovery metadata: RFC 8414 + RFC 9728, mounted at root *(PR #3)*
- [x] C2.5 **CIMD — primary path**: metadata advertises `client_id_metadata_document_supported: true` AND `"none"` in `token_endpoint_auth_methods_supported` (anti-downgrade gate asserted in tests + boot smoke test) *(PR #3)*
- [x] C2.6 **Loopback redirects, port-agnostic**: localhost/127.0.0.1/[::1] any port, rejects other hosts + userinfo smuggling *(PR #3)*
- [x] C2.7 **Static pre-registered clients** via `StaticClient` (Cursor). Admin-UI registration deferred to C7 *(PR #3)*
- [x] C2.8 **DCR** (RFC 7591) — compatibility fallback, persisted to `oauth_clients` *(PR #3)*
- [x] C2.9 Callback allowlist: claude.ai AND claude.com (distinct hosts); ChatGPT's callback arrives in its CIMD doc *(PR #3)*
- [ ] C2.10 Rate limits: writes 60/min, `use_api` 30/min per connection (§13) — **deferred to M2 hardening follow-up**
- [x] C2.11 Revocation: `revoke_connection` (idempotent) — authenticate fails immediately after; revoked indistinguishable from unknown *(PR #2)*

## C3 — Memory (M1–M2)

- [x] C3.1 Canonical write path: `add_memory` → verbatim insert with provenance (sync) → audit; ≤4096 UTF-8 *bytes*; engine ingest best-effort, never loses a write *(PR #2)*
- [x] C3.2 Supersession + tombstone: `update_memory` writes new row w/ `supersedes`; `delete_memory` tombstones (idempotent); keyset-paginated list over current view *(PR #2)*
- [x] C3.3 `MemoryEngine` interface (ingest/search/rebuild/drop) + `NullEngine`; ILIKE text-search fallback until Mem0 (C3.4). Failures can't fail a canonical write — AST-asserted no unguarded engine call *(PR #2)*
- [ ] C3.4 Mem0 OSS adapter (embedded, pin `mem0ai==2.0.19`): async ingest via background task queue; ingest failure = canonical write still succeeds. Config: always set `embedding_model_dims` explicitly (open P1 bug — silent data loss if unset); note history store is SQLite-only
- [ ] C3.4b **Mem0 ranking verification test (do FIRST, ~1 hr):** open issue #6883 — pgvector returns cosine *distance* but `score_and_rank` treats it as *similarity*, inverting rankings. Seed 50 memories, verify top-k is actually nearest. If broken: patch/workaround or lean on our own pgvector fallback (C3.5) for ranking
- [ ] C3.5 Semantic search: pgvector fallback + Mem0 recall, ranked results with provenance
- [ ] C3.6 Index rebuild command: drop + replay canonical log (proves "derived, droppable" §3.1–2)
- [ ] C3.7 **`purse-save-policy` skill** — the save-skill as a versioned artifact in-repo; wording iterated via its own issues (§8.2, product surface)
- [x] C3.8 REST `/v1/memories` CRUD + search, PRD-shaped structured errors, `X-Purse-Agent` per-call claim; `purse/gateway/app.py` wires real PAT auth *(PR #2)*

> **✅ Milestone M1 (Spine) complete** — PR #1 (data layer) + PR #2 (PAT auth, memory core, REST, end-to-end wiring). A minted PAT can `curl` a fact into the vault and search it back over authenticated HTTP; every write is provenanced and audited; revoked==unknown; 393 tests green vs real Postgres in CI. Remaining C3 items (Mem0 adapter C3.4–3.6, save-policy skill C3.7) are M2/M3 work.

## C4 — MCP gateway & tools (M2 core)

- [x] C4.1 MCP server, Streamable HTTP (`transport="http"`, `stateless_http=True`); workspace resolved only from the verified token, never a tool arg *(PR #3)*
- [x] C4.2 Structured errors: `UNAUTHORIZED_SCOPE`, `NOT_FOUND`, `PAYLOAD_TOO_LARGE`, etc. via `ToolError` envelope surviving masking *(PR #3)*
- [x] C4.3 Memory tools ×5: `search_memory`, `add_memory`, `list_memories`, `update_memory`, `delete_memory` — wrap `purse.memory.service` *(PR #3)*
- [ ] C4.4 Skills tools ×3: `list_skills`, `get_skill` (name, version?), `upsert_skill` (M3, with C5)
- [ ] C4.5 API tools ×3: `list_apis`, `get_api_ref` (M4), `use_api` (M5, with C6)
- [x] C4.6 Contract tests: tools × error codes × scope denial; no tool exposes `workspace_id`/`connection_id` *(PR #3)*
- [x] C4.7 `initiated_by` recorded as claim; `connection_id` from the token is the trusted provenance; MCP `agent_id` is always `None` (no trustworthy per-call identity) *(PR #3)*

> **▶ Milestone M2 (Cross-tool recall) in progress** — PR #3 landed the MCP gateway (C4) + OAuth 2.1 AS (C2.3–2.9) as one deployable app: `/mcp` (Streamable HTTP, all six auth modes), OAuth discovery at root with the CIMD anti-downgrade gate, `/v1` REST, verified vs real Postgres (451 tests) and a boot smoke test. **Remaining for M2:** staging deploy (Fly config ready, `docs/deploy-fly.md`), client verification C8.1–8.4, and the deferred C2.10 rate limits.

## C5 — Skills (M3)

- [ ] C5.1 Markdown + frontmatter parse/validate (name, description, semver version, updated_at)
- [ ] C5.2 Content-addressed versions; name → latest via `skill_heads`; history fetchable; ≤64 KB inline
- [ ] C5.3 `upsert_skill` version-bump semantics + MCP tools wired (C4.4)
- [ ] C5.4 Seed skills: `purse-save-policy` (from C3.7) preloaded in new vaults

## C6 — Secrets & proxy (M4 store → M5 executor)

- [ ] C6.1 Envelope encryption: per-key DEK, wrapped by KMS **or local keyfile** behind one interface (§8.6)
- [ ] C6.2 Secrets store CRUD: name, provider, base_url, auth_style, host allowlist, per-connection grants; key shown once; rotation
- [ ] C6.3 `list_apis` + `get_api_ref` — usage info, endpoint hints, **zero key material** (M4)
- [ ] C6.4 `use_api` executor (M5): scope → allowlist → DEK decrypt in-process → outbound HTTPS → scrub → return. Strips client auth headers; path resolved against stored base_url only; 30 s timeout; 256 KB response cap; no off-allowlist redirects
- [ ] C6.5 Secret-type hygiene: wrap all key material in pydantic `SecretStr` (or equivalent) with redacted `__repr__`/`__str__`; custom JSON encoder that hard-fails on serializing it; never in logs/exports/errors/audit
- [ ] C6.6 **Red-team CI**: scripted MCP calls asserting no key material in any response (§13, §14.5) — `launch-gate`
- [ ] C6.7 MVP auth styles: Bearer/API-key header only; upstream-OAuth APIs deferred (§17.2 — recommend yes)

## C7 — Web UI (M3)

- [ ] C7.1 Onboarding: vault auto-create + Personal workspace; **copy-MCP-URL front and center**; per-client setup snippets; guided "save that I prefer TypeScript" moment (§7.1)
- [ ] C7.2 Memories: current + history, search, edit (supersede) / tombstone, provenance filter
- [ ] C7.3 Skills: list/edit with version bump
- [ ] C7.4 APIs: add/rotate/revoke, allowlist editor, per-connection grants, key shown once
- [ ] C7.5 Connections: scopes, "writes on" badge, one-tap revoke
- [ ] C7.6 Static client + PAT management screens
- [ ] C7.7 Audit view: last 100 writes/executions
- [ ] C7.8 Export button → C1.9

## C8 — Client compatibility matrix (M2: 4 → M3: 7) — `launch-gate`

One issue per client: verified connect-auth-call flow + doc page with screenshots + caveats + CI script.

- [ ] C8.1 **Claude Code** — DCR + PKCE, loopback ephemeral ports (M2)
- [ ] C8.2 **Claude web/Desktop** — DCR/CIMD via Connectors; document mobile limitation + "remove & re-add" for cached negative verdicts (M2)
- [ ] C8.3 **Cursor** — static pre-registered client via `.cursor/mcp.json` (M2)
- [ ] C8.4 **Codex** — **PAT-first** (`bearer_token_env_var`); document OAuth bugs and why PAT sidesteps them (M2)
- [ ] C8.5 **ChatGPT** — CIMD, Developer Mode; document write-beta gating (`add_memory` may fail on some paid plans; read path works) (M3)
- [ ] C8.6 **VS Code** — CIMD via `.vscode/mcp.json` (M3)
- [ ] C8.7 **OpenClaw / headless** — PAT, scoped read-only token guide (M3)
- [ ] C8.8 CI: scripted connect-auth-call flow per client against staging vault, runs every release (§14.6)

## C9 — Self-hosting & distribution (M4)

- [ ] C9.1 Production compose: one command, Postgres + app, boots in seconds, **credentials printed on first boot** (§6 packaging bar)
- [ ] C9.2 Local master keyfile encryption + documented backup procedure; optional cloud-KMS config
- [ ] C9.3 Self-host auth docs in order: PAT → mcp-remote bridge → Tailscale/tunnel recipe → reverse-proxy + domain (§8.6)
- [ ] C9.4 Same build artifact hosted ≡ self-host; one-baseURL-change API parity
- [ ] C9.5 **Parity test**: fresh compose → Claude Code + Cursor connected in <15 min, scripted where possible (§14.7) — `launch-gate`
- [ ] C9.6 (Deferred per §17.4) single binary + SQLite — post-MVP issue, not in M4

---

## Launch gates (all must be green — §14, §19)

1. TTFW < 10 min (signup → `add_memory` from third-party tool)
2. Cross-tool recall same day
3. Seven-client matrix green in CI
4. Red-team CI: zero key material in any MCP response
5. Self-host parity < 15 min
6. Vault export works, documented schema

## Explicitly deferred (post-MVP backlog, from §15/§17)

Agent-write review queue · local daemon (SQLite mirror + op-log) · vendor memory import (growth mechanic — moves up if activation lags, §16) · `profile` kind · skill diffs · team workspaces · single binary · upstream-OAuth APIs in `use_api` · doctor diagnostic command.

## Sequencing risks to watch

- **C2 + C8 ≈ 40% of total effort** and interleave — verify each client the moment its auth mode lands; don't batch all six modes then test.
- **ChatGPT is the flakiest lane** (CIMD pickiness, write-beta gating, distinct callback) — start C8.5 early in W3, not at the end.
- **C6.4 (`use_api`) is deliberately last** — highest-risk surface, everything else is shippable without it (M4 checkpoint exists for exactly this reason).
- Rough issue count: **~65 issues** across 10 epics; a day or less each for one builder.
