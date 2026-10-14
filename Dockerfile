# pipensx-catalog runner Dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application modules, schemas and baseline catalog
COPY manage_catalog.py storage.py extractor.py flaresolverr_client.py scraper.py export_catalog.py schema.json overrides.json catalog.json ./

# Entrypoint script
COPY runner/entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/entrypoint

VOLUME /state
ENTRYPOINT ["entrypoint"]
CMD ["run"]
