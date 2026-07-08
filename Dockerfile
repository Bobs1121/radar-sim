# Control-plane server image (Linux).
#
# This image runs ONLY the rsim control server — the job/task scheduler that
# multiple Windows agents (and web consoles) connect to over HTTP. It does NOT
# build or run selena simulations itself: selena execution happens on the
# Windows machines that run `rsim agent` against this server. See
# docs/linux-server-deploy.md for the full architecture.
#
# The server is pure Python stdlib (no PyYAML/asammdf), so the image needs no
# pip install — just Python 3.9+ and the rsim_server.pyz zipapp.

FROM python:3.11-slim

# Data root for per-user SQLite control DBs (_control_<user>.db). Mount a
# volume here so jobs/logs survive container restarts.
ENV RSIM_HOME=/var/lib/rsim
RUN mkdir -p "$RSIM_HOME/results"

# The zipapp bundles the minimal server file set (stdlib-only).
COPY dist/rsim_server.pyz /opt/rsim/rsim_server.pyz

# Drop root.
RUN useradd --system --no-create-home --home-dir /var/lib/rsim rsim \
    && chown -R rsim:rsim /var/lib/rsim /opt/rsim
USER rsim

EXPOSE 8877
VOLUME ["/var/lib/rsim"]

# Bind 0.0.0.0 so Windows agents on other hosts can reach the container.
# Mode A (Linux cluster-only service): restrict to cluster.run so the server
# never accepts local.check / local.build_selena / local.run_sim jobs — those
# require a Windows machine with the full toolchain, not this Linux box.
CMD ["python", "/opt/rsim/rsim_server.pyz", "server", "serve", \
     "--host", "0.0.0.0", "--port", "8877", \
     "--allowed-task-types", "cluster.run"]
