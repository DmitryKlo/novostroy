FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NOVOSTROY_DATA_MODE=real \
    NOVOSTROY_REAL_STORE=empty \
    NOVOSTROY_REGISTRY_MODE=estateline \
    NOVOSTROY_EXPOSE_SOURCES=0 \
    NOVOSTROY_GEODATA_PYTHON=/usr/local/bin/python \
    NOVOSTROY_VALHALLA_TILE_DIR=/app/data/social/valhalla_moscow/tiles

WORKDIR /app
COPY deploy/requirements-api.txt /app/deploy/requirements-api.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates librsvg2-bin \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir -r /app/deploy/requirements-api.txt \
    && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin novostroy \
    && mkdir -p /app/data/runtime /app/assets \
    && chown -R novostroy:novostroy /app/data /app/assets

COPY backend/ /app/backend/
COPY analytics/ /app/analytics/
COPY recommendations/ /app/recommendations/
COPY contracts/ /app/contracts/
COPY deploy/entrypoint.py /app/deploy/entrypoint.py
COPY scripts/seed_developer_media_capture.py /app/scripts/seed_developer_media_capture.py

USER novostroy
EXPOSE 8000
CMD ["python", "/app/deploy/entrypoint.py"]
