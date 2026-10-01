# bdbackup — repository definitions for agents

This file names where the project's definitions live: testing and the gate in
`tests/AGENTS.md`, documentation below. Plans and briefs cite them; they do not restate them.
Internal planning documents live under the gitignored `.ayder/` directory.

## Testing and the gate

`tests/AGENTS.md` owns everything about testing: the test kinds, layers and environments, the
commands and their expected results, the gate procedure and the measured baselines. Plans and
briefs cite it. The gate agent is `.claude/agents/gate-agent.md`; it follows `tests/AGENTS.md`,
section Running the gate, and nothing else, and never edits a file. The developer agent
updates the baselines there before calling it.

## Documentation map

| File | Owns |
|---|---|
| `README.md` | The overview, installation, commands and exit codes, common configuration (`active`, `--dry-run`, scheduling), validation, history and restore, development and release; the Documentation table linking the files below. |
| `docs/README_FILE.md` | File jobs, exclusion templates, `restore --archive`. |
| `docs/README_MYSQLDUMP.md` | mysqldump jobs. |
| `docs/README_XTRABACKUP.md` | xtrabackup jobs (Percona XtraBackup and MariaDB Backup), `restore --backup`, encryption, engine pruning. |
| `docs/README_ENGINES.md` | The engine registry and custom engine plugins. |
| `docs/README_GFS.md` | The ledger and GFS rotation, including `--initialize-structure`. |
| `config.toml.example` | Every config key with a comment. |

Search tool for documentation sweeps: `rg` (ripgrep), over `README.md`, `docs/`,
`config.toml.example`, `bdbackup/` (help strings) and `scripts/`.

## Evidence and ledger

Per effort: `.ayder/superpowers_<YYYYMMDD>/pr/<topic>/` holding `red.txt`, `green.txt`,
`gate/` (stage logs) and `ledger.md`.

## Release

The version lives only in `pyproject.toml` (`project.version`); README §Development describes
lock refresh, build and tagging. Agents never tag, release, push or merge.
