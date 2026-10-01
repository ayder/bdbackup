"""GFS job configuration: stage parsing, the §3.1 rules and --validate."""

import os

import pytest
from click.testing import CliRunner

from bdbackup.cli import main
from bdbackup.config import Config
from bdbackup.gfs.stages import Keep, Stage
from bdbackup.validation import os_user

STAGES = [
    ('["BACKUP"]', "daily", '"5d"'),
    ('["NFS/daily"]', "daily", '"20d"'),
    ('["NFS/weekly"]', "weekly", '"8w"'),
]


def invoke(*args):
    return CliRunner().invoke(main, list(map(str, args)))


def stage_tables(stages=STAGES, extra=None):
    lines = []
    for index, (paths, period, keep) in enumerate(stages, 1):
        lines += ["[[gfs-main.stage]]", f"paths = {paths}", f'period = "{period}"',
                  f"keep = {keep}"]
        lines += (extra or {}).get(index, [])
    return "\n".join(lines) + "\n"


def write_config(tmp_path, *, history='database="state/history.sqlite3"', job="",
                 stages=None, extra=""):
    text = ""
    if history is not None:
        text += f"[history]\n{history}\n"
    text += extra
    text += f'[gfs-main]\ntype="gfs"\n{job}\n'
    text += stages if stages is not None else stage_tables()
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


@pytest.fixture
def gfs_tree(tmp_path):
    for name in ("BACKUP", "NFS/daily", "NFS/weekly"):
        (tmp_path / name).mkdir(parents=True)
    for name in ("NFS/daily", "NFS/weekly"):
        (tmp_path / name / ".bdbackup-destination").touch()
    return tmp_path


def test_gfs_config_parses_stages(gfs_tree):
    job = Config(write_config(gfs_tree)).jobs["gfs-main"]
    root = gfs_tree.resolve()
    assert job.params["stage"] == (
        Stage((root / "BACKUP",), "daily", Keep(5, "d")),
        Stage((root / "NFS/daily",), "daily", Keep(20, "d")),
        Stage((root / "NFS/weekly",), "weekly", Keep(8, "w")),
    )


def case_config(tmp_path, case):
    p = tmp_path.resolve()
    if case == "no-history":
        return write_config(tmp_path, history=None), "gfs jobs require a [history] section"
    if case == "history-in-stage":
        return (write_config(tmp_path, history='database="NFS/daily/h.sqlite3"'),
                f"[history] database must not be inside stage path {p / 'NFS/daily'}")
    if case == "no-stages":
        return write_config(tmp_path, stages=""), "stage must be a non-empty array of tables"
    if case == "unknown-stage-key":
        return (write_config(tmp_path, stages=stage_tables(extra={1: ['name = "hot"']})),
                "stage 1: unknown key 'name'")
    if case == "first-two-paths":
        stages = [('["BACKUP", "NFS/x"]', "daily", '"5d"'), *STAGES[1:]]
        return (write_config(tmp_path, stages=stage_tables(stages)),
                "stage 1: the first stage has exactly one path")
    if case == "first-not-daily":
        stages = [('["BACKUP"]', "weekly", '"5w"'), *STAGES[1:]]
        return (write_config(tmp_path, stages=stage_tables(stages)),
                'stage 1: the first stage\'s period must be "daily"')
    if case == "period-backwards":
        stages = [*STAGES, ('["NFS/d2"]', "daily", '"60w"')]
        return (write_config(tmp_path, stages=stage_tables(stages)),
                'stage 4: period "daily" goes backwards after "weekly"')
    if case in ("keep-format", "keep-bool"):
        keep = '"5 days"' if case == "keep-format" else "5"
        stages = [('["BACKUP"]', "daily", keep), *STAGES[1:]]
        return (write_config(tmp_path, stages=stage_tables(stages)),
                "stage 1: keep must be a positive whole number followed by d, w, m or y")
    if case == "keep-not-longer":
        stages = [STAGES[0], ('["NFS/daily"]', "daily", '"30d"'),
                  ('["NFS/weekly"]', "weekly", '"1m"')]
        return (write_config(tmp_path, stages=stage_tables(stages)),
                'stage 3: keep "1m" must be longer than "30d" on every calendar')
    if case == "paths-nested":
        stages = [*STAGES[:2], ('["NFS/daily/weekly"]', "weekly", '"8w"')]
        return (write_config(tmp_path, stages=stage_tables(stages)),
                f"stage paths overlap: {p / 'NFS/daily'} and {p / 'NFS/daily/weekly'}")
    if case == "job-into-later-stage":
        extra = '[xb]\ntype="xtrabackup"\nbackup_root="NFS/daily/db"\nretention_days=0\n'
        return (write_config(tmp_path, extra=extra),
                f"Job 'xb' writes into stage path {p / 'NFS/daily'}; "
                "engines write only into the first stage")
    if case == "xtrabackup-needs-zero":
        extra = '[xb]\ntype="xtrabackup"\nbackup_root="BACKUP/db"\n'
        return (write_config(tmp_path, extra=extra),
                f"Job 'xb' writes into first stage {p / 'BACKUP'}: set retention_days = 0")
    if case == "file-needs-timestamp":
        extra = '[daily]\ntype="file"\nbackup_dst="BACKUP/files/daily"\n'
        return (write_config(tmp_path, extra=extra),
                f"Job 'daily' writes into first stage {p / 'BACKUP'}: set timestamp = true")
    if case == "unknown-job-key":
        return (write_config(tmp_path, job='restore_root="r"'),
                "a gfs job accepts only type, active and stage")
    raise AssertionError(case)


