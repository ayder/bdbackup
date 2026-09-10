# Release preparation: bdbackup 0.5.1

Updated 2026-09-10 after implementing the repair plan in FIX_PLAN.md and adding
SQLite backup history and guided restoration.

The first hosted run passed all Python, MySQL and MariaDB checks. Its Percona
job exposed root-owned files created inside incremental working copies by the
Docker test harness. The harness now restores ownership on both target and
incremental copies before host-side cleanup. This correction is versioned as
0.5.1; already published tags remain intact.

**The listed code-level findings are fixed and locally validated.** This is a
release candidate for the documented support scope. The GitHub destination is
`ayder/bdbackup`; the authenticated owner has admin access, and the existing
remote branch is an ancestor of this checkout. Publication can preserve its
original history. PyPI publication still requires successful release-tag CI and
the correct trusted publisher/version availability; it is a separate action.
The original no-go assessment below describes the code before these repairs.

| Final check | Result |
| --- | --- |
| Unit/regression tests, Python 3.12, 3.13 and 3.14 | 173 passed on each |
| Python 3.14 statement coverage | 87% |
| Ruff, whitespace, actionlint 1.7.7, offline lockfile check | Passed |
| Isolated source/wheel build, Twine, distribution content checks | Passed |
| Fresh installed-wheel imports, CLI, history, recovery and failure preservation | Passed on 3.12, 3.13 and 3.14 |
| MySQL 8.4.9 logical recovery | Parallel/full dumps, special-character password, data/routines/events/triggers passed |
| Percona Server 8.4.10-10 / XtraBackup 8.4.0-6 | Compressed full plus two increments; full and incremental recovery passed |
| MariaDB 11.4.13 | Full plus two increments; both recovery points passed |

Database checks were performed during the 0.4.0 safety repairs. The 0.5.0
history and recovery dispatch additions have unit/regression and installed-wheel
coverage. Database checks used actual server and backup binaries inside disposable,
network-isolated containers. The physical test substitutes Docker execution for
the local process launcher while exercising the backend's actual backup,
verification, chain metadata, decompression and preparation logic. Restored
servers were started from independent Linux data volumes and queried for the
expected records. These tests do not certify every version or production setup.

Repairs by area:

- Optional SQLite history records backup attempts, UTC timestamps, outcomes,
  artifact identities and recovery routing without storing credentials.
  Guided restore offers successful, available artifacts and requires a new
  recovery destination. File extraction, SQL recovery to a file, and physical
  preparation are supported; importing SQL or starting a database stays separate.
- File backups stage output under a writer lock, propagate input/traversal
  failures, check selected members and readable payloads, detect ordinary file
  changes, verify before atomic replacement, and discard failed staging files.
  Destination files and their aliases are excluded from their own backups.
- Restore uses an explicit safe extraction filter, rejects path/link escapes and
  special files, accepts existing destination directories, and restores contained
  links, empty directories and ordinary directory metadata.
- CLI failures use actual nonzero process exits; password prompts, parallel
  database dispatch, safe/custom dump options and per-job failure isolation work.
- Logical dumps keep stderr separate, reject empty/corrupt gzip payloads, publish
  unique verified files, and quote credential values. Metadata logging imports
  are repaired; config paths expand home and resolve relative to the config file.
- Physical backups validate checkpoints before marking success or pruning, lock
  retention against writers, record unique identities and explicit full/parent
  LSN dependencies, and handle both MySQL and modern MariaDB checkpoint names.
  Preparation decompresses and merges working copies, verifies the requested
  recovery point, and preserves original backups.
- Flat-file retention pins intermediate increments as well as their full and
  defers expiration when skipped files make dependencies uncertain. Its contract
  now explicitly requires a single chronological stream. The fixed-date config
  test freezes its clock.
- Publishing requires a matching version tag and successful reusable CI,
  including real database recovery checks. It uploads the tested artifacts
  instead of rebuilding in the publishing job. Examples and scripts ship in the
  source distribution; POSIX and Python/format requirements are documented.

Behavior changes to account for:

- `tar.zst` requires Python 3.14; Python 3.12/3.13 users can use tar or tar.gz.
- Verification before publication is mandatory. `--no-verify` skips only the
  additional verification pass after publication.
- Physical compression defaults to off. Legacy physical backups require a new
  full before creating incrementals with explicit dependency metadata.
