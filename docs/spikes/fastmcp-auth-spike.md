# Spike C0.4 — FastMCP as Purse's OAuth 2.1 Authorization Server

**Date:** 2026-09-02
**Question:** Can FastMCP carry Purse's six-mode auth compatibility requirement, and where do we have to write code ourselves?
**Verdict:** **GO** — with a named, bounded set of gaps.

---

## 0. Two premise corrections before anything else

The spike brief has two assumptions that the research invalidated. Both change the plan.

### 0.1 "current fastmcp 2.x release" — 2.x is two majors behind

FastMCP shipped **3.0.0** (2026-02-18) and **4.0.0 GA on 2026-08-31** — two days before this spike.

| Line | Last release | Date | Notes |
|---|---|---|---|
| 2.x | **2.14.7** | 2026-04-13 | End of line. No CIMD, no MultiAuth, no `application_type`. |
| 3.x | **3.4.7** | 2026-08-10 | CIMD (3.0.0), MultiAuth (3.1.0). Handshake-era protocol only. |
| 4.x | **4.0.0** | 2026-08-31 | MCP Python SDK v2, serves both the sessionless `2026-07-28` protocol **and** handshake-era clients. |

*Verified:* `https://api.github.com/repos/PrefectHQ/fastmcp/releases` (tag/date/prerelease flags read directly) and `https://pypi.org/pypi/fastmcp/json`.

**Do not build on 2.x.** Every capability Purse needs beyond plain PKCE landed in 3.0.0 or later.

Also note: the repo moved from `jlowin/fastmcp` to **`PrefectHQ/fastmcp`**, and 4.0 split packaging — `pip install fastmcp` now resolves to `fastmcp-slim[client,server]==4.0.0`. The auth code lives in `fastmcp_slim/fastmcp/server/auth/`.

### 0.2 "DCR ... incl. handling the 401 invalid_client re-registration signal" — DCR is deprecated, and that signal is not a contract

