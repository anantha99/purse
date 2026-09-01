# What

<!-- What does this change do, in one or two sentences? -->

# Why

<!-- The problem this solves. Link the issue and/or the TASKS.md item, e.g. "closes #12", "implements C3.2". -->

Related: <!-- #issue / TASKS.md item -->

# How

<!-- Notable implementation decisions, tradeoffs, anything a reviewer would otherwise have to reverse-engineer. Delete if trivial. -->

# Type of change

- [ ] `feat` — new functionality
- [ ] `fix` — bug fix
- [ ] `docs` — documentation only
- [ ] `ci` — build, CI, or tooling
- [ ] `test` — tests only
- [ ] `refactor` / `chore` — no behavior change
- [ ] `security` — security fix or hardening

# Testing

<!-- How you verified this. Include the commands you ran. -->

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy purse`
- [ ] `uv run pytest`
- [ ] Tested manually (describe below)

# Security checklist

- [ ] **No secret material can reach an MCP response, log, export, error, or audit entry from this change.** (The core invariant — see SECURITY.md.)
- [ ] No secrets, keys, or tokens are hardcoded in code, tests, fixtures, compose files, or docs.
- [ ] Any new query is workspace-scoped.
- [ ] Any new MCP tool or endpoint enforces its scope, and denial is tested.
- [ ] Any new write records provenance (connection, agent, `initiated_by`).
- [ ] Strict-typed packages (`purse.auth.*`, `purse.secrets.*`) still pass mypy strict — no new `# type: ignore` or loosened overrides.

# Checklist

- [ ] One logical change; branch is up to date with `main`.
- [ ] Title follows Conventional Commits.
- [ ] Tests added or updated for the behavior changed.
- [ ] Docs updated if user-facing behavior changed.
- [ ] `uv.lock` committed if `pyproject.toml` dependencies changed.
- [ ] CI is green.

# Breaking changes

<!-- API, schema, config, or MCP tool contract changes, and the migration path. Write "None" if none. -->

None
