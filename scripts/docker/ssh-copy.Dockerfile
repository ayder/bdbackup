# Use Ubuntu's SSH/rsync packages on both fixture versions. Managed Python 3.12
# also runs on 22.04, whose system Python is too old for the candidate wheel.
ARG UBUNTU=22.04
FROM ghcr.io/astral-sh/uv:0.9.10 AS uv
FROM ubuntu:${UBUNTU}
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates openssh-client openssh-server rsync \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 0755 /run/sshd \
    && useradd --create-home --shell /bin/sh bdprobe \
    && passwd --delete bdprobe
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python UV_NO_CACHE=1
COPY *.whl /wheels/
RUN uv python install 3.12 \
    && uv venv --python 3.12 /opt/bdbackup \
    && uv pip install --python /opt/bdbackup/bin/python /wheels/*.whl \
    && rm -rf /wheels
