# Versions match .python-version and CI's uv pin; digests are registry indexes.
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv
FROM python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1

COPY pyproject.toml uv.lock .python-version README.md LICENSE ./
RUN test "$(python --version)" = "Python $(cat .python-version)" \
    && uv sync --locked --no-dev \
    && uv cache clean
COPY src/ ./src/
COPY prompts/ ./prompts/
COPY alembic.ini ./
COPY infra/backend-entrypoint.sh /usr/local/bin/djedai-start
RUN groupadd --gid 10001 djedai \
    && useradd --uid 10001 --gid djedai --create-home djedai \
    && mkdir -p /app/var/data/datasets /app/var/data/results \
    && chown -R djedai:djedai /app/var \
    && chmod 755 /usr/local/bin/djedai-start

USER djedai
EXPOSE 8000
ENTRYPOINT ["/bin/sh", "/usr/local/bin/djedai-start"]
