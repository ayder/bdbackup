"""Probe SSH copy prerequisites with synthetic keys in isolated Linux containers.

Build from the checkout's wheel with --build-probe-images, then run --probe all.
"""

import argparse
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = {"22.04": "legacy SCP", "24.04": "SFTP"}
CASES = (
    "rsync", "scp", "existing-name", "publish-race", "checksum", "unreachable",
    "unknown-host", "refused-key", "session-stop", "session-stall", "killed-transfer",
)
COLLISIONS = ("existing file", "existing directory", "dangling symlink", "after precheck")
# The Ubuntu sshd login sessions can retain these masks.
UMASKS = (0o022, 0o077)
SOURCE_MODES = (0o600, 0o640)
PORT = 22222
USER = "bdprobe"


def source_hash(root=ROOT):
    return ""


def run_integration(case=None, ubuntu=None):
    pass


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

    for method, protocol in (("rsync", "rsync"), ("scp", FIXTURES[ubuntu])):
        for umask in UMASKS:
            for source in SOURCE_MODES:
                observed, before, after = report["modes"][method, umask, source]
                label = f"{protocol} with umask {umask:03o}, source {source:04o}"
                expect(f"{label}: observed protocol", observed == protocol)
                expect(f"{label}: staged permissions capped", after == before & source)


def capture(*argv, check=True):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)  # noqa: S603
    if check and result.returncode:
        raise RuntimeError(f"{argv[0]} exited {result.returncode}: {result.stderr.strip()}")
    return result


def wait_for_server(process, log):
    for _ in range(100):
        if process.poll() is not None:
            break
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"fixture sshd did not start: {log.read_text()}")


@contextmanager
def server(work, umask):
    config = work / "sshd_config"
    config.write_text(
        f"ListenAddress 127.0.0.1\nPort {PORT}\nHostKey {work}/host\n"
        f"PidFile {work}/pid\nAuthorizedKeysFile {work}/authorized_keys\n"
        f"AllowUsers {USER}\nAuthenticationMethods publickey\n"
        "UsePAM no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
        "PermitRootLogin no\nPermitUserRC no\n"
        f"Subsystem sftp internal-sftp -u {umask:03o}\n"
    )
    log = work / "sshd.log"
    with log.open("w") as errors:
        with subprocess.Popen(  # noqa: S603
            ("/usr/sbin/sshd", "-D", "-e", "-f", config), stderr=errors, umask=umask,
        ) as process:
            try:
                wait_for_server(process, log)
                yield
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


def prepare(work):
    work.chmod(0o755)
    for name in ("host", "client"):
        capture("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", work / name)
    shutil.copyfile(work / "client.pub", work / "authorized_keys")
    (work / "authorized_keys").chmod(0o644)
    (work / "known_hosts").write_text(f"[127.0.0.1]:{PORT} {(work / 'host.pub').read_text()}")
    config = work / "client_config"
    config.write_text(
        f"Host probe\n  HostName 127.0.0.1\n  Port {PORT}\n  User {USER}\n"
        f"  IdentityFile {work}/client\n  UserKnownHostsFile {work}/known_hosts\n"
        "  GlobalKnownHostsFile /dev/null\n  IdentityAgent none\n  IdentitiesOnly yes\n"
        "  StrictHostKeyChecking yes\n  BatchMode yes\n  ConnectTimeout 5\n"
    )
    dest = work / "destination"
    dest.mkdir()
    shutil.chown(dest, user=USER)
    return config, dest


def versions(config):
    handshake = capture("ssh", "-v", "-F", config, "probe", "true").stderr
    server_version = next(
        (line.split("remote software version ", 1)[1] for line in handshake.splitlines()
         if "remote software version " in line),
        "",
    )
    return {
        "client ssh": capture("ssh", "-V").stderr.strip(),
        "server ssh": server_version,
        "client rsync": capture("rsync", "--version").stdout.splitlines()[0],
        "server rsync": capture("ssh", "-F", config, "probe", "rsync --version")
        .stdout.splitlines()[0],
    }


def snapshot(path):
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if path.is_symlink():
        payload = path.readlink()
    elif path.is_dir():
        payload = {item.name: snapshot(item) for item in path.iterdir()}
    else:
        payload = path.read_bytes()
    return info.st_ino, info.st_mode, payload


