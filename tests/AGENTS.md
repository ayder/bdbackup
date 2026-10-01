# bdbackup tests — agent instructions

Agents follow this file to run bdbackup's tests and its gate. It is complete on its own: it
defines the test kinds, layers and commands, the gate procedure, and the measured baselines,
and it cites no other file. The gate agent (`.claude/agents/gate-agent.md`) follows the section
Running the gate and nothing else; it never edits this file. The developer agent updates
Measured baselines before calling the gate.

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

## Running a single layer

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

## Running the gate

Follow these steps in order on the candidate commit you were given. You only report: do not
edit any file, do not fix anything, do not skip or relax a step, do not create a worktree.
Run every command from the repository root.

Write each step's full output to `<evidence dir>/gate/<step number>-<name>.log`, where the
evidence directory is the one named in your brief. Capture every step the same way, so that
the output of every command in the step lands in its log, not only the last one:

```bash
sh -c '<commands of the step>' > <log> 2>&1; echo "exit=$?" >> <log>
```

1. **Candidate.** Run `git rev-parse HEAD` and `git status --porcelain`.
   Expected: HEAD equals the candidate SHA from your brief; status prints nothing.
   Otherwise: stop, report RED with the actual SHA and status.
2. **Environment.** Run `.venv/bin/python -m pip install -e '.[dev]' -q` and
   `.venv/bin/python -c "import bdbackup, sys; print(bdbackup.__version__, sys.version)"`.
   Expected: exit 0; prints a version and a Python ≥ 3.12.
3. **Lint.** Run `.venv/bin/ruff check bdbackup tests scripts`.
   Expected: `All checks passed!`, exit 0.
4. **Unit.** Run `.venv/bin/python -m pytest -q -p no:cacheprovider`.
   Expected: last line `N passed in …` with no `failed`, `error` or `skipped`; exit 0; and N
   equal to the unit test count under Measured baselines below. A different N is a FAIL of
   this step, reported as `expected <baseline>, measured <N>`, even when every test passed.
   You do not judge whether the change in count was intended.
5. **Docker available.** Run `docker info --format '{{.ServerVersion}}'`.
   Expected: a version string. Otherwise: report steps 6–9 as NOT RUN and OVERALL RED.
6. **Integration mysqldump.** Run `.venv/bin/python scripts/integration_mysql.py`.
   Expected: exit 0.
7. **Integration percona.** Run `.venv/bin/python scripts/integration_physical.py percona`.
   Expected: exit 0 and the line `Real full and two-incremental database recovery passed`.
8. **Integration percona-encrypted.** Same with `percona-encrypted`. Same expectation.
9. **Integration mariadb.** Same with `mariadb`. Same expectation.
10. **Package.** Run `rm -rf dist && .venv/bin/python -m build -q && .venv/bin/twine check dist/* && .venv/bin/python scripts/smoke_install.py dist`
    as one `sh -c '…'`, captured as above.
    Expected: the log contains one `Checking dist/… PASSED` line for the wheel and one for the
    sdist, then the smoke line `Installed wheel: … passed`, then `exit=0`. A log without the
    two twine lines is a FAIL of this step, even when it ends in `exit=0`.
11. **Clean after.** Run `git status --porcelain`.
    Expected: prints nothing except `dist/` if it is not ignored (it is ignored today).

### Report format

```text
GATE REPORT
candidate: <sha>
1 candidate:            PASS|FAIL
2 environment:          PASS|FAIL  <version> <python>
3 lint:                 PASS|FAIL
4 unit:                 PASS|FAIL  <N> passed (baseline <B>)
5 docker:               PASS|FAIL
6 integration mysql:    PASS|FAIL|NOT RUN
7 integration percona:  PASS|FAIL|NOT RUN
8 integration percona-encrypted: PASS|FAIL|NOT RUN
9 integration mariadb:  PASS|FAIL|NOT RUN
10 package:             PASS|FAIL
11 clean after:         PASS|FAIL
logs: <evidence dir>/gate/
OVERALL: GREEN|RED
GATE COMPLETE
```

`OVERALL: GREEN` only when every step is PASS. Anything else is `OVERALL: RED`. For a FAIL,
quote the last 20 lines of that step's log under the report; for a step 4 baseline mismatch,
also the line `expected <baseline>, measured <N>`.

Print nothing else: no analysis, no explanation of why a step failed, no guess whether a
failure was intended. The report and, for a FAIL, the quoted lines are the whole answer.

## Measured baselines

Measured by the runner, never calculated from a diff. The developer agent writes the new value
here and commits it with the candidate before calling the gate; the gate agent never edits it.

| Baseline | Kind | Current value | Runner | Measured at |
|---|---|---|---|---|
| Unit test count | inventory | 345 passed | `.venv/bin/python -m pytest -q` | `23b3d99` |
