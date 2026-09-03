# Purse web dashboard

The operator dashboard for a self-hosted [Purse](../README.md) instance — a
Next.js (App Router, TypeScript, Tailwind) app that acts as a **BFF** in front
of the Python backend's `/web/*` API. The browser only ever talks to this
Next.js origin; the app forwards a session token to the backend server-side.

## Screens

| Route | What |
| --- | --- |
| `/` | Public landing — the pitch, self-host CTA, MCP URL with copy. |
| `/login` | Operator login (single operator per instance). |
| `/app/memories` | The core view — search, add, edit (supersede), delete (tombstone), history. |
| `/app/connections` | Who can open the vault — auth mode, scopes, "writes on", revoke. |
| `/app/skills` | Markdown editor with version bump on save. |
| `/app/tokens` | Mint PATs (token shown once), list, revoke. |
| `/app/audit` | Last 100 audit entries, newest first. |
| `/app/apis` | Coming-soon stub. |
| Export | Sidebar action → downloads the full-vault JSON. |

## Develop

```bash
npm install
cp .env.example .env.local   # then edit
npm run dev                  # http://localhost:3000
```

With `PURSE_BACKEND_URL` **unset**, the BFF serves built-in stub data so every
screen renders without a live backend. Point it at the Python backend to wire
the real API. Any password is accepted in stub mode.

## Build

```bash
npm run lint
npm run build
npm start          # serves the production build
```

## Environment

| Var | Where | Purpose |
| --- | --- | --- |
| `PURSE_BACKEND_URL` | server | Base URL of the Python backend. The BFF proxies `${PURSE_BACKEND_URL}/web/*`. Unset → stubs. |
| `PURSE_MCP_URL` | server | MCP URL shown on the landing page and copied from the dashboard. |
| `PURSE_ALLOW_STUB_FALLBACK` | server | `1` → fall back to stubs when a configured backend is unreachable (dev only). |
| `PURSE_COOKIE_SECURE` | server | `1` → mark the session cookie `Secure` (auto-on when `NODE_ENV=production`). |

All are read server-side at request time — pass them at `docker run` / deploy
time, no rebuild needed. No secrets are baked into the image or the client
bundle.

## Auth model (BFF)

1. `/login` POSTs to `/api/login`, which calls the backend `/web/login`.
2. The backend returns a `session_token`; the `/api/login` route sets it as an
   **httpOnly, SameSite=Lax, Secure** cookie (`purse_session`) on this origin.
   The token never reaches client JS.
3. Every `/api/*` route reads that cookie and forwards it to the backend as
   `Authorization: Bearer <token>`.
4. `middleware.ts` gates `/app/*` on the cookie's presence, redirecting to
   `/login` otherwise. A 401 from the backend also bounces the client to login.

## Docker

```bash
docker build -t purse-web .
docker run -p 3000:3000 \
  -e PURSE_BACKEND_URL=http://host.docker.internal:8000 \
  -e PURSE_MCP_URL=https://your-vault.dev/mcp \
  -e PURSE_COOKIE_SECURE=1 \
  purse-web
```

Builds the standalone Next.js server (`output: "standalone"`) and runs
`node server.js` on port 3000 as a non-root user.
