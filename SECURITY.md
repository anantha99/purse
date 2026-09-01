# Security Policy

Purse holds memory, skills, and API credentials for people's agent tools. Security is the product, not a feature of it. This document states what we defend, how, and — plainly — what we do not defend.

## Supported versions

Purse is **pre-release**. There are no released versions and no maintained release branches yet.

| Version | Supported |
|---|---|
| `main` (pre-alpha) | ✅ Fixes land on `main` |
| Tagged releases | None yet |

Until a `0.1.0` release exists, security fixes are applied to `main` only. Once releases begin, this table will list the supported line and its support window.

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.**

Report privately through **GitHub Security Advisories**: go to the repository's **Security** tab → **Report a vulnerability**. This opens a private advisory visible only to you and the maintainers. If private reporting is unavailable to you, contact a maintainer directly through their GitHub profile and ask for a private channel — do not include vulnerability details in a public message.

Please include: what you found, how to reproduce it, the impact you believe it has, and any affected commit or configuration. Proof-of-concept code is welcome.

**What to expect:** acknowledgement within a few days, an assessment and planned fix timeline after triage, and credit in the advisory unless you prefer otherwise. We ask for coordinated disclosure — give us a reasonable window to ship a fix before publishing. We will not pursue legal action against good-faith research that avoids privacy violations, data destruction, and service degradation. There is no bug bounty.

## Threat model

### 1. Prompt injection in a connected client — the primary threat

Purse is reachable from clients that process untrusted content: web pages, documents, repositories, tool output. An attacker who controls that content can attempt to steer a connected agent into calling Purse's tools on their behalf — exfiltrating memory, writing false facts, or abusing API access.

**Mitigations**

- **Proxy-only keys.** No MCP tool returns raw secret material. `use_api` executes server-side with credentials injected by Purse, so an injected agent can at most *use* an API, never *learn* the key.
- **Host allowlists.** Every stored API carries an allowlist; `use_api` resolves the request path against the stored base URL only, strips client-supplied auth headers, and refuses off-allowlist redirects. Exfiltration to an attacker-controlled host is not reachable through the proxy.
- **Read-only default scopes.** Only the onboarding connection is provisioned with writes enabled, with a visible "writes on" badge and one-tap revoke. Every later connection defaults to read-only; `memory:write`, `skills:write`, and `apis:use` are opt-in per connection.
- **Provenance and audit.** Every row records the originating connection, optional agent ID, and whether the write was user- or agent-initiated. The audit log carries the last writes and executions. Bad writes are attributable and reviewable after the fact.
- **Caps and limits.** Payload size caps, response size caps, request timeouts, and per-connection rate limits (writes 60/min, `use_api` 30/min) bound the blast radius of an automated attack.

**Residual risk, stated plainly:** an injected writer with `memory:write` **can insert misleading facts into your vault**, and those facts will be served to your other agents until you notice and supersede them. Provenance tells you which connection did it; it does not stop it happening. An agent-write review queue is planned (the schema is ready for it) but is not in the MVP. Similarly, an injected agent holding `apis:use` **can make real API calls on your behalf** — that misuse is bounded by the host allowlist and rate limits, not eliminated. Grant `apis:use` only to connections you would trust with the underlying account.

### 2. Server compromise

An attacker with access to the database or the host.

**Mitigations**

- **Envelope encryption for secrets.** Each stored API key gets its own data encryption key, wrapped by a cloud KMS or a local master keyfile behind a single interface. Database access alone does not yield plaintext keys.
- **KMS / keyfile separation.** The wrapping key lives outside the database. Self-hosters use a local master keyfile with a documented backup procedure; hosted deployments use managed KMS.
- **Minimal logging.** Secret material is never written to logs, exports, error messages, or audit entries — the audit log records names and IDs only for secret-touching actions.

**Encryption honesty:** secrets get true envelope encryption. Memory and skills get encryption at rest plus TLS in transit — **not** end-to-end encryption, because server-side semantic search requires readable content. Purse will never market memory as end-to-end encrypted. A local-daemon path for genuinely E2E memory is post-MVP.

### 3. Stolen tokens

A leaked personal access token, OAuth access token, or refresh token.

**Mitigations**

- **Scopes.** Tokens carry the minimum scope set for their connection; a read-only token cannot write, and no token can retrieve key material.
- **Revocation.** Revoking a connection invalidates its tokens immediately and writes an audit entry. PATs are shown once and stored hashed at rest.
- **Rate limits and expiry.** Per-connection rate limits, OAuth token expiry with refresh, and the audit trail bound and expose abuse.

### 4. Malicious or compromised MCP client

Containment is the same mechanism as the injection case: per-connection scopes, writes-off by default, workspace isolation on every query, and audit as the detection surface.

## Core invariant

> **No MCP response ever contains key material.**

This is the one property Purse does not trade away. Raw key retrieval is not an unimplemented feature — it is absent by design, and any future exception would require out-of-band per-key approval in the web UI.

**How it will be enforced** (the secrets subsystem is not yet implemented — see the roadmap in [TASKS.md](TASKS.md), clusters C6/M5): secret values will be wrapped in redacting types whose `__repr__`/`__str__` do not reveal them, with a serializer that hard-fails rather than emit them, and a red-team CI job will drive scripted MCP calls against a seeded vault asserting that no key material appears in any response. That job is a launch gate: if it fails, nothing ships. Until that lands, no release exposes any secrets functionality at all.

## Scope of this policy

**In scope:** the Purse server, MCP gateway, OAuth authorization server, secrets proxy, web UI, and the official Docker/compose deployment artifacts.

**Out of scope:** vulnerabilities in third-party MCP clients (report those to their vendors), issues requiring a compromised operator account or physical access, missing hardening on a self-hosted instance the operator configured insecurely, and social engineering of maintainers.
