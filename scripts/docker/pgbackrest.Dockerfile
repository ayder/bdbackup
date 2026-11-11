# PostgreSQL with a pinned pgBackRest, for scripts/integration_pgbackrest.py.
#
# docker build -f scripts/docker/pgbackrest.Dockerfile --build-arg PG_MAJOR=16 \
#   --build-arg PGBACKREST_VERSION=2.58.0 -t bdbackup-pgbackrest:16-2.58.0 scripts/docker
#
# The postgres image already trusts the PGDG apt repository. A pgBackRest version that has
# left it is installed from apt-archive.postgresql.org, which the same key signs and which
# serves over https only, hence ca-certificates on that path. openssl
# generates the throwaway TLS certificates of each test run.
ARG PG_MAJOR
FROM postgres:${PG_MAJOR}
ARG PGBACKREST_VERSION
RUN set -eux; \
    test -n "${PGBACKREST_VERSION}"; \
    apt-get update; \
    if ! apt-get install -y --no-install-recommends \
            "pgbackrest=${PGBACKREST_VERSION}-*" openssl; then \
        apt-get install -y --no-install-recommends ca-certificates; \
        codename="$(. /etc/os-release && echo "${VERSION_CODENAME}")"; \
        sed -e 's#http://apt.postgresql.org/pub/repos/apt#https://apt-archive.postgresql.org/pub/repos/apt#' \
            -e "s# ${codename}-pgdg # ${codename}-pgdg-archive #" \
            /etc/apt/sources.list.d/pgdg.list > /etc/apt/sources.list.d/pgdg-archive.list; \
        apt-get update; \
        apt-get install -y --no-install-recommends \
            "pgbackrest=${PGBACKREST_VERSION}-*" openssl; \
    fi; \
    rm -rf /var/lib/apt/lists/*; \
    test "$(pgbackrest version)" = "pgBackRest ${PGBACKREST_VERSION}"
