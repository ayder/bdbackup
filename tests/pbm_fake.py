"""Synthetic PBM storage shared by the local export and CLI tests."""

NAME = "2026-11-25T08:59:27Z"


def make_storage(storage, name=NAME, data=b"backup bytes\x00\x01\xff"):
    files = {
        f"{name}.pbm.json": b'{"status":"done"}',
        f"{name}/rs0/data/collection.s2": data,
        f"{name}/rs0/oplog.s2": b"oplog bytes",
    }
    for path, data in files.items():
        target = storage / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (storage / name / "rs0" / "empty").mkdir()
    return files