def publish_cases(config, dest):
    results = {}
    for index, case in enumerate(("free name", *COLLISIONS)):
        folder = dest / str(index)
        temp, final = folder / ".bdbackup-tmp-fixture", folder / "final"
        commands = {
            "free name": "true",
            "existing file": f"printf existing > {final}",
            "existing directory": f"mkdir {final} && printf sentinel > {final}/sentinel",
            "dangling symlink": f"ln -s -- {folder}/missing {final}",
            "after precheck": f"test ! -e {final} && test ! -L {final} && printf raced > {final}",
        }
        capture("ssh", "-F", config, "probe", f"mkdir {folder} && printf staged > {temp}")
        capture("ssh", "-F", config, "probe", commands[case])
        staged = snapshot(temp)
        original = None if case == "free name" else snapshot(final)
        link = capture("ssh", "-F", config, "probe", f"ln -T -- {temp} {final}", check=False)
        if case == "free name" and link.returncode == 0:
            capture("ssh", "-F", config, "probe", f"rm -- {temp}")
            correct = snapshot(final) == staged and not temp.exists()
        else:
            correct = snapshot(temp) == staged and snapshot(final) == original
        results[case] = link.returncode, correct
        print(f"ln -T -- {case}: exit={link.returncode} verified={correct}", flush=True)
    return results


def transfer_modes(config, dest, umask):
    result = {}
    for method in ("rsync", "scp"):
        for source in SOURCE_MODES:
            local = config.parent / f"source-{source:04o}"
            local.write_bytes(b"probe archive\n")
            local.chmod(source)
            temp = dest / f".bdbackup-tmp-{method}-{umask:03o}-{source:04o}"
            if method == "rsync":
                capture("rsync", "-e", f"ssh -F {config}", "--", local, f"probe:{temp}")
                observed = "rsync"
            else:
                output = capture("scp", "-v", "-F", config, "--", local, f"probe:{temp}").stderr
                if "Sending subsystem: sftp" in output:
                    observed = "SFTP"
                elif "Sending command: scp " in output:
                    observed = "legacy SCP"
                else:
                    raise RuntimeError(f"could not identify SCP protocol: {output}")
            before = stat.S_IMODE(temp.stat().st_mode)
            if before & ~source:
                capture("ssh", "-F", config, "probe", f"chmod {before & source:04o} {temp}")
            after = stat.S_IMODE(temp.stat().st_mode)
            result[method, umask, source] = observed, before, after
            print(
                f"{observed} umask={umask:03o} source={source:04o} "
                f"remote={before:04o} capped={after:04o}", flush=True,
            )
    return result


def probe(ubuntu):
    with tempfile.TemporaryDirectory(prefix="bdbackup-ssh-", dir="/run") as directory:
        work = Path(directory)
        config, dest = prepare(work)
        report = {"modes": {}}
        for umask in UMASKS:
            with server(work, umask):
                actual = capture("ssh", "-F", config, "probe", "umask").stdout.strip()
                expect(f"remote umask {umask:03o}", int(actual, 8) == umask)
                if umask == UMASKS[0]:
                    report["versions"] = versions(config)
                    print(f"Ubuntu {ubuntu}: {report['versions']}", flush=True)
                    report["publish"] = publish_cases(config, dest)
                report["modes"].update(transfer_modes(config, dest, umask))
        return report


def image(ubuntu):
    return f"bdbackup-ssh-copy:ubuntu-{ubuntu}"


def build_probe_images():
    with tempfile.TemporaryDirectory(prefix="bdbackup-ssh-wheel-") as directory:
        subprocess.run(  # noqa: S603
            (sys.executable, "-m", "build", "--wheel", "--outdir", directory), check=True,
            cwd=ROOT,
        )
        for ubuntu in FIXTURES:
            subprocess.run(  # noqa: S603
                ("docker", "build", "-f", ROOT / "scripts/docker/ssh-copy.Dockerfile",  # noqa: S607
                 "--build-arg", f"UBUNTU={ubuntu}", "-t", image(ubuntu), directory),
                check=True,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--build-probe-images", action="store_true")
    action.add_argument("--probe", choices=("all", *FIXTURES), metavar="UBUNTU")
    action.add_argument("--inside-container", choices=FIXTURES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.build_probe_images:
        build_probe_images()
    elif args.inside_container:
        if not Path("/.dockerenv").is_file():
            parser.error("this probe must run inside its Docker fixture")
        verify(args.inside_container, probe(args.inside_container))
        print(f"Ubuntu {args.inside_container}: SSH prerequisite probe passed")
    else:
        fixtures = FIXTURES if args.probe == "all" else (args.probe,)
        for ubuntu in fixtures:
            subprocess.run(  # noqa: S603
                ("docker", "run", "--rm", "--network", "none", "-i", image(ubuntu),  # noqa: S607
                 "/opt/bdbackup/bin/python", "-", "--inside-container", ubuntu),
                input=Path(__file__).read_bytes(), check=True,
            )


if __name__ == "__main__":
    main()
