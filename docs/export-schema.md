# Vault export schema

**Format:** `purse.vault.export`
**Version:** `1.0`
**Status:** public contract. Purse will not break it without bumping the version.

Exit is a feature (PRD §8.1). Export is one call, ungated — no plan check, no
rate limit, no support ticket. This page is the format it produces, written so
that someone who has left Purse can still read their file in five years.

Produced by `purse.db.export.export_vault(session, user_id)`; the returned
object contains only JSON-native values, so `json.dumps()` on it needs no
custom encoder.

---

## What is in it, and what is not

| Data | Exported | Notes |
|---|---|---|
| Memories | **All of them** | Including superseded and tombstoned rows, with provenance. The log *is* the history. |
| Skills | **All versions** | Not just the head of each name. |
| APIs | Names and references only | `name`, `provider`, `base_url`, `auth_style`, `allowed_hosts`. |
| API key material | **Never** | `key_ciphertext` and `dek_wrapped` are not in the file in any form or encoding. |
| Connections | Yes, minus secrets | So `provenance.connection_id` resolves to a client name. `token_hash` is never included. |
| Workspaces, user | Yes | Identity and structure. |
| Memory embeddings | No | Derived from `content`, model-specific, rebuildable, and large. See below. |
| Audit log | No | An operational record of the vault, not the user's content. |

Two of those rows are enforced by tests rather than by good intentions:

- `tests/db/test_export.py::test_no_key_material_survives_serialization`
  serializes a vault containing known ciphertext bytes and greps the output for
  them raw, hex-encoded, and base64-encoded.
- `purse/db/export.py` fails at import time if a new column is added to `apis`
  or `connections` without being explicitly classified as exportable or not.
  A future column is absent from the export by default.

### Why embeddings are excluded

An embedding is a cache of `content` under whatever model was configured when
the memory was written. It is not something the user said. It is rebuildable
(C3.6), it would multiply the file size by an order of magnitude, and it is
meaningless to anyone using a different embedding model. `content` is in the
export; re-embed from that.

---

## Document shape

```json
{
  "format": "purse.vault.export",
  "format_version": "1.0",
  "exported_at": "2026-09-02T11:04:33.921044+00:00",
  "user": {
    "id": "0f1c…",
    "email": "you@example.com",
    "created_at": "2026-08-30T09:12:00+00:00"
  },
  "workspaces": [
    {
      "id": "…",
      "name": "Personal",
      "created_at": "…",
      "connections": [ … ],
      "memories":    [ … ],
      "skills":      [ … ],
      "apis":        [ … ]
    }
  ]
}
```

### Top level

| Field | Type | Meaning |
|---|---|---|
| `format` | string | Always `"purse.vault.export"`. Check this before parsing. |
| `format_version` | string | See [Versioning](#versioning). |
| `exported_at` | string | ISO 8601, UTC, with offset. |
| `user` | object | `id`, `email`, `created_at`. Never auth material. |
| `workspaces` | array | Every workspace in the vault, oldest first. |

### `workspaces[]`

| Field | Type | Meaning |
|---|---|---|
| `id` | string (uuid) | |
| `name` | string | e.g. `Personal`, `Work`. Unique within the vault. |
| `created_at` | string | ISO 8601. |
| `connections` | array | See below. |
| `memories` | array | Ordered oldest first. |
| `skills` | array | Every version of every skill. |
| `apis` | array | Names and references only. |

### `memories[]`

The canonical log. Every row that was ever written, in write order.

| Field | Type | Meaning |
|---|---|---|
| `id` | string (uuid) | |
| `workspace_id` | string (uuid) | |
| `content` | string | Verbatim, as written. Purse never rewrites it. |
| `kind` | string | `fact` \| `preference` \| `decision`. |
| `supersedes` | string (uuid) \| null | The memory this one replaced. |
| `tombstone` | boolean | True if this memory was deleted. |
| `created_at` | string | ISO 8601. |
| `is_current` | boolean | Convenience: see [Reconstructing the current set](#reconstructing-the-current-set). |
| `provenance` | object | `connection_id`, `agent_id` (nullable), `initiated_by` (`user` \| `agent`). |

`initiated_by` is a **claim made by the calling client**. `connection_id` is
the trusted provenance — it is what Purse authenticated.

#### Reconstructing the current set

`is_current` is derived, and you can recompute it from the log alone:

> A memory is current when `tombstone` is `false` **and** no other memory in
> the file has `supersedes` equal to its `id`.

Supersession is permanent. If `A <- B <- C` and `C` is tombstoned, the chain is
dead: `B` and `A` do **not** become current again. A tool that re-derives
currency with "superseded by a row that is itself still current" will silently
resurrect old versions of things the user deleted.

### `skills[]`

Every stored version. Skills are content-addressed and immutable; a new version
is a new row.

| Field | Type | Meaning |
|---|---|---|
| `id` | string (uuid) | |
| `workspace_id` | string (uuid) | |
| `name` | string | Unique per workspace together with `version`. |
| `version` | string | Semver. Compare with a semver parser, not string order. |
| `frontmatter` | object | Parsed YAML frontmatter. |
| `content` | string | The markdown body. |
| `content_hash` | string | sha256 hex of `content`. |
| `created_at` | string | ISO 8601. |
| `is_head` | boolean | True for the version `get_skill(name)` resolves to. |

### `apis[]`

References, never credentials. After an export you still need to re-enter the
API key wherever you take your data — that is the intended behaviour, not a
gap.

| Field | Type | Meaning |
|---|---|---|
| `id` | string (uuid) | |
| `workspace_id` | string (uuid) | |
| `name` | string | Unique per workspace. |
| `provider` | string | e.g. `Stripe`. |
| `base_url` | string | The only origin `use_api` will call. |
| `auth_style` | string | e.g. `bearer`. Describes the shape, not the secret. |
| `allowed_hosts` | array of string | Outbound host allowlist. |
| `created_at`, `rotated_at` | string \| null | ISO 8601. |
| `key_exported` | boolean | Always `false`. Present so its absence is explicit rather than an oversight. |

### `connections[]`

Included so memory provenance resolves to something human-readable.

| Field | Type | Meaning |
|---|---|---|
| `id` | string (uuid) | Referenced by `memories[].provenance.connection_id`. |
| `workspace_id` | string (uuid) | |
| `client_name` | string | e.g. `claude-code`, `cursor`. |
| `auth_mode` | string | `oauth_dcr` \| `oauth_cimd` \| `oauth_static` \| `pat`. |
| `scopes` | array of string | |
| `writes_enabled` | boolean | |
| `created_at`, `revoked_at` | string \| null | ISO 8601. |

`token_hash` is never present.

---

## Versioning

`format_version` is `MAJOR.MINOR`.

- **Additive changes do not bump the major.** New keys may appear in any
  object. **Readers must ignore keys they do not recognise.**
- **A major bump means a reader written for the old version can be wrong** —
  a field removed, retyped, or given a different meaning.
- A minor bump signals a meaningful addition worth knowing about, but it stays
  backward compatible.

Purse will not silently change what an existing field means.

---

## Stability notes for re-importers

- **UUIDs are stable.** `id` values are the same ones Purse used internally, so
  `supersedes` and `provenance.connection_id` can be resolved within the file
  without any rewriting.
- **Timestamps are ISO 8601 with an offset**, always UTC.
- **Order is meaningful for memories** (oldest first), not for the other
  arrays.
- **`content` is verbatim.** No normalization, no trimming, no extraction. What
  the user's agent wrote is what comes out.
