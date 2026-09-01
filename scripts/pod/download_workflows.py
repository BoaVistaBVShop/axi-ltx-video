#!/usr/bin/env python3
"""Download pinned ComfyUI workflows into the persistent network volume."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def is_valid(path: Path, expected_bytes: int, expected_sha256: str) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == expected_bytes
        and sha256_file(path) == expected_sha256
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--root", type=Path, default=Path("/workspace/workflows"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("/opt/ltx-stack/workflows-manifest.json"),
    )
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    available_profiles = sorted(
        {
            profile
            for entry in manifest["files"]
            for profile in entry.get("profiles", [])
        }
    )
    if args.profile not in available_profiles:
        parser.error(
            f"unknown profile {args.profile!r}; choose one of: "
            f"{', '.join(available_profiles)}"
        )

    selected = [
        entry for entry in manifest["files"] if args.profile in entry["profiles"]
    ]
    args.root.mkdir(parents=True, exist_ok=True)

    for entry in selected:
        destination = args.root / entry["path"]
        if is_valid(destination, entry["bytes"], entry["sha256"]):
            print(f"OK      {entry['path']}")
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.part")
        request = Request(entry["url"], headers={"User-Agent": "axi-ltx-video/1"})
        print(f"FETCH   {entry['path']}")
        with urlopen(request, timeout=60) as response, temporary.open("wb") as handle:
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)

        if not is_valid(temporary, entry["bytes"], entry["sha256"]):
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"Integrity check failed for {entry['path']}")
        os.replace(temporary, destination)
        print(f"VERIFIED {entry['path']}")

    print(f"Workflow profile {args.profile!r} is complete and verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
