"""Probe the PBM versions used by the bdbackup integration fixtures.

Build the two images, then run the probes:

    .venv/bin/python scripts/integration_pbm.py --build-probe-images
    .venv/bin/python scripts/integration_pbm.py --probe all

Run one fixture or one case:

    .venv/bin/python scripts/integration_pbm.py --probe mongo6-pbm210
    .venv/bin/python scripts/integration_pbm.py --probe mongo7-pbm217 --case restore-physical
"""

import argparse

FIXTURES = {
    "mongo6-pbm210": ("6.0.30", "2.10.0", "storage"),
    "mongo7-pbm217": ("7.0.48", "2.17.0", "storagePath"),
}
CASES = ("single-secondary", "restore-logical", "restore-physical", "modes", "pitr")


def expect(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"{name} failed" + (f"\n{detail}" if detail else ""))


def source_hash(root=None):
    return ""


def run_probes(fixture, case):
    pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--build-probe-images", action="store_true")
    action.add_argument("--probe", choices=("all", *FIXTURES))
    action.add_argument("--source-hash", action="store_true")
    parser.add_argument("--case", choices=CASES)
    args = parser.parse_args(argv)
    if args.case and not args.probe:
        parser.error("--case requires --probe")
    if args.probe:
        run_probes(args.probe, args.case)
