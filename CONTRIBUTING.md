# Contributing to Purse

Purse is pre-alpha and built in public. Contributions are welcome — especially client-compatibility reports, docs, and anything that shortens the path from "clone" to "connected agent."

By contributing you agree that your contributions are licensed under the [Apache License 2.0](LICENSE), and that you will follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

Requirements: **Python 3.12+** and [**uv**](https://docs.astral.sh/uv/). Docker is needed only for the database and the compose smoke test.

```bash
git clone https://github.com/<org>/purse.git
cd purse

# Install the project + dev tools into .venv, from the committed uv.lock
uv sync

# Lint, format check, typecheck, test
uv run ruff check .
uv run ruff format --check .
uv run mypy purse
uv run pytest
```

To format rather than just check:

```bash
uv run ruff format .
uv run ruff check --fix .
```

`uv.lock` is **committed** — CI installs from it. If you change dependencies in `pyproject.toml`, run `uv sync` and commit the updated lock file in the same PR.

### Local database

```bash
cp .env.example .env      # then edit POSTGRES_PASSWORD
docker compose -f docker-compose.dev.yml up db
```

Configuration is environment variables only. **Never put a secret in a compose file, a Dockerfile, or a docs snippet** — `.env` is gitignored and `.env.example` carries placeholders only.

## Code standards

- **Ruff** for lint + format, line length 100, import sorting on (`I`). No hand-formatting.
- **Mypy** across `purse`. `purse.auth.*` and `purse.secrets.*` are checked in **strict** mode — these are the security-critical packages and they stay that way. Do not loosen those overrides; fix the types.
- **Pytest** for tests. New behavior needs a test; bug fixes need a regression test.
- Secret material is wrapped in a redacting type (pydantic `SecretStr` or equivalent) and never logged, exported, serialized into an error, or returned from an MCP tool. This is the project's core invariant and CI enforces it.

## Pull requests

- **One logical change per PR.** Small and reviewable beats complete and unreviewable.
- **Branch from `main`**, name it `<area>/<short-slug>` — e.g. `auth/pat-hashing`, `docs/cursor-setup`.
- **Commit and PR titles** use Conventional Commits: `feat:`, `fix:`, `docs:`, `ci:`, `test:`, `refactor:`, `chore:`, `security:`. Add a scope when it helps: `feat(memory): supersession on update`.
- **Link the task or issue.** If the work maps to a TASKS.md item, cite it by ID (e.g. "closes C3.2") in the description.
- **Fill in the PR template**, including the security checklist item about key material.
- **Green CI is required**: ruff check, ruff format check, mypy, pytest, and the compose/docker build job.
- Discuss large or architectural changes in an issue before writing the code — the build order is deliberate (see below) and out-of-sequence work is hard to land.

## Where to start

Work is organised into clusters in **[TASKS.md](TASKS.md)**, which is the roadmap:

| Cluster | Area | Label |
|---|---|---|
| C0 | Repo & infrastructure | `area:repo` |
| C1 | Data layer | `area:data` |
| C2 | Auth & Connect | `area:auth` |
| C3 | Memory | `area:memory` |
| C4 | MCP gateway & tools | `area:gateway` |
| C5 | Skills | `area:skills` |
| C6 | Secrets & proxy | `area:secrets` |
| C7 | Web UI | `area:ui` |
| C8 | Client compatibility | `area:clients` |
| C9 | Self-hosting & distribution | `area:selfhost` |

Issues also carry `type:feat|bug|docs|ci|security` and, where relevant, `launch-gate`.

**`good-first-issue` labels mark the entry points.** As each cluster opens, the self-contained pieces get tagged — start there. Client-compatibility work (C8) is the most parallelisable: if you use a client we haven't verified yet, a report through the [client compatibility issue template](.github/ISSUE_TEMPLATE/client_compatibility.yml) is a real contribution.

## Reporting bugs and requesting features

Use the issue templates:

- **Bug report** — something behaves incorrectly
- **Feature request** — something should exist
- **Client compatibility** — a specific MCP client fails to connect, authenticate, or call tools

**Do not open a public issue for a security vulnerability.** Follow [SECURITY.md](SECURITY.md) instead.
