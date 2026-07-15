# Unified radar-sim Linux control plane.
#
# Build:
#   docker build -t radar-sim-control .
# Run (an auth file is deliberately required for a non-loopback bind):
#   docker run --rm -p 8878:8878 \
#     -v rsim-data:/var/lib/rsim \
#     -v "$PWD/http-auth.json:/run/secrets/rsim-auth.json:ro" \
#     radar-sim-control
#
# Linux is the Web/API/scheduler/Cluster execution entry point.  It never
# advertises or executes Selena build capability; builds are delegated to an
# authenticated Windows full/light Agent.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RSIM_HOME=/var/lib/rsim \
    RSIM_PORT=8878 \
    RSIM_AUTH_FILE=/run/secrets/rsim-auth.json

WORKDIR /opt/rsim-src
COPY . /opt/rsim-src

# serve-v1 owns the Web UI, REST/SDK API, scheduler and Agent endpoints.  The
# former stdlib `server serve` zipapp is kept only as a compatibility adapter
# and is intentionally not installed as this image's release entry point.
RUN pip install --no-cache-dir ".[v5-server]" \
    && useradd --system --no-create-home --home-dir /var/lib/rsim rsim \
    && mkdir -p /var/lib/rsim/results \
    && chown -R rsim:rsim /var/lib/rsim /opt/rsim-src

USER rsim
EXPOSE 8878
VOLUME ["/var/lib/rsim"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('RSIM_PORT', '8878') + '/api/v1/health', timeout=3)" || exit 1

CMD ["sh", "-c", "exec rsim server serve-v1 --host 0.0.0.0 --port ${RSIM_PORT} --auth-file ${RSIM_AUTH_FILE}"]
