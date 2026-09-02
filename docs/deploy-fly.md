# Deploy Purse to Fly.io (staging)

A staging instance gives claude.ai web / ChatGPT connectors a public HTTPS URL
to reach — which they cannot do against `localhost` — and doubles as the seed of
the hosted beta. Everything below is run from the repo root with the
[`fly` CLI](https://fly.io/docs/flyctl/install/) authenticated (`fly auth login`).

Secrets are never committed: `PURSE_OAUTH_SECRET`, the Postgres `DATABASE_URL`,
and the database password all go in via `fly secrets`, which stores them
encrypted and injects them as environment variables at runtime.

## Database: pgvector is required, so NOT Fly Postgres

Purse's memory layer needs the `pgvector` extension, and **Fly's default Postgres
image does not ship it** (`CREATE EXTENSION vector` fails with "extension is not
available"). Use a Postgres that has pgvector built in. The staging instance uses
**Neon** (free tier, AWS us-east-2) — any pgvector-capable Postgres works
(Neon, Supabase, Crunchy, or a self-run `pgvector/pgvector` image on Fly).

Create a Neon project, then copy its **direct** connection string (the endpoint
host without `-pooler`). Migrations create the `vector` extension themselves, and
Neon's default role is allowed to.

## One-time setup

```sh
# 1. Create the app (uses the fly.toml in this repo; don't deploy yet).
fly apps create purse-staging --org personal

# 2. Point it at your pgvector Postgres. Single-quote the whole string so the
#    shell doesn't eat the '&' or the password.
fly secrets set --app purse-staging \
  DATABASE_URL='postgresql://USER:PASSWORD@ep-xxxx.us-east-2.aws.neon.tech/DBNAME?sslmode=require'

# 3. Set the OAuth signing secret (any long random string).
fly secrets set --app purse-staging \
  PURSE_OAUTH_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"

# 4. Confirm PURSE_PUBLIC_URL in fly.toml matches https://<app>.fly.dev, then deploy.
fly deploy --app purse-staging

# 5. Pin to one machine — OAuth codes/tokens are in-memory per process, so a
#    multi-machine deploy breaks OAuth flows and splits the rate-limit budget.
fly scale count 1 --app purse-staging
```

The `release_command` in `fly.toml` runs the migrations
(`python -c "from purse.db.migrate import upgrade; upgrade()"`) automatically on
each deploy, before the new version takes traffic — including creating the
`vector` extension on first run.

## First credentials

Purse prints an onboarding PAT on first boot only if you run the bootstrap. On
Fly, run it once against the deployed database:

```sh
fly ssh console --app purse-staging -C "python -m purse.auth.bootstrap"
```

Copy the printed `purse_pat_…` token — it is shown once. Use it as the bearer
for PAT-first clients (Codex, headless) and for the REST smoke check:

```sh
curl -s https://purse-staging.fly.dev/v1/memories \
  -H "Authorization: Bearer purse_pat_…"
```

## Connecting clients

- **MCP endpoint:** `https://purse-staging.fly.dev/mcp`
- **OAuth discovery:** `https://purse-staging.fly.dev/.well-known/oauth-authorization-server`
- Per-client setup lives in the client docs (C8). OAuth clients hit the discovery
  URL and are sent through the consent page; PAT clients use the token above.

## Tearing down

```sh
fly apps destroy purse-staging
# then delete the Neon project from the Neon dashboard
```
