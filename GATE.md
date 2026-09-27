# Gate instructions

Follow these steps in order on the candidate commit you were given. You only report: do not
edit any file, do not fix anything, do not skip or relax a step, do not create a worktree.
The source of truth for commands and expected results is `CLAUDE.md` (sections Commands and
Layers and environments); if this file and `CLAUDE.md` disagree, stop and report that.

Write each step's full output to `<evidence dir>/gate/<step number>-<name>.log`, where the
evidence directory is the one named in your brief. Capture every step the same way, so that
the output of every command in the step lands in its log, not only the last one:

```bash
sh -c '<commands of the step>' > <log> 2>&1; echo "exit=$?" >> <log>
```

## Steps

1. **Candidate.** Run `git rev-parse HEAD` and `git status --porcelain`.
   Expected: HEAD equals the candidate SHA from your brief; status prints nothing.
   Otherwise: stop, report RED with the actual SHA and status.
2. **Environment.** Run `.venv/bin/python -m pip install -e '.[dev]' -q` and
   `.venv/bin/python -c "import bdbackup, sys; print(bdbackup.__version__, sys.version)"`.
   Expected: exit 0; prints a version and a Python ≥ 3.12.
3. **Lint.** Run `.venv/bin/ruff check bdbackup tests scripts`.
   Expected: `All checks passed!`, exit 0.
4. **Unit.** Run `.venv/bin/python -m pytest -q -p no:cacheprovider`.
   Expected: last line `N passed in …` with no `failed`, `error` or `skipped`; exit 0.
   Record N.
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

## Report format

```text
GATE REPORT
candidate: <sha>
1 candidate:            PASS|FAIL
2 environment:          PASS|FAIL  <version> <python>
3 lint:                 PASS|FAIL
4 unit:                 PASS|FAIL  <N> passed
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
quote the last 20 lines of that step's log under the report.
