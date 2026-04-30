# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:/root/.local/bin:$PATH \
    HOST=0.0.0.0 \
    PORT=8000

# Keep apt's downloaded .debs around so BuildKit cache mounts work.
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        tini \
        xvfb \
        xauth \
        ca-certificates \
        curl \
        fonts-liberation \
        fonts-noto-color-emoji \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        xdg-utils

# uv for dependency install.
RUN --mount=type=cache,target=/root/.cache/uv \
    pip install --no-cache-dir uv

WORKDIR /app

# Install deps before copying source so dep layer is reused on code-only changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Non-root user. /tmp stays world-writable (default 1777) for chromium's user-data-dir.
RUN useradd --create-home --uid 1000 passe \
    && mkdir -p /extensions \
    && chown -R passe:passe /app /extensions

COPY --chown=passe:passe docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER passe

VOLUME ["/extensions"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
