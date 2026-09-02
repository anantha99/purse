# Deploy Purse to Fly.io (staging)

A staging instance gives claude.ai web / ChatGPT connectors a public HTTPS URL
to reach — which they cannot do against `localhost` — and doubles as the seed of
the hosted beta. Everything below is run from the repo root with the
[`fly` CLI](https://fly.io/docs/flyctl/install/) authenticated (`fly auth login`).

Secrets are never committed: `PURSE_OAUTH_SECRET`, the Postgres `DATABASE_URL`,
and the database password all go in via `fly secrets`, which stores them
encrypted and injects them as environment variables at runtime.

## One-time setup

```sh
# 1. Create the app (accept the fly.toml in this repo; don't deploy yet).
fly launch --no-deploy --copy-config --name purse-staging

# 2. Provision managed Postgres and attach it — this sets DATABASE_URL as a secret.
fly postgres create --name purse-staging-db --region iad --initial-cluster-size 1
fly postgres attach purse-staging-db --app purse-staging

# 3. Enable the pgvector extension (one time), via a psql console on the cluster.
fly postgres connect --app purse-staging-db -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 4. Set the OAuth signing secret (any long random string; generate one).
fly secrets set --app purse-staging PURSE_OAUTH_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"

# 5. Confirm the public URL in fly.toml (PURSE_PUBLIC_URL) matches the app's
#    https://<app>.fly.dev hostname, then deploy.
fly deploy --app purse-staging
```

The `release_command = "alembic upgrade head"` in `fly.toml` runs the migrations
automatically on each deploy, before the new version takes traffic.

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
fly apps destroy purse-staging-db
```
