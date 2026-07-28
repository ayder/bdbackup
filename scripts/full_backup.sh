#!/bin/bash
set -euo pipefail

# DEPRECATED: This script is kept for environments that cannot run the Python
# CLI directly. The Python implementation in bdbackup.xtrabackup is the canonical
# source of truth. Consider migrating to:
#   bdbackup xtrabackup full --database ... --root ...

# Companion script for full MySQL physical backups via xtrabackup/mariabackup.

BACKUP_ROOT="${BACKUP_ROOT:-/backup/mysql}"
DATE=$(date +%F)
TIME=$(date +%H-%M-%S)
BACKUP_DIR="${BACKUP_ROOT}/${DATE}/Full_${TIME}"
LOG_FILE="${BACKUP_ROOT}/${DATE}/backup.log"
FLAG_FILE="${BACKUP_ROOT}/${DATE}/.full_success"
LATEST_SYMLINK="${BACKUP_ROOT}/${DATE}/Full_Latest"

MYSQL_USER="${MYSQL_USER:-xtrabackup}"
MYSQL_PASSWORD="${MYSQL_PASSWORD:-}"
RETENTION_DAYS="${RETENTION_DAYS:-5}"
BACKUP_BIN="${BACKUP_BIN:-xtrabackup}"

mkdir -p "${BACKUP_ROOT}/${DATE}"

echo "[$(date)] Starting FULL MySQL backup into ${BACKUP_DIR}..." | tee -a "${LOG_FILE}"

if [[ -n "${MYSQL_PASSWORD}" ]]; then
    "${BACKUP_BIN}" --backup \
        --compress=zstd \
        --compress-threads=4 \
        --target-dir="${BACKUP_DIR}" \
        --user="${MYSQL_USER}" \
        --password="${MYSQL_PASSWORD}" \
        >> "${LOG_FILE}" 2>&1
else
    "${BACKUP_BIN}" --backup \
        --compress=zstd \
        --compress-threads=4 \
        --target-dir="${BACKUP_DIR}" \
        --user="${MYSQL_USER}" \
        >> "${LOG_FILE}" 2>&1
fi

if [[ $? -eq 0 ]]; then
    ln -sfn "${BACKUP_DIR}" "${LATEST_SYMLINK}"
    touch "${FLAG_FILE}"
    echo "[$(date)] Full backup Successful. Symlink updated." | tee -a "${LOG_FILE}"
    find "${BACKUP_ROOT}" -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}" -exec rm -rf {} +
else
    echo "[$(date)] Full backup FAILED!" | tee -a "${LOG_FILE}"
    exit 1
fi