CASES = [
    "no-history", "history-in-stage", "no-stages", "unknown-stage-key", "first-two-paths",
    "first-not-daily", "period-backwards", "keep-format", "keep-bool", "keep-not-longer",
    "paths-nested", "job-into-later-stage", "xtrabackup-needs-zero", "file-needs-timestamp",
    "unknown-job-key",
]


@pytest.mark.parametrize("case", CASES)
def test_gfs_config_rule_violations_exit_2(gfs_tree, case):
    path, message = case_config(gfs_tree, case)
    for args in (("run", "--validate", "--config", path), ("run", "-c", path, "-j", "gfs-main")):
        result = invoke(*args)
        output = " ".join(result.output.split())
        assert result.exit_code == 2, result.output
        assert message in output, result.output


def test_gfs_validate_reports_stage_markers(gfs_tree):
    (gfs_tree / "NFS/weekly/.bdbackup-destination").unlink()
    path = write_config(gfs_tree)
    result = invoke("run", "--validate", "--config", path)
    root = gfs_tree.resolve()
    lines = result.output.splitlines()
    assert result.exit_code == 1, result.output
    assert (f"[OK] Stage path {root / 'NFS/daily'}: writable, marker present and writable"
            in lines), result.output
    assert (f"[FAIL] Stage path {root / 'NFS/weekly'}: .bdbackup-destination missing; "
            f"run bdbackup run -c {path.resolve()} -j gfs-main --initialize-structure"
            in lines), result.output
    assert f"Stage path {root / 'BACKUP'}" not in result.output


def test_validate_marker_not_writable(gfs_tree):
    assert os.geteuid() != 0, "permission tests need a non-root user"
    marker = gfs_tree / "NFS/weekly/.bdbackup-destination"
    marker.chmod(0o444)
    try:
        result = invoke("run", "--validate", "--config", write_config(gfs_tree))
    finally:
        marker.chmod(0o644)
    assert result.exit_code == 1, result.output
    assert (f"[FAIL] Stage path {gfs_tree.resolve() / 'NFS/weekly'}: .bdbackup-destination "
            f"not writable by OS user {os_user()}" in result.output.splitlines()), result.output

