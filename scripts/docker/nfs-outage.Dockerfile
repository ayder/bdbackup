# A privileged container with a kernel NFS server, a hard NFS client mount over loopback,
# iptables and bdbackup, for scripts/integration_nfs_outage.py.
#
# .venv/bin/python -m build -q
# docker build -f scripts/docker/nfs-outage.Dockerfile \
#   --build-arg SOURCE_HASH="$(.venv/bin/python scripts/integration_nfs_outage.py --source-hash)" \
#   -t bdbackup-nfs-outage:test dist
#
# bdbackup is installed from the wheel in the build context (dist/), built on the host, so that
# the test run needs no network: only this build does. The label records the hash of the
# sources the wheel was built from; the script refuses an image built from other sources.
# python:3.13-slim and not debian:bookworm-slim: bdbackup needs Python 3.12 or later.
FROM python:3.13-slim
ARG SOURCE_HASH
LABEL bdbackup.source="${SOURCE_HASH}"
RUN set -eux; \
    test -n "${SOURCE_HASH}"; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        nfs-kernel-server nfs-common rpcbind iptables procps mount; \
    rm -rf /var/lib/apt/lists/*
COPY *.whl /wheels/
RUN python -m venv /opt/bdbackup \
    && /opt/bdbackup/bin/pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels
ENV PATH=/opt/bdbackup/bin:$PATH
