# Release repair plan

Scope: repair the findings in RELEASE_REVIEW.md, preserve Python 3.12+ support,
and validate the resulting package. No publishing is included.

- [x] File safety: atomic verified publication, failure propagation, safe extraction,
  directory/link round trips, exclusions, and version-gated zstd.
- [x] CLI/database safety: real exit codes, secure optional password prompts,
  database lists and defaults, SQL/stderr separation, quoted credentials,
  metadata logger, and config path normalization.
- [x] Physical backups: verified publication before pruning, locking, unique
  identities, explicit parent/LSN metadata, relative links, and recovery copies
  with decompression and ordered incremental preparation.
- [x] Retention and release: preserve prerequisite incrementals, deterministic
  tests, validated configuration, CI-gated publication of tested artifacts,
  tag/version checks, examples, and truthful support documentation.
- [x] Validation: regression tests on Python 3.12/3.13/3.14, lint, isolated build,
  Twine, installed-artifact smoke tests, and update the review with evidence.
- [x] Versioning: use `pyproject.toml` as the sole version source; derive runtime
  and CLI versions from package metadata and validate the release tag against it.

Implementation choices:

- Every built-in backend verifies before publishing, even when a caller skips
  its optional additional verification pass.
- File restore uses an explicit extraction filter on every supported Python.
  Safe relative links are supported; escaping links and unsafe special files fail.
- tar.zst requires Python 3.14; tar and tar.gz remain supported on 3.12+.
- Physical recovery prepares a separate destination copy, preserving the source
  backups and their incremental bases.
- Flat-file GFS assumes one chronological backup stream and conservatively keeps
  intermediate incrementals. XtraBackup manages its own directory chains using
  explicit metadata; the flat-file policy is not advertised for that layout.
- Actual database recovery, GitHub account configuration, and PyPI publisher
  registration require external evidence and will not be claimed from mocks.


Completed validation:

- 141 tests passed on each of Python 3.12, 3.13 and 3.14; 84% statement coverage on 3.14.
- Ruff, whitespace checks, actionlint 1.7.7, and the offline lockfile check passed.
- Source distribution and wheel built successfully and passed Twine. Examples
  and validation scripts are included in the source distribution.
- Fresh wheel installations passed imports, CLI, compressed file recovery and
  failed-replacement preservation on Python 3.12, 3.13 and 3.14.
- Real MySQL 8.4.9: parallel and full dumps, special-character credentials,
  and recovery of data, routines, events and triggers passed.
- Real Percona Server 8.4.10-10 with XtraBackup 8.4.0-6: Zstandard full backup,
  two chained increments, and both full and incremental recovery passed.
- Real MariaDB 11.4.13: full backup, two chained increments, and both recovery
  points passed using the modern checkpoint filename and MariaDB prepare flags.
- Test databases used disposable, network-isolated Docker containers and volumes;
  the test resources were removed after each run.

The same three database recovery jobs now gate the reusable CI workflow and
release artifact build. GitHub-hosted execution and PyPI publisher/account setup
remain external release prerequisites. Nothing was published.
