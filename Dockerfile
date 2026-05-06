# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

# Chrome for Testing — Google's automation-friendly Chrome build.
# Two modes:
#   * CHANNEL (default): resolve the channel's latest version from the official
#     last-known-good catalog at build time. Channels: Stable | Beta | Dev | Canary.
#   * VERSION: pin to an exact version. When set, takes precedence over CHANNEL.
# Caveat: Docker's layer cache keys off the RUN command text and build ARGs, not
# the resolved URL — so a channel-based rebuild will happily reuse a stale CfT
# layer. Pass `--no-cache` (or bump VERSION) when you want fresh.
# Only linux64 is published, so the image is implicitly amd64.
# Catalog: https://googlechromelabs.github.io/chrome-for-testing/
ARG CHROME_FOR_TESTING_CHANNEL=Stable
ARG CHROME_FOR_TESTING_VERSION=

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:/root/.local/bin:$PATH \
    HOST=0.0.0.0 \
    PORT=8000 \
    CHROME_PATH=/opt/chrome-for-testing/chrome-linux64/chrome

# Keep apt's downloaded .debs around so BuildKit cache mounts work.
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache

# Note: no `chromium` package — we ship Chrome for Testing instead (see below).
# The libs below are CfT's runtime dependencies (same set Debian's chromium pulls).
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        tini \
        unzip \
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

# Install Chrome for Testing. We use it instead of Google Chrome stable because
# stable hard-blocks --load-extension regardless of feature flags, breaking
# UNPACKED_EXTENSION_DIRS. CfT mirrors stable behavior closely but keeps the
# automation switches honored.
RUN set -eu; \
    if [ -n "${CHROME_FOR_TESTING_VERSION}" ]; then \
        url="https://storage.googleapis.com/chrome-for-testing-public/${CHROME_FOR_TESTING_VERSION}/linux64/chrome-linux64.zip"; \
        echo "Pinned CfT ${CHROME_FOR_TESTING_VERSION} → ${url}"; \
    else \
        catalog="https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"; \
        url=$(curl -fsSL "${catalog}" | python3 -c 'import json,sys,os; \
ch=os.environ["CHROME_FOR_TESTING_CHANNEL"]; \
d=json.load(sys.stdin)["channels"][ch]; \
print(next(x["url"] for x in d["downloads"]["chrome"] if x["platform"]=="linux64"))'); \
        echo "Resolved CfT channel=${CHROME_FOR_TESTING_CHANNEL} → ${url}"; \
    fi; \
    curl -fsSL -o /tmp/cft.zip "${url}"; \
    mkdir -p /opt/chrome-for-testing; \
    unzip -q /tmp/cft.zip -d /opt/chrome-for-testing; \
    rm /tmp/cft.zip; \
    /opt/chrome-for-testing/chrome-linux64/chrome --version

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
