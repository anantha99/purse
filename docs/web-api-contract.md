# Web API contract (C7)

The seam between the Python backend (`purse/web/`) and the Next.js frontend
(`web/`). Both build to this. Distinct from the agent surfaces: `/mcp` (MCP
tools) and `/v1` (agent REST, PAT/OAuth) are for *agents*; `/web/*` is for the
*human operator* dashboard and is session-authenticated.

## Auth model — BFF, no cross-site cookies

- Single operator per instance (MVP). Password is `PURSE_OWNER_PASSWORD` (env),
  compared constant-time at login. No per-user password stored in the DB (no
  migration needed). If `PURSE_OWNER_PASSWORD` is unset, login is disabled and
  `/web/login` returns a clear "operator password not configured" error.
- On successful login the backend issues an **opaque signed session token**
  (itsdangerous, `PURSE_SESSION_SECRET` env, ~12h expiry) encoding the operator
  user id + Personal workspace id. No DB sessions table.
- The Next.js app is a **BFF**: browser talks only to the Next.js origin. Next.js
  `/api/*` route handlers hold the session token in an **httpOnly, SameSite=Lax,
  Secure** cookie on the frontend origin, and forward it to the backend as
  `Authorization: Bearer <session-token>` on every `/web/*` call. So there are no
  cross-site cookies and the backend needs no CORS for the browser.
- Every `/web/*` endpoint (except `/web/login`) requires a valid session token →
  resolves to the operator's workspace; all queries are workspace-scoped via the
  existing `Repo`. Invalid/expired token → 401 `{"error":{"code":"UNAUTHENTICATED"}}`.

## Errors

Same shape as the rest of Purse: `{"error":{"code","message"}}`. Codes:
`UNAUTHENTICATED` (401), `INVALID_CREDENTIALS` (401), `NOT_FOUND` (404),
`VALIDATION` (422), `PAYLOAD_TOO_LARGE` (413), `RATE_LIMITED` (429).

## Endpoints (all under `/web`)

### Session
- `POST /web/login` — body `{password}` → `200 {user:{email}, workspace:{id,name}}` + sets nothing itself (the BFF sets the cookie from the returned token). Response body also includes `{session_token}` for the BFF to store. Wrong password → 401 `INVALID_CREDENTIALS`.
- `POST /web/logout` — 204. (BFF clears its cookie; token is stateless so this is a client-side clear.)
- `GET /web/session` — → `200 {user:{email}, workspace:{id,name}, writes_enabled_default:false}` or 401.

### Memories (reuse `purse.memory.service` + `Repo`)
- `GET /web/memories?cursor=&limit=&kind=&initiated_by=` → page of the **current view**: `{items:[{id,content,kind,created_at,provenance:{connection_id,client_name,agent_id,initiated_by},superseded_count}], next_cursor}`. `client_name` is resolved from the connection for display.
- `GET /web/memories/search?q=&limit=` → `{results:[…same shape…, score]}` (semantic when the engine is on, else keyword).
- `GET /web/memories/{id}/history` → `{versions:[{id,content,created_at,provenance,tombstoned}]}` (the supersession chain oldest→newest).
- `POST /web/memories` — `{content,kind,initiated_by}` → the new record. (Lets the operator add from the UI; `initiated_by` defaults `user`.)
- `PATCH /web/memories/{id}` — `{content}` → supersede, returns new record.
- `DELETE /web/memories/{id}` → `{id,deleted:true}` tombstone.

### Skills (reuse `purse.skills.service`)
- `GET /web/skills` → `{skills:[{name,description,version}]}`.
- `GET /web/skills/{name}?version=` → `{name,description,version,frontmatter,body,versions:[…]}`.
- `PUT /web/skills/{name}` — `{content}` → `{name,version}` (upsert; version bump per C5 rules).

### Connections (reuse `Repo` + `purse.auth.provisioning`)
- `GET /web/connections` → `{connections:[{id,client_name,auth_mode,scopes,writes_enabled,created_at,revoked_at,last_seen_at?}]}` — for the Connections screen. Include revoked ones flagged.
- `DELETE /web/connections/{id}` → revoke → `{id,revoked:true}` (idempotent).

### Personal access tokens
- `POST /web/tokens` — `{client_name, scopes:[…], writes_enabled}` → `{connection:{id,client_name,scopes,writes_enabled}, token}` — **token shown once**; mints via `provisioning.mint_pat`.

### Audit
- `GET /web/audit?limit=100` → `{entries:[{action,target_type,target_id,client_name,agent_id,created_at}]}` newest-first (order by the `seq` column).

### Export (reuse C1.9 `purse.db.export`)
- `GET /web/export` → the full-vault JSON (memories+provenance+history, skills+versions, API names only), `Content-Disposition: attachment`. Documented schema already exists.

### Workspace
- `GET /web/workspace` → counts for the sidebar: `{memories,skills,apis,connections}`.

## Frontend routes (Next.js app-router)
- `/` — public landing (the pitch, OSS/self-host, GitHub, copy-MCP-URL). No auth.
- `/login` — operator login. On success → `/dashboard/memories`.
- `/dashboard/*` — auth-gated dashboard (middleware redirects to `/login` without a session): `memories`, `skills`, `connections`, `tokens`, `audit`. `apis` is a "coming soon" stub (C6). Export is an action, not a page.
- Design: match `docs/design/purse-design-direction.html` exactly — tokens (brass `#C9A24B` sole accent, graphite grounds, mono for all technical text), the four screens shown there, dark-first with a light theme on the same tokens.

## Config / env
- Backend: `PURSE_OWNER_PASSWORD` (enables login), `PURSE_SESSION_SECRET` (signs sessions; may reuse a distinct secret from `PURSE_OAUTH_SECRET`).
- Frontend: `PURSE_BACKEND_URL` (server-side base for the BFF, e.g. `https://purse-staging.fly.dev`), `PURSE_MCP_URL` (shown on landing/onboarding).
