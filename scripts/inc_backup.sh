#!/bin/bash
set -euo pipefail

# Companion script for incremental MySQL physical backups via xtrabackup/mariabackup.
# Requires a successful full backup flag and a valid base directory.

BACKUP_ROOT="${BACKUP_ROOT:-/backup/mysql}"
DATE=$(date +%F)
HOUR=$(date +%H-%M-%S)
LOG_FILE="${BACKUP_ROOT}/${DATE}/backup.log"
FLAG_FILE="${BACKUP_ROOT}/${DATE}/.full_success"
LATEST_SYMLINK="${BACKUP_ROOT}/${DATE}/Full_Latest"

MYSQL_USER="${MYSQL_USER:-xtrabackup}"
MYSQL_PASSWORD="${MYSQL_PASSWORD:-}"
BACKUP_BIN="${BACKUP_BIN:-xtrabackup}"

if [[ ! -f "${FLAG_FILE}" ]]; then
    echo "[$(date)] ERROR: Full backup flag missing. Skipping." | tee -a "${LOG_FILE}"
    exit 1
fi

BASEDIR=""
for dir in $(ls -td "${BACKUP_ROOT}/${DATE}/Incremental"/*/ 2>/dev/null || true); do
    if [[ -f "${dir}xtrabackup_checkpoints" ]]; then
        BASEDIR="${dir%/}"
        break
    fi
done

if [[ -z "${BASEDIR}" ]]; then
    if [[ -f "${LATEST_SYMLINK}/xtrabackup_checkpoints" ]]; then
        BASEDIR="${LATEST_SYMLINK}"
    else
        echo "[$(date)] ERROR: No valid base found with xtrabackup_checkpoints." | tee -a "${LOG_FILE}"
        exit 1
    fi
fi

INC_DIR="${BACKUP_ROOT}/${DATE}/Incremental/${HOUR}"
mkdir -p "${INC_DIR}"

echo "[$(date)] Starting INCREMENTAL backup (Base: ${BASEDIR})..." | tee -a "${LOG_FILE}"

if [[ -n "${MYSQL_PASSWORD}" ]]; then
    "${BACKUP_BIN}" --backup \
        --compress=zstd \
        --compress-threads=4 \
        --target-dir="${INC_DIR}" \
        --incremental-basedir="${BASEDIR}" \
        --user="${MYSQL_USER}" \
        --password="${MYSQL_PASSWORD}" \
        >> "${LOG_FILE}" 2>&1
else
    "${BACKUP_BIN}" --backup \
        --compress=zstd \
        --compress-threads=4 \
        --target-dir="${INC_DIR}" \
        --incremental-basedir="${BASEDIR}" \
        --user="${MYSQL_USER}" \
        >> "${LOG_FILE}" 2>&1
fi

if [[ $? -eq 0 ]]; then
    echo "[$(date)] Incremental backup Successful: ${INC_DIR}" | tee -a "${LOG_FILE}"
else
    echo "[$(date)] Incremental FAILED. Cleaning up ${INC_DIR}" | tee -a "${LOG_FILE}"
    rm -rf "${INC_DIR}"
    exit 1
fi
