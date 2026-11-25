"""An SSH peer and transfer tools at the subprocess boundary of copy tests."""

import hashlib
import subprocess
from pathlib import Path


class Remote:
    def __init__(self):
        self.files = {}
        self.calls = []
        self.transfers = []
        self.before = None
        self.after_transfer = None
        self.bad_checksum = False
        self.cleanup_blocked = False

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if self.before is not None:
            answer = self.before(argv)
            if answer is not None:
                return answer
        tool = argv[0]
        if tool in {"rsync", "scp"}:
            path = Path(argv[-2])
            remote = argv[-1].split(":", 1)[1]
            self.transfers.append((tool, path, remote))
            self.files[remote] = path.read_bytes()
            if self.after_transfer is not None:
                self.after_transfer(path)
            return subprocess.CompletedProcess(argv, 0, "", "")

        assert tool == "ssh", argv
        command = argv[-1]
        stdout = ""
        if command.startswith("test -d "):
            final = command.split("sha256sum -- ", 1)[1].split()[0]
            if final in self.files:
                stdout = f"{self.digest(final)}  {final}\n"
        elif command.startswith("test -f "):
            temp = command.split("sha256sum -- ", 1)[1].split()[0]
            digest = "a" * 64 if self.bad_checksum else self.digest(temp)
            stdout = f"{digest}  {temp}\n600\n"
        elif command.startswith("ln -T -- "):
            _, _, _, temp, final, *_ = command.split()
            assert final not in self.files
            self.files[final] = self.files[temp]
        elif command.startswith("rm -f -- "):
            if self.cleanup_blocked:
                return subprocess.CompletedProcess(argv, 1, "", "cannot remove temporary name")
            self.files.pop(command.split()[-1], None)
        else:
            raise AssertionError(f"unexpected SSH command: {command}")
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def digest(self, name):
        return hashlib.sha256(self.files[name]).hexdigest()
