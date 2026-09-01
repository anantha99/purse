# syntax=docker/dockerfile:1

FROM python:3.12-slim

# uv provides the install path; copied from the official uv image so the build does
# not need curl or extra CA wrangling.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependency layer first, so source edits do not invalidate the install.
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY purse/ ./purse/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# Configuration is environment variables only. No secrets are baked into this image.
RUN useradd --create-home --uid 10001 purse && chown -R purse:purse /app
USER purse

CMD ["python", "-c", "import purse; print(purse.__version__)"]
