"""The built-in metric families, declared once (D11); the catalogue describes each of them.

Every metric is a gauge. Timestamps are Unix seconds, sizes are bytes and durations are seconds.
A plugin engine's families are named ``bdbackup_<engine>_*`` (see ``registry.engine_prefix``) and
are declared by its collector.
"""

from __future__ import annotations

from bdbackup.metrics.registry import Family, Registry

JOB = ("job_name", "job_type")

# Self: always written when the file can be written.
LAST_RUN = "bdbackup_metrics_last_run_timestamp_seconds"
SUCCESS = "bdbackup_metrics_success"
ERROR = "bdbackup_metrics_error"

# Job: any job type that records last_runs.
JOB_LAST_RUN_TIMESTAMP = "bdbackup_job_last_run_timestamp_seconds"
JOB_LAST_RUN_EXIT_CODE = "bdbackup_job_last_run_exit_code"
JOB_LAST_RUN_RESULT = "bdbackup_job_last_run_result"
JOB_RUNNING_SINCE = "bdbackup_job_running_since_timestamp_seconds"
JOB_LAST_SUCCESS_TIMESTAMP = "bdbackup_job_last_success_timestamp_seconds"
JOB_LAST_SUCCESS_BYTES = "bdbackup_job_last_success_bytes"
JOB_LAST_SUCCESS_PHASE = "bdbackup_job_last_success_phase_seconds"

HISTORY_UNITS = "bdbackup_history_units"
DESTINATION_READY = "bdbackup_destination_ready"

GFS_LAST_RUN_UNITS = "bdbackup_gfs_last_run_units"
GFS_STAGE_INFO = "bdbackup_gfs_stage_info"
GFS_STAGE_UNITS = "bdbackup_gfs_stage_units"
GFS_STAGE_BYTES = "bdbackup_gfs_stage_bytes"

BUILTIN = (
    Family(LAST_RUN, "End of this metrics run.", ()),
    Family(SUCCESS, "1 when every source of this metrics run was read, else 0.", ()),
    Family(
        ERROR,
        "1 for each source that failed in this metrics run; job_name is empty unless the "
        "failure belongs to one job.",
        ("source", "job_name", "reason"),
    ),
    Family(JOB_LAST_RUN_TIMESTAMP, "End of the job's last run, dry runs excluded.", JOB),
    Family(JOB_LAST_RUN_EXIT_CODE, "Exit code of the job's last run.", JOB),
    Family(
        JOB_LAST_RUN_RESULT,
        "Result of the job's last run; the value is 1 and the result is the label.",
        (*JOB, "result"),
    ),
    Family(
        JOB_RUNNING_SINCE,
        "Start of the job's oldest unfinished run; absent when none is running.",
        JOB,
    ),
    Family(JOB_LAST_SUCCESS_TIMESTAMP, "Completion of the job's newest successful backup.", JOB),
    Family(JOB_LAST_SUCCESS_BYTES, "Size of the job's newest successful backup.", JOB),
    Family(
        JOB_LAST_SUCCESS_PHASE,
        "Seconds one phase of the job's newest successful backup took.",
        (*JOB, "phase"),
    ),
    Family(
        HISTORY_UNITS,
        "Successful records of the job, by state: available, deleted (by GFS) or unavailable.",
        ("job_name", "state"),
    ),
    Family(
        DESTINATION_READY,
        "1 when the destination marker is a file, else 0.",
        ("job_name", "stage", "path"),
    ),
    Family(
        GFS_LAST_RUN_UNITS,
        "Units by action in the Summary of the gfs job's newest finished run.",
        ("job_name", "action"),
    ),
    Family(
        GFS_STAGE_INFO,
        "1; maps a stage index of the gfs job to its settings.",
        ("job_name", "stage", "period", "keep", "path"),
    ),
    Family(
        GFS_STAGE_UNITS, "Units whose newest location is in the stage.", ("job_name", "stage")
    ),
    Family(GFS_STAGE_BYTES, "Bytes of the units in the stage.", ("job_name", "stage")),
)


def builtin_registry() -> Registry:
    """A registry holding the built-in families; a run adds its engines' families to it."""
    registry = Registry()
    for family in BUILTIN:
        registry.declare(family.name, family.help, family.labels)
    return registry