- `xtrabackup prepare` requires `--root` and a new `--dst` outside the backup root.
  Prepared files still need the server's ownership and a compatible filesystem
  when moved into its data directory.
- GFS flat-file retention is not an adapter for XtraBackup's directory layout.
  Use physical prune for those complete dated chains.

The release version is declared only in `pyproject.toml`; runtime and CLI versions use generated
package metadata, and the release tag gate reads `pyproject.toml` directly.
The MIT license is included explicitly in distribution metadata and artifacts.
PyPI name/version ownership and trusted-publisher configuration have not been
changed or claimed as verified.

---

## Original audit (before repairs)

# Release review: bdbackup 0.4.0

Reviewed 2026-09-10 at commit `c58a5bb`. Application code was not changed and nothing was published.

**Verdict: do not publish this version to PyPI or mark it as a usable GitHub release.** Publishing the source as an explicitly experimental GitHub project is reasonable, provided its known limitations are visible. The package builds, but confirmed security, data-loss, and false-success defects prevent release approval.

## Verification results

| Check | Result |
| --- | --- |
| Ruff (`ruff check bdbackup tests`) | Pass |
| Python 3.14 test suite with coverage | 84 passed, 1 failed; 80% statement coverage |
| Python 3.12 and 3.13 test suites | Each: 84 passed, 1 failed |
| Isolated source distribution and wheel build | Pass; wheel built from the source distribution |
| `twine check` on both artifacts | Pass |
| Fresh temporary environment: install wheel, CLI help/version, `pip check` | Pass, with Click 8.5.0 |
| Targeted failure and restore reproductions | Multiple defects confirmed, listed below |
| Tracked-file scan for common private-key/token signatures | No matches; this is not a comprehensive history or credential audit |

Tests ran on macOS. Python 3.12/3.13 runs reused the existing pure-Python test dependencies through `PYTHONPATH`, with plugin autoload disabled; they were not fresh dependency installations. Python 3.14 used the project virtual environment. No live database backup/restore integration was run. XtraBackup and mariabackup are unavailable locally; their process execution was mocked where stated. A local MySQL 8.4 client was used only for parsing synthetic credentials. Other reproductions used temporary directories and synthetic files.

The normal isolated build initially encountered sandbox DNS restrictions; it subsequently succeeded with network access. This was an environment restriction, not a project build defect. Artifacts are in `/tmp/bdbackup-release-audit-dist/`.

## Release blockers

