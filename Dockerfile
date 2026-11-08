FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app
RUN groupadd --gid 10001 downloader \
    && useradd --uid 10001 --gid 10001 --no-create-home downloader \
    && install -o 10001 -g 10001 -m 0700 -d /media /spool
COPY prodownloader /app/prodownloader
USER 10001:10001

FROM base AS app
USER root
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY migrations migrations
USER 10001:10001

FROM node:22-bookworm-slim AS node

FROM base AS extractor
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browser
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY requirements-extractor.txt .
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && pip install --no-cache-dir -r requirements-extractor.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
