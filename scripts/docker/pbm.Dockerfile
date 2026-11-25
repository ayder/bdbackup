# Build this fixture with scripts/integration_pbm.py --build-probe-images.
ARG MONGO_TAG
ARG PBM_TAG
FROM ghcr.io/astral-sh/uv:0.9.10 AS uv
FROM percona/percona-backup-mongodb:${PBM_TAG} AS pbm
FROM percona/percona-server-mongodb:${MONGO_TAG}
ARG SOURCE_HASH
LABEL bdbackup.source="${SOURCE_HASH}"
USER root
COPY --from=pbm /usr/bin/pbm /usr/bin/pbm-agent /usr/bin/
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python UV_NO_CACHE=1
COPY *.whl /wheels/
RUN mkdir -p /data/db     && chown -R 1001:0 /data/db     && uv python install 3.12     && uv venv --python 3.12 /opt/bdbackup     && uv pip install --python /opt/bdbackup/bin/python /wheels/*.whl     && rm -rf /wheels
USER 1001:0
