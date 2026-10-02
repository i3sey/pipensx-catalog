# pipensx-catalog runner: scrape + merge + publish on a schedule.
# Lightweight by design: python-slim, no cron daemon inside (the host
# triggers one run per tick), state volume avoids re-downloading the
# 23 MB snapshot when nothing changed.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 runner
WORKDIR /app
COPY build_catalog.py scrape.py overrides.json schema.json ./
COPY runner/entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/entrypoint

USER runner
VOLUME /state
ENTRYPOINT ["entrypoint"]
CMD ["run"]
