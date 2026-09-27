# bdbackup — repository definitions for agents

This file is the project's source of truth for how work is tested, gated and documented.
Plans and briefs cite it; they do not restate it. Internal planning documents live under the
gitignored `.ayder/` directory.

## Test kinds

| Kind | Meaning | Red proof |
|---|---|---|
| `baseline regression` | Asserts behaviour the parent commit gets wrong, or a stated behaviour change against the parent. | Fails on the parent commit with its named assertion text. |
| `new behaviour` | Asserts a new interface or option. | Fails at an assertion once a minimal callable version exists (options accepted, no behaviour); a missing option or symbol is not a red. |
| `preservation` | Asserts behaviour that must not change. | None; its assertion must visibly depend on the preserved behaviour. |

## Layers and environments

| Layer | What it runs | Environment | Where |
|---|---|---|---|
| `unit` | pytest tests of the package and the CLI (`click.testing.CliRunner`). Backup tools and database clients are never executed: physical backups use the `physical_runner` fixture (`tests/conftest.py`) through a patched `subprocess.run`; MySQL clients are patched as in `tests/test_helpers.py`. | `local-python` | `tests/` |
| `integration` | Real servers and real backup tools in disposable, network-isolated Docker containers with synthetic data; a restore starts a server on the recovered data and checks rows. | `docker` | `scripts/integration_mysql.py`, `scripts/integration_physical.py` |

- `local-python`: Python 3.12+, editable install `pip install -e '.[dev]'` in `.venv/`. CI runs
  Python 3.12, 3.13 and 3.14.
- `docker`: a running Docker daemon with the images pulled: `mysql:8.4`,
  `percona/percona-server:8.4`, `percona/percona-xtrabackup:8.4`, `mariadb:11.4`. The scripts
  create and remove only their own containers and volumes.

Code that runs a backup tool, a database client or a restore against a server has an
`integration` proof in `scripts/integration_*.py`. CLI and configuration logic is proven at
`unit`.

## Commands

| Purpose | Command | Expected result |
|---|---|---|
| Lint | `.venv/bin/ruff check bdbackup tests scripts` | `All checks passed!`, exit 0 |
| Unit, all | `.venv/bin/python -m pytest -q` | last line `N passed in …`, no `failed`/`error`, exit 0 |
| Unit, named tests with evidence | `.venv/bin/python -m pytest -v <file>::<test>` | one line per test id ending in `PASSED` or `FAILED` |
| Integration | `.venv/bin/python scripts/integration_mysql.py`; `.venv/bin/python scripts/integration_physical.py percona`, `percona-encrypted`, `mariadb` | each ends with its success line (`Real full and two-incremental database recovery passed` for physical runs) and exit 0 |
| Package | `.venv/bin/python -m build && .venv/bin/twine check dist/* && .venv/bin/python scripts/smoke_install.py dist` | `PASSED` for every artifact from twine; smoke script exit 0 |

Reading pytest evidence: only a test id's own `PASSED` / `FAILED` line in `-v` output counts.
A summary line alone, a `SKIPPED` line, or a missing test id is not evidence. A parametrized
test reports one line per parameter id; each is required.

## Gate

The gate instructions file is `GATE.md` at the repository root. The gate agent follows it and
nothing else.

## Baselines

| Baseline | Kind | Current value | Runner | Measured at |
|---|---|---|---|---|
| Unit test count | inventory | 257 passed | `.venv/bin/python -m pytest -q` | `ca12781` |

The coordinator updates this table after the runner reports; the gate agent never edits it.

## Documentation map

| File | Owns |
|---|---|
| `README.md` | All user documentation: commands, options, config keys, history and restore, development and release. |
| `config.toml.example` | Every config key with a comment. |

Search tool for documentation sweeps: `rg` (ripgrep), over `README.md`, `config.toml.example`,
`bdbackup/` (help strings) and `scripts/`.

## Evidence and ledger

Per effort: `.ayder/superpowers_<YYYYMMDD>/pr/<topic>/` holding `red.txt`, `green.txt`,
`gate/` (stage logs) and `ledger.md`.

## Release

The version lives only in `pyproject.toml` (`project.version`); README §Development describes
lock refresh, build and tagging. Agents never tag, release, push or merge.
