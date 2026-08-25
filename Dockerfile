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

ARG RSIM_AGENT_TOOLS_RELEASE=container

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
RUN pip install --no-cache-dir ".[v5-server,mcp]" \
    && mkdir -p /opt/agent-tools-wheels \
    && pip wheel --no-cache-dir --wheel-dir /opt/agent-tools-wheels ".[mcp]" \
    && for pyver in 3.10 3.11 3.12 3.13; do \
         pip download --no-cache-dir --dest /opt/agent-tools-wheels --only-binary=:all: \
           --platform win_amd64 --python-version "$pyver" --implementation cp \
           "PyYAML>=6.0" "httpx==0.28.1" "pydantic==2.13.4" "mcp>=1.28,<2" "pywin32>=310"; \
       done \
    && python scripts/build_agent_tools_bundle.py \
        --wheel-dir /opt/agent-tools-wheels \
        --skill-dir /opt/rsim-src/skills/radar-sim-simulation \
        --output /opt/rsim-src/dist/agent-tools.zip \
        --release-version "${RSIM_AGENT_TOOLS_RELEASE}" \
        --sdk-version "$(python -c 'from importlib.metadata import version; print(version("radar-sim"))')" \
        --mcp-version "$(python -c 'import radar_sim_mcp; print(radar_sim_mcp.__version__)')" \
        --mcp-dependency-version "$(python -c 'from importlib.metadata import version; print(version("mcp"))')" \
        --skill-version "${RSIM_AGENT_TOOLS_RELEASE}" \
        --connector-contract-version "$(python -c 'from core.agent_policy import WINDOWS_CONNECTOR_CONTRACT_VERSION; print(WINDOWS_CONNECTOR_CONTRACT_VERSION)')" \
    && python -c 'from core.agent_distribution import AgentToolsDistribution; AgentToolsDistribution.from_files("/opt/rsim-src/dist/agent-tools.zip", "/opt/rsim-src/dist/agent-tools.json")' \
    && useradd --system --no-create-home --home-dir /var/lib/rsim rsim \
    && mkdir -p /var/lib/rsim/results \
    && chown -R rsim:rsim /var/lib/rsim /opt/rsim-src

USER rsim
EXPOSE 8878
VOLUME ["/var/lib/rsim"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('RSIM_PORT', '8878') + '/api/v1/health', timeout=3)" || exit 1

CMD ["sh", "-c", "exec rsim server serve-v1 --host 0.0.0.0 --port ${RSIM_PORT} --auth-file ${RSIM_AUTH_FILE}"]
