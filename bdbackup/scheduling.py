"""Read-only crontab inspection and safely quoted job recommendations."""

from __future__ import annotations

import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bdbackup.config import Config, Job


def validate_schedule(value: str) -> str:
    """Accept portable five-field numeric cron expressions."""
    if not isinstance(value, str) or len(value.split()) != 5 or any(c in value for c in "\r\n"):
        raise ValueError("schedule must contain five numeric cron fields (e.g. 0 2 * * *)")
    for field, (low, high) in zip(
        value.split(), [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)], strict=True
    ):
        for part in field.split(","):
            match = re.fullmatch(r"(\*|[0-9]+(?:-[0-9]+)?)(?:/([0-9]+))?", part)
            if not match:
                raise ValueError(f"Invalid cron field: {field!r}")
            base, step = match.groups()
            if step is not None and not 1 <= int(step) <= high - low + 1:
                raise ValueError(f"Invalid cron step: {field!r}")
            if base != "*":
                bounds = list(map(int, base.split("-")))
                if any(n < low or n > high for n in bounds) or bounds[0] > bounds[-1]:
                    raise ValueError(f"Cron field out of range: {field!r}")
    return " ".join(value.split())


def os_user() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


def _command(config: Config, job: Job) -> str:
    executable = shutil.which("bdbackup")
    prefix = (
        [str(Path(executable).absolute())]
        if executable
        else [
            str(Path(sys.executable).absolute()),
            "-m",
            "bdbackup.cli",
        ]
    )
    args = [*prefix, "run", "--config", str(config.path), "--", job.name]
    # Cron interprets percent signs before invoking the shell, even inside quotes.
    command = f"cd {shlex.quote(str(Path.cwd()))} && {shlex.join(args)}"
    if any(c in command for c in "\r\n\x00"):
        raise ValueError("Cron paths and job names must not contain control characters")
    return command.replace("%", r"\%")


def _scheduled_jobs(crontab: str, config: Config) -> dict[str, list[str]]:
    """Recognize ordinary bdbackup run commands, including our own recommendations."""
    found: dict[str, list[str]] = {}
    for line in crontab.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(None, 1 if line.startswith("@") else 5)
        if len(fields) not in (2, 6):
            continue
        schedule, command = " ".join(fields[:-1]), fields[-1]
        if len(fields) == 2 and not fields[0].startswith("@"):
            continue
        try:
            tokens = shlex.split(command.replace(r"\%", "%"), comments=True)
        except ValueError:
            continue
        cwd = Path.home()
        if len(tokens) >= 3 and tokens[0] == "cd" and tokens[2] == "&&":
            cwd = Path(tokens[1]).expanduser()
            tokens = tokens[3:]
        # Only recognize an actual command, never an echo/comment containing its name.
        if not tokens:
            continue
        if Path(tokens[0]).name == "bdbackup":
            args = tokens[1:]
        elif (
            len(tokens) > 3
            and Path(tokens[0]).name.startswith("python")
            and tokens[1:3] == ["-m", "bdbackup.cli"]
        ):
            args = tokens[3:]
        else:
            continue
        if any(flag in args for flag in ("--validate", "--cron")):
            continue
        config_path = None
        positional = []
        all_jobs = False
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in {"&&", ";", "||", "|", ">", ">>", "2>&1"}:
                break
            if arg in {"--config", "-c", "--logging"} and index + 1 < len(args):
                if arg != "--logging":
                    config_path = args[index + 1]
                index += 2
                continue
            if arg.startswith("--config="):
                config_path = arg.split("=", 1)[1]
            elif arg == "--all":
                all_jobs = True
            elif not arg.startswith("-"):
                positional.append(arg)
            elif arg == "--" and index + 1 < len(args):
                positional.append(args[index + 1])
                break
            index += 1
        if not config_path or not positional or positional[0] != "run":
            continue
        if (cwd / Path(config_path).expanduser()).resolve() != config.path:
            continue
        names = list(config.jobs) if all_jobs else positional[1:2]
        for name in names:
            if name in config.jobs:
                found.setdefault(name, []).append(schedule)
    return found


def recommend_cron(config: Config, jobs: list[Job]) -> tuple[list[str], bool]:
    lines = [f"Crontab for OS user {os_user()} (read-only; no entries installed)."]
    content = ""
    checked = True
    binary = shutil.which("crontab")
    if binary is None:
        lines.append("[FAIL] crontab executable not found; existing schedules cannot be checked.")
        checked = False
    else:
        try:
            result = subprocess.run(  # noqa: S603
                [binary, "-l"],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "LC_ALL": "C"},
            )
            if result.returncode == 0:
                content = result.stdout
            elif result.returncode == 1 and "no crontab for" in result.stderr.lower():
                lines.append("No crontab is installed for this user.")
            else:
                lines.append("[FAIL] Cannot read crontab; existing schedules are unknown.")
                checked = False
        except (OSError, subprocess.TimeoutExpired):
            lines.append("[FAIL] Cannot read crontab; existing schedules are unknown.")
            checked = False
    existing = _scheduled_jobs(content, config)
    lines.append("Suggested user crontab entries (review times in the cron daemon's timezone):")
    lines.append("SHELL=/bin/sh")
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    if not any(c in path for c in "\n\r\x00\"'"):
        lines.append(f'PATH="{path}"')
    lines.append("# Defaults: backups daily from 02:00; retention daily from 04:00.")
    counters = {False: 0, True: 0}
    # Use all config jobs to keep defaults stable when checking only one job.
    defaults = {}
    for job in config.jobs.values():
        retention = job.type == "retention"
        minute = (240 if retention else 120) + counters[retention] * 15
        counters[retention] += 1
        defaults[job.name] = f"{minute % 60} {(minute // 60) % 24} * * *"
    for job in jobs:
        schedule = job.schedule or defaults[job.name]
        label = repr(job.name)
        if job.name in existing:
            times = ", ".join(existing[job.name])
            lines.append(f"# {label}: already scheduled at {times}; no duplicate suggested.")
            if job.schedule and job.schedule not in existing[job.name]:
                lines.append(f"# Configured schedule is {job.schedule}; review the existing entry.")
            continue
        if job.type == "retention" and not job.params.get("apply", False):
            lines.append(f"# {label}: dry-run retention (apply = false); no files deleted.")
        if job.type == "xtrabackup":
            lines.append(f"# {label}: full backup; includes automatic XtraBackup pruning.")
        try:
            lines.append(f"{schedule} {_command(config, job)}")
        except ValueError as exc:
            lines.append(f"[FAIL] {exc}")
            checked = False
    lines.append(
        "Allow for actual backup duration before retention; times do not enforce ordering."
    )
    return lines, checked