1. **[P1, security] Restore permits path traversal on Python 3.12/3.13.**
   Location: `bdbackup/filebackup.py:241–247`.
   Skipping link/device members does not validate regular-file names. A tar member named `../escaped.txt` wrote outside the restore destination in an actual Python 3.12 run. An absolute member name or an existing destination symlink also requires containment checks. Specify a safe extraction filter explicitly and test traversal and existing symlinks across supported versions. Python 3.12/3.13 default to fully trusted extraction; Python 3.14 changes the default. See [Python extraction filters](https://docs.python.org/3.13/library/tarfile.html#extraction-filters).

2. **[P1] Failed CLI backups report process exit status 0.**
   Location: `bdbackup/cli.py:42–60` and the callers returning its integer.
   Click does not translate a callback's returned integer into a failing process exit in its normal standalone invocation. A file backup whose verification logged “contains no members” exited 0, both through `CliRunner` and a real subprocess. Lock contention is affected by the same implementation. Use Click's explicit exit mechanism or exceptions and test the installed command's actual process status for backup, verification, external-process, and lock failures. Some existing smoke tests pass despite the operation failing.

3. **[P1] Incomplete file backups are reported as successful.**
   Location: `bdbackup/filebackup.py:156–211`.
   Missing template entries are warned about and skipped; exceptions from `tar.add()` are logged and suppressed. With one good file and one missing file, both `backup()` and `verify()` succeeded. A simulated permission error on the second input also passed verification. Directory traversal has no error handler, so unreadable subtrees can also disappear silently. Propagate failures and verify completeness against the selected input set.

4. **[P1] A failed replacement destroys the previous good file backup.**
   Location: `bdbackup/filebackup.py:91–94,178–181`.
   The final destination is opened directly in write mode. Re-running the documented fixed `daily` destination with missing inputs replaced the good archive with an empty archive before verification failed. Write to a unique temporary file, finish and verify it, then atomically replace the destination. Coordinate concurrent writers. Mysqldump also writes directly to final names and uses second-resolution timestamps, so it needs equivalent publication and collision handling.

5. **[P1] File backup/restore loses filesystem content.**
   Location: `bdbackup/filebackup.py:166–175,244–246`.
   Traversal emits files but no directory entries, losing empty directories and directory metadata; directory symlinks are omitted by the default walk. Tar can encode the second name of a hard-linked file as a hardlink member, but restore skips every hardlink. A round trip of two hard-linked filenames restored only one. Define and implement a safe round-trip policy for directories and links, including `--follow-symlinks`, then assert both names and contents survive.

6. **[P1] Physical backup retention runs before verification.**
   Location: `bdbackup/mysql/xtrabackup.py:145–158`; verification occurs later in CLI/config callers.
   A subprocess returning success publishes the directory, updates `Full_Latest`, writes `.full_success`, and prunes old backups before checking checkpoints. With a mocked successful process producing no checkpoints, an old backup was deleted; the subsequent `verify()` raised `BackupError`. Verify the temporary backup before publishing success or deleting previous recovery points. Standalone prune also lacks the backup lock and can race with an incremental operation.

7. **[P1] Incremental identity and ordering fail across days.**
   Location: `bdbackup/mysql/xtrabackup.py:174–197`.
   Incrementals under one full's date directory are named only `HHMMSS` and selected by reverse lexical order. A chain containing an older `230000` and newer next-day `010000` selected `230000`. Repeated daily schedules also collide on the same directory name. Further, selection does not exclude `.tmp` directories or bind incrementals to the current full after another full is taken on the same day. Use unique full timestamps and explicit parent/LSN metadata; accept only completed, verified bases.

8. **[P1] GFS “chain safety” does not retain intermediate incremental dependencies.**
   Location: `bdbackup/retention.py:333–370`.
   Every incremental is attached directly to a full by timestamp, and only that full is pinned. In a full → old incremental → recent incremental chain, the policy retained the full and recent incremental but marked the required old incremental for deletion. This is safe only for differential backups independently based on the full; the project's XtraBackup backend explicitly chains to preceding incrementals. Preserve the transitive dependency chain using backup metadata, or limit and document this retention mode as differential-only. The GFS scanner handles flat files, so it also does not manage XtraBackup's current directory layout without an adapter.

9. **[P1] Mysqldump stderr contaminates the SQL payload.**
   Location: `bdbackup/mysql/mysqldump.py:72–83`.
   `stderr=STDOUT` compresses diagnostic text into the SQL stream. A synthetic mysqldump process emitted valid SQL on stdout and a warning on stderr, exited 0, and produced a verified dump containing the warning. Separate stderr from SQL and preserve diagnostics without risking pipe deadlock. Verification currently also accepts an empty decompressed SQL stream: an empty gzip file passed `verify()`.

10. **[P1] XtraBackup credentials-file argument is in the wrong position.**
    Location: `bdbackup/mysql/xtrabackup.py:73–80,211–215`.
    Both backup and prepare append `--defaults-extra-file` after other arguments. The advertised legacy interface requires it as the first option. This is confirmed by command construction and [Percona's legacy option reference](https://docs.percona.com/percona-xtrabackup/2.4/xtrabackup_bin/xbk_option_reference.html), not a local XtraBackup integration run. Move it immediately after the binary and test against each supported binary/version.

11. **[P1] Credential serialization changes valid passwords.**
    Location: `bdbackup/mysql/helpers.py:28–35`.
    Options are written unquoted and unescaped. Using the local MySQL option parser on a synthetic password `abc#def` returned `abc`. Backslashes, whitespace, and newlines also have option-file semantics. Quote and escape values correctly and reject unsupported control characters. File permissions are correctly restricted, but do not fix serialization. See [MySQL option-file syntax](https://dev.mysql.com/doc/refman/8.0/en/option-files.html).

## Other defects to resolve before advertising the documented features

| Priority | Location | Finding and required correction |
| --- | --- | --- |
| P2 | `bdbackup/cli.py:185–191` and other password options | `mysqldump --database demo -p` exits 2 with “requires an argument”; secure prompting is not implemented. Implement the documented prompt consistently. Supplying a value also places the password in bdbackup's own argv, despite the README's blanket claim about command lines. |
| P2 | `bdbackup/cli.py:229–244` | `--database db1,db2 --jobs 4` calls `backup('db1,db2')` once; `backup_all()` is never called. Parse database lists and dispatch parallel backups with verification for each result. |
| P2 | `bdbackup/cli.py:229–232` | The CLI replaces backend defaults. Normal dumps omit the backend's routines/events options; `--full` also discards `--options` and `--single-transaction`. Define consistent option merging and verify complete, consistent SQL restores. |
| P2 | `bdbackup/cli.py:612` | Restoring into an existing directory fails before extraction: it is passed as `backup_dst`, whose constructor rejects directories. Reproduced as `IsADirectoryError`. Decouple restore from archive-creation initialization. |
| P2 | `bdbackup/filebackup.py:22`; `pyproject.toml:11` | `tar.zst` is offered on Python >=3.12 but depends on Python 3.14 tarfile functionality. Python 3.12 raises `CompressionError("unknown compression type 'zstd'")`. Add compatible support, gate the format clearly, or raise the minimum Python version. See [Python tarfile version changes](https://docs.python.org/3/library/tarfile.html). |
| P2 | `bdbackup/dboperations.py:7,30` | Metadata support cannot import: `setup_logging` no longer exists in `bdbackup.utils`. The old call also expects a logger return and passes a string where the replacement function expects a logging level. Update both the import and logger initialization; add an import/metadata smoke test. |
| P2 | `bdbackup/filebackup.py:37`; `bdbackup/config.py:65–68` | The documented TOML `template_filename = "~/.config/bdbackup/files.template"` remains literal because paths are never expanded. Normalize configuration paths with a defined home/relative-path policy. |
| P2 | `bdbackup/filebackup.py:156–175` | Nested directory exclusions do not prune traversal. A template containing `.` with `exclude_pattern=['node_modules']` still archived `node_modules/secret`. Directory-only custom template patterns have a similar issue unless separate content patterns are supplied. Match ancestor exclusions consistently while preserving the chosen negation semantics. |
| P2 | `bdbackup/mysql/xtrabackup.py:155` | A relative backup root produces a broken `Full_Latest`: the link target includes the relative root again. Reproduced with `backup_root='relative-root'`. Use an absolute target or a target relative to the link's parent. |
| P2 | `bdbackup/mysql/xtrabackup.py:205–219` | `prepare()` invokes only `--prepare`, although backups are compressed by default. The documented prepare recipe lacks decompression; there is no incremental merge workflow. Provide and integration-test the complete recovery procedure. See [Percona decompression and preparation](https://docs.percona.com/percona-xtrabackup/8.0/prepare-compressed-backup.html). |
| P2 | `tests/test_retention.py:28,276–303` | The failed test generates fixtures relative to 2026-07-29, but its config job uses the real clock. It expected 17 survivors and got 7 on the audit date. Freeze or inject the clock. This failure alone does not prove that GFS's normal full-backup tier selection is wrong. |
| P2 | `.github/workflows/publish.yml:15–37` | Publishing independently rebuilds and uploads without running tests or requiring CI success for the published commit. There is no tag/version check. Gate publication on checks for that commit and publish the validated artifacts. |

## Publication readiness and remaining evidence

The repository has an MIT license, README, package metadata, matching `0.4.0` versions, a CLI entry point, and CI/publish workflows. Wheel and source-distribution contents include the application and license. However, `config.toml.example` and `template.txt` are omitted from the source distribution; the README's relative example link is not a substitute for distributing the referenced example.

`git remote -v` returned no configured remotes. The metadata points to `ayder/bdbackup`, but repository access/ownership, the availability of PyPI version `0.4.0`, and publisher account configuration were not verified. Public-page lookups were inconclusive. The intended trusted publisher should match owner `ayder`, repository `bdbackup`, workflow `publish.yml`, and environment `pypi`; this must be registered on PyPI. See [PyPI trusted-publisher configuration](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).

Platform support also needs an explicit statement: `bdbackup/utils.py` imports POSIX-only `fcntl` unconditionally, so the package cannot import on Windows. The universal wheel tag does not establish Windows compatibility. Document POSIX support or implement an alternative lock. Document required database executables and exact supported versions as well.

Release approval requires regression coverage for the blockers, passing tests on supported Python versions, fresh artifact installation checks, and real database restore exercises for each advertised engine/version. Include compressed physical backups, full-to-incremental recovery, midnight transitions, failures before publication, retention of required ancestors, and file restore fidelity. Build and metadata validation already pass, but do not establish backup correctness.
