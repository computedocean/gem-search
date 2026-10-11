# Multi-arch index digest for python:3.12-slim (3.12.15, Debian trixie).
# Update procedure: docs/DOCKER.md#updating-the-base-image
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GEM_SEARCH_HOST=0.0.0.0 \
    LAUNCH_MODE=dry_run \
    GEM_SEARCH_DRY_RUN_LOCK=1 \
    FREE_FEED_ENABLED=1 \
    ENABLE_PAID_X=0 \
    GROK_ENABLED=0 \
    SPIDER_ALLOW_LAUNCH=0

WORKDIR /app

RUN groupadd --system --gid 10001 gemsearch \
    && useradd --system --uid 10001 --gid gemsearch --home-dir /app --no-create-home --shell /usr/sbin/nologin gemsearch \
    && mkdir -p /app/data \
    && chown gemsearch:gemsearch /app/data \
    && chmod 0700 /app/data

# Application code is owned by root and not writable by the service user; only /app/data is.
COPY --chown=root:root . /app

USER 10001:10001

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3).close()"]

CMD ["python", "app.py", "--port", "8787"]
