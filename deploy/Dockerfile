# Playwright MCP needs Node 20+; Debian bookworm ships 18 (it exits with "Playwright requires
# Node.js 20 or higher"), so take Node from the official image instead.
FROM node:22.23.3-bookworm-slim AS node

FROM python:3.12-slim-bookworm

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    JOBHELPER_PLAYWRIGHT_MCP=playwright-mcp \
    JOBHELPER_ROOT=/app

# Virtual display + VNC (so you can log in to portals / clear CAPTCHAs from your phone),
# supervisor to run it all.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify supervisor tzdata ca-certificates curl libatomic1 \
        fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

RUN node --version && npm install -g @playwright/mcp@0.0.83 && npm cache clean --force \
    && playwright-mcp --help >/dev/null

WORKDIR /app
# Dependencies and Chromium first (with a stub package) so code edits don't re-download them.
COPY pyproject.toml ./
RUN mkdir -p jobhelper && touch jobhelper/__init__.py \
    && pip install ".[dev]" && pip uninstall -y jobhelper \
    && playwright install --with-deps chromium \
    && rm -rf jobhelper

# The app runs from /app (python -m jobhelper), not site-packages.
COPY jobhelper ./jobhelper
COPY .claude/skills ./.claude/skills
COPY .claude/agents ./.claude/agents
COPY tests ./tests
COPY deploy/supervisord.conf ./deploy/supervisord.conf

RUN useradd -m -u 1000 jh \
    && mkdir -p /app/data /app/config /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix \
    && chown -R jh:jh /app /ms-playwright
USER jh

HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
    CMD curl -fs http://127.0.0.1:8095/api/health || exit 1

CMD ["supervisord", "-c", "/app/deploy/supervisord.conf"]
