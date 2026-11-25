"""Probe the SSH copy prerequisites with synthetic keys in isolated Linux containers."""

import argparse

FIXTURES = {"22.04": "legacy SCP", "24.04": "SFTP"}
COLLISIONS = ("existing file", "existing directory", "dangling symlink", "after precheck")
UMASKS = (0o000, 0o077)
SOURCE_MODES = (0o600, 0o640)


def expect(check, condition):
    if not condition:
        raise AssertionError(f"{check} failed")


def verify(ubuntu, report):
    versions = report.get("versions", {})
    for program in ("client ssh", "server ssh", "client rsync", "server rsync"):
        expect(f"Ubuntu {ubuntu}: {program} version observed", bool(versions.get(program)))

    names = report["publish"]
    expect("free name: link created and temporary name removed", names["free name"] == (0, True))
    for case in COLLISIONS:
        expect(f"{case}: rejected with source and destination intact", names[case] == (1, True))

    for protocol in ("rsync", FIXTURES[ubuntu]):
        for umask in UMASKS:
            for source in SOURCE_MODES:
                observed, before, after = report["modes"][protocol, umask, source]
                label = f"{protocol} with umask {umask:03o}, source {source:04o}"
                expect(f"{label}: observed protocol", observed == protocol)
                expect(f"{label}: staged permissions capped", after == before & source)


def probe(ubuntu):
    return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", choices=FIXTURES, required=True, metavar="UBUNTU")
    args = parser.parse_args()
    verify(args.probe, probe(args.probe))


if __name__ == "__main__":
    main()