The MCP spec revision **`2026-07-28`** formally **deprecated RFC 7591 DCR in favour of CIMD** (PR #2858, SEP-2596 lifecycle policy). Exact normative text from `https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization`:

> "Authorization servers and MCP clients **SHOULD** support OAuth Client ID Metadata Documents."
> "Authorization servers and MCP clients **MAY** support the OAuth 2.0 Dynamic Client Registration Protocol (RFC7591). Note that Dynamic Client Registration is deprecated and retained for backwards compatibility with authorization servers that do not support Client ID Metadata Documents."

DCR's earliest removal is "first revision released on or after 2027-07-28", so we still ship it — but as the **fallback**, not the primary path. CIMD is the primary path.

Separately: **the "401 `invalid_client` → client re-registers" behaviour is not documented by Anthropic.** It appears only as *open bug reports asking for it* (`anthropics/claude-code` issues #9720, #79505, #59460). Anthropic's own docs state the opposite for hosted Claude: "DCR causes Claude to register a new client on every fresh connection." Treat re-registration-on-401 as best-effort hardening, not an acceptance criterion. CIMD makes the question moot — there is no registration record to invalidate.

**Consequence for Purse:** mode 3 (CIMD) is promoted from "nice, newer thing" to **the highest-value mode**, and it is precisely the mode with the biggest gap in the architecture Purse wants.

---

## 1. What FastMCP actually ships (verified against source, not docs)

Source of truth read directly at `PrefectHQ/fastmcp@main` (post-4.0.0), path `fastmcp_slim/fastmcp/server/auth/`:

```
auth.py                    AuthProvider, TokenVerifier, RemoteAuthProvider,
                           MultiAuth, OAuthProvider, TokenHandler,
                           PrivateKeyJWTClientAuthenticator
cimd.py                    CIMDDocument, CIMDFetcher, CIMDAssertionValidator,
                           CIMDClientManager        (marked "Beta Feature" in module docstring)
redirect_validation.py     is_loopback_host(), matches_allowed_pattern(), validate_redirect_uri()
jwt_issuer.py              JWT minting
identity_assertion.py      SEP-990 ID-JAG
handlers/authorize.py      FastMCP's own /authorize handler
oauth_proxy/               proxy.py, models.py (ProxyDCRClient), consent.py, upstream.py, ui.py
providers/                 jwt.py (JWTVerifier, StaticTokenVerifier), introspection.py,
                           debug.py, in_memory.py (InMemoryOAuthProvider), + 16 IdP providers
```

**The single most important structural fact:** FastMCP has *two* server-side OAuth paths, and they are **not** feature-equivalent.

| | `OAuthProxy` (proxy to an upstream IdP) | `OAuthProvider` (Purse is its own AS) |
|---|---|---|
| RFC 8414 AS metadata | Yes, overridden + enriched | Yes, `build_metadata()` from SDK |
| RFC 9728 PRM | Yes | Yes |
| PKCE S256 | Yes, dual-layer (client↔proxy, proxy↔upstream) | Yes (SDK `AuthorizeRequest.code_challenge_method: Literal["S256"]`) |
| DCR `/register` | Yes (synthesises clients) | Yes (SDK `RegistrationHandler`) |
| `401 invalid_client` on token | Yes (`TokenHandler` override) | Yes (same override) |
| **CIMD** | **Yes** — `enable_cimd=True` default | **No — not wired** |
| **`client_id_metadata_document_supported`** | **Yes** — `proxy.py:2537` | **No** |
| **`token_endpoint_auth_methods_supported`** | **`["none"]` (+ `"private_key_jwt"` w/ CIMD)** | **`["client_secret_post","client_secret_basic"]`** from SDK |
| **Port-agnostic loopback redirect** | **Yes** — `ProxyDCRClient._matches_registered_loopback_redirect_uri` | **No — SDK exact-match only** |
| RFC 9207 `iss` | Yes, always advertised | Not set |
| Consent screen, encrypted token storage | Yes | You implement |
| Requires an external upstream IdP | **Yes** | No |

### The three lines of code that decide everything

**CIMD flag is proxy-only** — `fastmcp_slim/fastmcp/server/auth/oauth_proxy/proxy.py:2530-2539`:

```python
auth_methods = ["none"]
if self._cimd_manager is not None:
    metadata.client_id_metadata_document_supported = True
    auth_methods.append("private_key_jwt")
metadata.token_endpoint_auth_methods_supported = auth_methods
```

`OAuthProvider.get_routes()` (`auth.py:894-994`) rebuilds the metadata route too — but only to fix the `issuer` field. It never touches `client_id_metadata_document_supported` and never swaps in `PrivateKeyJWTClientAuthenticator`; it constructs a plain `ClientAuthenticator(self)`.

**SDK metadata omits `none`** — `mcp/server/auth/routes.py:178`:

```python
token_endpoint_auth_methods_supported=["client_secret_post", "client_secret_basic"],
```

This matters enormously. Anthropic's connector docs state the selection rule verbatim:

> "Claude selects CIMD only when your authorization server metadata advertises **both** `"client_id_metadata_document_supported": true` **and** `"none"` in `token_endpoint_auth_methods_supported`... If either is missing, Claude falls back to DCR."

A vanilla `OAuthProvider` fails **both** conditions.

**SDK redirect validation is exact-match** — `mcp/shared/auth.py:187-198`:

```python
def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
    if redirect_uri is not None:
        if not self.redirect_uris or redirect_uri not in self.redirect_uris:
            raise InvalidRedirectUriError(f"Redirect URI '{redirect_uri}' not registered for client")
```

No RFC 8252 §7.3 loopback exemption. Claude Code declares `http://localhost/callback` and `http://127.0.0.1/callback` in its CIMD document at `https://claude.ai/oauth/claude-code-client-metadata` and then binds an arbitrary ephemeral port — exact-match rejects it.

**The good news:** FastMCP already solved all three, in reusable modules. `ProxyDCRClient` fixes loopback via a `validate_redirect_uri` override on an `OAuthClientInformationFull` subclass. `CIMDClientManager(enable_cimd=..., default_scope=..., allowed_redirect_uri_patterns=...)` exposes `is_cimd_client_id()`, `get_client()`, `validate_private_key_jwt()` — all public, all importable, all independent of `OAuthProxy`. `PrivateKeyJWTClientAuthenticator(provider, cimd_manager, token_endpoint_url)` is a drop-in for the SDK's `ClientAuthenticator`. And `is_loopback_host()` in `redirect_validation.py` correctly implements `127.0.0.0/8`, `::1`, and the RFC 6761 §6.3 `.localhost` namespace.

Filling the gaps means **wiring FastMCP's own parts together**, not writing OAuth from scratch.

---

## 2. Verdict table — six modes

Assessed for the recommended architecture: **subclass `OAuthProvider`** (Purse is its own AS; no external IdP dependency, which the self-host requirement demands).

| # | Mode | Status | Fill strategy | Est. |
|---|---|---|---|---|
| **1** | OAuth 2.1 + PKCE, RFC 8414 + RFC 9728 discovery, expiry + refresh | **Native** | `OAuthProvider.get_routes()` emits `/.well-known/oauth-authorization-server` (path-aware per RFC 8414 §3.1), `/.well-known/openid-configuration`, `/.well-known/oauth-protected-resource/*`, `/authorize`, `/token`, `/register`, `/revoke`. PKCE `S256` is mandatory in the SDK's `AuthorizeRequest` and advertised via `code_challenge_methods_supported`. Refresh via `load_refresh_token`/`exchange_refresh_token`. **Add:** set `authorization_response_iss_parameter_supported = True` and emit RFC 9207 `iss` — currently **SHOULD**, spec says explicitly it is expected to become **MUST**. | — |
| **2** | DCR (RFC 7591) + `401 invalid_client` | **Native** | SDK `RegistrationHandler` mounted automatically when `client_registration_options.enabled`. FastMCP's `TokenHandler` already rewrites SDK `unauthorized_client` → `invalid_client` at HTTP 401 and `invalid_grant` 400 → 401 (MCP-mandated). Implement `register_client()` to persist. **Caveat:** SEP-837 requires clients to send `application_type`; honour it (`"native"` → loopback allowed, `"web"` → HTTPS non-loopback only) — reuse `is_redirect_uri_allowed_for_application_type` from `redirect_validation`. **Known SDK bug:** issue #2460 — `register.py` grant_types validation; verify against SDK v2 (current `main` only requires `authorization_code`, so likely already fixed). | S |
| **3** | **CIMD** — advertise flag, `none` + `private_key_jwt` | **GAP** (proxy-only today) | Three-part wire-up, all from FastMCP's own modules: **(a)** override the metadata route in `get_routes()` (copy the pattern already at `auth.py:930-955`) to set `client_id_metadata_document_supported = True` and `token_endpoint_auth_methods_supported = ["none", "private_key_jwt", "client_secret_post", "client_secret_basic"]`; **(b)** in `get_client()`, branch on `CIMDClientManager.is_cimd_client_id(client_id)` → `await cimd_manager.get_client(url)`; **(c)** replace `ClientAuthenticator(self)` with `PrivateKeyJWTClientAuthenticator(self, cimd_manager, token_endpoint_url)` on the `/token` route. **Do not implement CIMD from scratch** — `cimd.py` already has SSRF blocking, HTTP-cache-aware fetching, JTI replay tracking, `client_id`-matches-URL validation, and shared-secret-method rejection. **Flag:** `cimd.py` docstring says "**Beta Feature**: CIMD support is currently in beta. The API may change." Pin exactly and add a contract test. | **M** |
| **4** | Static pre-registered `client_id` / `client_secret` (Cursor) | **Native (you own the store)** | `OAuthProvider` delegates client lookup to *your* `get_client()`. Seed statics from config/DB and return them. SDK `ClientAuthenticator` (`client_auth.py:66-116`) already handles `client_secret_basic`, `client_secret_post`, and `none`, with `hmac.compare_digest` on the secret and `client_secret_expires_at` enforcement. Only gap is advertisement, fixed by the same metadata override as mode 3. | S |
| **5** | Loopback redirects, **any port** (`localhost` / `127.0.0.1`) | **GAP** | Return a subclass of `OAuthClientInformationFull` from `get_client()` that overrides `validate_redirect_uri`. Port the ~25-line `_matches_registered_loopback_redirect_uri` from `oauth_proxy/models.py:163-190` (scheme + host + path + params + query + fragment must match; **port ignored**; userinfo rejected to block `http://localhost@evil.com`) and gate it on `is_loopback_host()` from `redirect_validation.py`. Optionally accept `allowed_client_redirect_uris`-style wildcards (`http://localhost:*/callback`) via `matches_allowed_pattern()`. **Must also cover `http://[::1]:*`.** | **S–M** |
| **6** | Personal access tokens as first-class bearer auth | **Native** | `MultiAuth(server=PurseOAuthProvider(...), verifiers=[PurseePATVerifier()])` — added in 3.1.0. `MultiAuth` runs `server.verify_token()` first, then each `TokenVerifier` in list order, first non-`None` `AccessToken` wins, 401 if all fail; routes and metadata come from `server` alone, so the MCP discovery surface stays clean. Write `PursePATVerifier(TokenVerifier)` with one `async def verify_token(self, token) -> AccessToken \| None` doing a hashed lookup. `DebugTokenVerifier` (2.13.1+) is a useful shape reference but is **dev-only**. | S |

**Totals: 4 native / 2 gaps.** Both gaps are wiring jobs against modules FastMCP already ships and tests. Neither requires writing an OAuth primitive.

### The alternative: `OAuthProxy` instead

If Purse is willing to depend on an upstream IdP (WorkOS AuthKit, Auth0, Keycloak self-hosted, GitHub, Google — 16 providers ship in-tree), **all six modes are native today**, including CIMD and loopback. Modes 4 and 6 come via `MultiAuth`. Zero gap work.

That is a genuinely attractive fallback and worth offering as a *deployment option* (`PURSE_AUTH_MODE=proxy`), especially for teams that already run Keycloak. But it should not be the default: an open-source self-hostable vault that cannot authenticate without a third-party IdP is a worse product, and `OAuthProxy`'s token-factory model (Fernet-encrypted upstream tokens, `client_storage` backend, mandatory `FernetEncryptionWrapper` in production) adds an operational surface Purse does not otherwise need.

---

## 3. Recommended fill strategy — and what *not* to do

**Do this:** subclass `OAuthProvider`, and use **`fastmcp.server.auth.providers.in_memory.InMemoryOAuthProvider` (369 lines) as the reference implementation.** It is a complete, working, in-tree implementation of all ten abstract methods (`get_client`, `register_client`, `authorize`, `load_authorization_code`, `exchange_authorization_code`, `load_refresh_token`, `exchange_refresh_token`, `load_access_token`, `revoke_token`, `verify_token`). Swap its dicts for Postgres, add the two gap fills, done.

**Do not** write custom Authlib AS routes mounted alongside FastMCP. Reasons:
1. You would be reimplementing `/authorize`, `/token`, `/register`, `/revoke`, RFC 8414 metadata, RFC 9728 PRM, PKCE verification, and the MCP-specific error-code mapping (`unauthorized_client`→`invalid_client`, `invalid_grant` 400→401) — all of which `OAuthProvider` + `TokenHandler` already give you correctly.
2. You would lose FastMCP's `set_mcp_path()` / path-aware well-known routing, which is fiddly and spec-load-bearing (RFC 8414 §3.3 requires `issuer` to byte-match the discovery URL).
3. Two AS implementations in one process is a security liability.

**Do not** implement CIMD from scratch. `cimd.py` is 825 lines of SSRF hardening and cache semantics. Reuse it.

**Authlib is already a dependency** — `fastmcp-slim[server]` requires `authlib>=1.6.11` and `fastmcp-slim[client]` requires it too. So if a narrow, specific need arises (e.g. JWKS handling, a JWT assertion edge case), reaching for Authlib costs no new dependency. Use it as a **library inside** your `OAuthProvider`, never as a parallel route tree.

### Concrete shape

```python
# purse/auth/provider.py
from fastmcp.server.auth.auth import OAuthProvider, PrivateKeyJWTClientAuthenticator
from fastmcp.server.auth.cimd import CIMDClientManager
from fastmcp.server.auth.redirect_validation import is_loopback_host
from mcp.shared.auth import OAuthClientInformationFull

class PurseClient(OAuthClientInformationFull):
    """Adds RFC 8252 §7.3 port-agnostic loopback matching (gap #5)."""
    def validate_redirect_uri(self, redirect_uri): ...

class PurseOAuthProvider(OAuthProvider):
    def __init__(self, ...):
        super().__init__(base_url=..., client_registration_options=..., revocation_options=...)
        self._cimd = CIMDClientManager(enable_cimd=True, allowed_redirect_uri_patterns=None)

    async def get_client(self, client_id):
        if self._cimd.is_cimd_client_id(client_id):        # gap #3b
            return await self._cimd.get_client(client_id)
        return await self._store.load(client_id)            # DCR + static, as PurseClient

    def get_routes(self, mcp_path=None):
        routes = super().get_routes(mcp_path)
        # gap #3a: patch metadata route -> client_id_metadata_document_supported,
        #          token_endpoint_auth_methods_supported incl. "none" + "private_key_jwt",
        #          authorization_response_iss_parameter_supported (RFC 9207)
        # gap #3c: patch /token route -> PrivateKeyJWTClientAuthenticator(self, self._cimd, token_url)
        return routes

# purse/server.py
mcp = FastMCP("purse", auth=MultiAuth(server=PurseOAuthProvider(...),
                                      verifiers=[PursePATVerifier()]))
mcp.run(transport="http")
```

### Non-negotiable acceptance tests for this spike's follow-up

1. `GET /.well-known/oauth-authorization-server` returns `client_id_metadata_document_supported: true` **and** `"none"` in `token_endpoint_auth_methods_supported` (Claude's CIMD gate — both, or it silently downgrades to DCR).
2. Authorize with `redirect_uri=http://127.0.0.1:54321/callback` against a client registered with `http://127.0.0.1/callback` → **accepted**. Same for `localhost` and `[::1]`. `http://localhost@evil.com/callback` → **rejected**.
3. `code_challenge_methods_supported: ["S256"]` present (clients **MUST refuse** to proceed without it).
4. Expired access token → HTTP **401** (not 400). Dead refresh token → `"error": "invalid_grant"`.
5. RFC 8707 `resource` accepted on both `/authorize` and `/token`, and audience-validated on every request.
6. `iss` present in the authorization response and byte-identical to the advertised `issuer`.
7. PAT and OAuth token both accepted on the same endpoint via `MultiAuth`.

---

## 4. Version pins

| Package | Pin | Rationale |
|---|---|---|
| **Python** | **3.12** (3.13 acceptable) | **Not 3.14.** FastMCP classifiers stop at 3.13; CI matrix is `3.10` (ubuntu+windows) + `3.13` (ubuntu) only; repo `.python-version` is `3.12`. mem0ai CI is `3.10/3.11/3.12` only and its `protobuf>=5.29.6,<7.0.0` pin excludes the 7.x line where 3.14 support landed (6.33.6 classifiers stop at 3.13; it will likely *install* on 3.14 via abi3 wheels but is untested upstream). Two untested-on-3.14 dependencies is not a risk worth taking on a compatibility-critical component. |
| **fastmcp** | `>=4.0.0,<5.0.0` | 4.0.0 GA 2026-08-31. Serves both the sessionless `2026-07-28` protocol and handshake-era clients with per-connection negotiation — every client on Purse's list is still on `2025-11-25`/`2025-06-18`, so this is the only version that is correct today *and* correct after they migrate. **Risk: two days old.** Fall back to `>=3.4.7,<4.0.0` if 4.0 proves unstable — but 3.x is handshake-era only and will need a migration later. |
| **mcp** | transitive, `>=2.0.0,<3.0.0` | Pulled by `fastmcp-slim[mcp]`. Purse imports from it directly (`mcp.shared.auth`, `mcp.server.auth.*`), so pin it explicitly in the lockfile and treat SDK-v2 changes as breaking. |
| **mcp-types** | transitive, `>=2.0.0,<3.0.0` | — |
| **authlib** | transitive, `>=1.6.11` | Already required by `fastmcp-slim[server]`. Available if needed; do not build parallel routes with it. |
| **mem0ai** | `==2.0.19` | Released 2026-08-24. Latest is the 2.x line; **0.1.x (last: 0.1.118, 2025-09-25) is legacy** and PyPI's lexical JSON ordering makes it look newer. Apache-2.0. **2.0.0 is a hard breaking release — ignore all 0.1.x docs and blog posts.** |
| **psycopg** | `[binary,pool]>=3.2.8` | mem0's pgvector store prefers psycopg3, falls back to psycopg2. Install this directly rather than mem0's `vector-stores` extra, which drags in ~25 unrelated DB clients. |

`pip install fastmcp` resolves to `fastmcp-slim[client,server]==4.0.0`; auth lives in the `fastmcp_slim` package.

---

## 5. Transport: Streamable HTTP

**Mature and correct to standardise on.** `transport="http"` in FastMCP *is* Streamable HTTP; `"streamable-http"` is an explicit alias for the identical thing. It has been the default HTTP transport since FastMCP 2.3.

Streamable HTTP is the **only** non-stdio transport in the current spec. The 2024-11-05 **HTTP+SSE transport** (two-endpoint `GET /sse` + `POST /messages`) is deprecated — since `2025-03-26`, formally reclassified Deprecated in `2026-07-28` under SEP-2596, earliest removal "three months after SEP-2596 reaches Final". FastMCP still accepts `transport="sse"`; **don't use it.**

Do not confuse that with SSE-as-a-response-body: `Content-Type: text/event-stream` on a POST response is core and mandatory inside Streamable HTTP (servers MUST support both it and `application/json`; clients MUST support both).

### The real transport risk: `2026-07-28` removes sessions

This is the item most likely to bite Purse's architecture, and it is not an auth issue:

- `Mcp-Session-Id` and protocol-level sessions: **removed**.
- `initialize` / `notifications/initialized` handshake: **removed** (replaced by `server/discover` + per-request `_meta`).
- Standalone `GET` SSE stream, `Last-Event-ID` resumability, `resources/subscribe`: **removed**.
- Server-initiated sampling / elicitation / roots: **removed** from the server API in FastMCP 4 (`ctx.sample()`, `ctx.sample_step()`, `ctx.list_roots()` gone across *all* protocol versions).
- New **REQUIRED** headers on every POST (SEP-2243): `MCP-Protocol-Version`, `Mcp-Method`, and `Mcp-Name` for `tools/call`/`resources/read`/`prompts/get`. Header/body mismatch MUST be rejected with 400 / JSON-RPC `-32020`.

**Design Purse as if sessions do not exist.** Run `stateless_http=True` (or `FASTMCP_STATELESS_HTTP=true`), never key vault state off `Mcp-Session-Id`, and use FastMCP 4's `UserSession` (state bound to the *authenticated user*, which Purse has by definition) or explicit server-minted handles passed as ordinary tool arguments. This aligns naturally with Purse's model — the vault is keyed by identity, not by connection.

---

## 6. mem0 embedded + pgvector

**Feasible, with one bug to verify before committing.**

- **In-process is first-class.** `Memory` and `AsyncMemory` in `mem0/memory/main.py`, plus `Memory.from_config(...)`. No server, no Docker.
- **pgvector is a registered provider** (`mem0/vector_stores/pgvector.py`). Config keys: `dbname`, `collection_name`, `embedding_model_dims`, `user`/`password`/`host`/`port` or `connection_string` or `connection_pool`, `sslmode`, `hnsw` (default `True`), `diskann`, `minconn`/`maxconn`. Requires `CREATE EXTENSION IF NOT EXISTS vector;`.
- **No graph store needed — it was removed from OSS in 2.0.0.** No Neo4j/Memgraph/Kuzu/AGE. Entity extraction now writes to a parallel collection in the same vector store. Purse needs **only Postgres**.
- **LLM key is not mandatory.** Defaults to OpenAI (`gpt-5-mini` + `text-embedding-3-small`, 1536 dims) but `LlmFactory`/`EmbedderFactory` support ollama, litellm, anthropic, bedrock, vLLM, LM Studio, etc. Fully-local zero-key operation is possible.
- **`qdrant-client` is a hard core dependency even when using pgvector** — you cannot escape the protobuf/grpcio chain by choosing pgvector. This is the Python-3.14 blocker.

**Open bugs that matter:**

| Issue | Impact | Action |
|---|---|---|
| **#6883** (open, 2026-08-10) — pgvector returns cosine **distance**, but `score_and_rank` treats it as **similarity**; ranking is inverted | **Severe.** Reporter measured 46/50 returned results not among the 50 nearest vectors. Also breaks `add()` dedup and interacts badly with the new `threshold=0.1` default. | **Verify in a one-hour spike before committing.** Insert ~50 known memories, search, confirm nearest ranks first. Fix is `score = 1 - distance` at the store boundary if reproduced. |
| **#4985** (open, P1) — switching embedder silently drops writes on dim mismatch | Silent data loss; API returns success + an ID, nothing persists. | **Always set `embedding_model_dims` explicitly** to match the embedder. Drop/recreate on embedder change. |
| **#7107** (open) — schema hardcoded to `public` | Blocks non-`public` schema deployments. | Accept `public`, or patch. |
| #1740, #3782 | Closed/fixed; both 0.1.x-era. | None. |

**Also note:** mem0's history store is **SQLite only** (`mem0/memory/storage.py`, stdlib `sqlite3`, default `~/.mem0/history.db`). There is no Postgres history backend — only `history_db_path` is configurable. Fine for a single embedded process; a real wart for multi-process/containerised Purse deployments. Flag as a follow-up.

---

## 7. GO / NO-GO

### **GO on FastMCP 4.x.**

The case:
- 4 of 6 modes are native; the 2 gaps are **wiring**, not implementation, and every part needed is already in the package (`CIMDClientManager`, `PrivateKeyJWTClientAuthenticator`, `is_loopback_host`, `matches_allowed_pattern`, `_matches_registered_loopback_redirect_uri` as a pattern to port).
- `InMemoryOAuthProvider` is a complete 369-line reference implementation of the exact class Purse must subclass.
- The MCP-specific error-code semantics that trip everyone up (`invalid_client` at 401, `invalid_grant` promoted 400→401) are **already** handled by FastMCP's `TokenHandler`.
- `MultiAuth` makes mode 6 (PAT) a ~30-line `TokenVerifier` with no impact on discovery metadata.
- 4.0 is the only version that serves both protocol eras from one deployment, which the seven-client requirement demands.
- Authlib is already in the dependency tree if a narrow need arises.

The risks, and how they're contained:

| Risk | Containment |
|---|---|
| FastMCP 4.0.0 is 2 days old | Pin exactly (`fastmcp==4.0.0`) in the lockfile, not a range, until a 4.0.x lands. Fall back to `3.4.7` if blocked — costs a later migration, not a rewrite. |
| CIMD module is marked **Beta**, "API may change" | Pin exactly; wrap `CIMDClientManager` behind a thin Purse adapter so an upstream signature change is a one-file fix; add contract tests on the metadata document and the `private_key_jwt` exchange. |
| Gap fills sit on internal-ish surfaces | All three are public modules with docstrings and their own test suites upstream (`tests/server/auth/test_redirect_validation.py`, `tests/server/auth/oauth_proxy/*`). Mirror those tests in Purse. |
| Upstream may wire CIMD into `OAuthProvider` and conflict with ours | Track issue #2863 (open, opened by jlowin 2026-01-13). Our fill is additive and easy to delete if upstream ships it. **File an upstream issue** — this gap is generic, not Purse-specific. |
| Python 3.14 | Don't. Pin 3.12. |

### Fallback plan, named

**If FastMCP 4.x proves unworkable: `Starlette` + the MCP Python SDK's `StreamableHTTPSessionManager` + an Authlib-backed authorization server mounted as sibling routes.**

That is the layer directly beneath FastMCP — same `mcp>=2.0.0` SDK, same `mcp.server.auth.routes.create_auth_routes` / `build_metadata` / `RegistrationHandler` primitives, same Starlette app. Migration cost is losing FastMCP's tool/resource decorators, `MultiAuth`, the `TokenHandler` error-code fixes, and path-aware well-known routing — all of which we would reimplement. **Estimate: 2–3 weeks.** It is a real fallback, not a fig leaf, because Purse's auth code would be subclassing SDK interfaces either way.

**Intermediate fallback (cheaper, prefer this first): `OAuthProxy` + a self-hosted Keycloak.** All six modes native, zero gap work, ships in-tree as `fastmcp.server.auth.providers.keycloak`. Cost is an external IdP in the self-host story. Worth building as an *optional* deployment mode (`PURSE_AUTH_MODE=proxy`) regardless, since it de-risks the whole spike: if the `OAuthProvider` gap fills stall, we still have a shipping path.

---

## 8. Sources

Verified by direct read unless marked.

**FastMCP — source (`PrefectHQ/fastmcp@main`, read 2026-09-02, post-4.0.0)**
- `fastmcp_slim/fastmcp/server/auth/auth.py` — `OAuthProvider`, `MultiAuth`, `TokenHandler`, `PrivateKeyJWTClientAuthenticator`
- `fastmcp_slim/fastmcp/server/auth/cimd.py` — `CIMDClientManager`, `CIMDFetcher`, `CIMDAssertionValidator` ("Beta Feature")
- `fastmcp_slim/fastmcp/server/auth/redirect_validation.py` — `is_loopback_host`, `matches_allowed_pattern`
- `fastmcp_slim/fastmcp/server/auth/oauth_proxy/proxy.py` — metadata override at L2530-2539
- `fastmcp_slim/fastmcp/server/auth/oauth_proxy/models.py` — `ProxyDCRClient`, `_matches_registered_loopback_redirect_uri` at L163-190
- `fastmcp_slim/fastmcp/server/auth/providers/in_memory.py` — `InMemoryOAuthProvider`
- `fastmcp_slim/fastmcp/server/mixins/transport.py` — transport literals
- `pyproject.toml`, `fastmcp_slim/pyproject.toml`, `.python-version`, `.github/workflows/run-tests.yml`

**MCP Python SDK (`modelcontextprotocol/python-sdk@main`)**
- `src/mcp/server/auth/routes.py` — `build_metadata` (L151-198)
- `src/mcp/shared/auth.py` — `validate_redirect_uri` (L187-198), `OAuthMetadata.client_id_metadata_document_supported` (L229)
- `src/mcp/server/auth/middleware/client_auth.py` — `ClientAuthenticator`
- `src/mcp/server/auth/handlers/authorize.py`, `handlers/register.py`

**FastMCP docs & releases**
- https://gofastmcp.com/servers/auth/authentication · /token-verification · /oauth-proxy · /full-oauth-server · /multi-auth
- https://gofastmcp.com/clients/auth/cimd
- https://gofastmcp.com/changelog · https://gofastmcp.com/updates
- https://gofastmcp.com/deployment/server-configuration · /running-server · /http
- https://api.github.com/repos/PrefectHQ/fastmcp/releases
- https://pypi.org/pypi/fastmcp/json
- Issues: [#2863 CIMD/SEP-991](https://github.com/PrefectHQ/fastmcp/issues/2863) (open) · [#2460 DCR grant_types](https://github.com/PrefectHQ/fastmcp/issues/2460) · [#3085 static client creds](https://github.com/PrefectHQ/fastmcp/issues/3085) (closed, client-side)

**MCP specification**
- https://modelcontextprotocol.io/specification/ (latest revision `2026-07-28`)
- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- .../authorization/client-registration · .../authorization-server-discovery · .../security-considerations
- https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- https://modelcontextprotocol.io/specification/2026-07-28/changelog · /deprecated
- https://modelcontextprotocol.io/seps/991-enable-url-based-client-registration-using-oauth-c (SEP-991, status **Final**)
- https://blog.modelcontextprotocol.io/posts/2026-07-28/
- https://datatracker.ietf.org/doc/draft-ietf-oauth-client-id-metadata-document/ (WG draft `-02`, 2026-07-06; **MCP normatively cites `-00`** — terminology drift to watch)

**Client behaviour**
- https://claude.com/docs/connectors/building/authentication — CIMD gate rule, PKCE, callback URLs, Claude Code CIMD doc at `https://claude.ai/oauth/claude-code-client-metadata`, `static_headers` (beta)
- https://developers.openai.com/plugins/build/auth — ChatGPT CIMD `none` + `private_key_jwt`
- https://code.visualstudio.com/api/extension-guides/ai/mcp — VS Code DCR, redirects `http://127.0.0.1:33418` + `https://vscode.dev/redirect`
- https://developers.openai.com/codex/cli/reference — Codex OAuth + bearer
- *Unverified / weakly sourced:* VS Code CIMD support (maintainer blog only); Cursor CIMD (community forum, appears DCR-only); Codex registration mechanism; "401 `invalid_client` → re-register" in Claude (open issues [#9720](https://github.com/anthropics/claude-code/issues/9720), [#79505](https://github.com/anthropics/claude-code/issues/79505), [#59460](https://github.com/anthropics/claude-code/issues/59460), **not documented behaviour**)

**mem0**
- https://pypi.org/pypi/mem0ai/json · /2.0.19/json · https://github.com/mem0ai/mem0/releases
- https://raw.githubusercontent.com/mem0ai/mem0/main/pyproject.toml
- `mem0/vector_stores/pgvector.py` · `mem0/configs/vector_stores/pgvector.py` · `mem0/memory/main.py` · `mem0/memory/storage.py` · `mem0/utils/factory.py`
- https://docs.mem0.ai/components/vectordbs/dbs/pgvector · https://docs.mem0.ai/migration/oss-v2-to-v3
- Issues: [#6883 inverted ranking](https://github.com/mem0ai/mem0/issues/6883) (open) · [#4985 silent dim-mismatch data loss](https://github.com/mem0ai/mem0/issues/4985) (open, P1) · [#7107 hardcoded schema](https://github.com/mem0ai/mem0/issues/7107) (open) · #1740, #3782 (closed)
