from __future__ import annotations

import csv
import hashlib
from pathlib import Path


ROOT = Path(__file__).parent


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    with (ROOT / "public_manifest.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    failures = []
    for row in rows:
        path = ROOT / row["path"]
        if not path.is_file():
            failures.append(f"missing: {row['path']}")
        elif path.stat().st_size != int(row["bytes"]):
            failures.append(f"size: {row['path']}")
        elif sha256(path) != row["sha256"]:
            failures.append(f"sha256: {row['path']}")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"verified {len(rows)} files")


if __name__ == "__main__":
    main()
