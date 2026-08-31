#!/usr/bin/env python3
"""Download a pinned LTX model profile into the persistent network volume."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from huggingface_hub import hf_hub_download


def sha256_file(path: Path, chunk_size: int = 16 * 1024 * 1024) -> str:
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
    parser.add_argument(
        "--profile",
        choices=("preview", "final-int8", "final-bf16"),
        required=True,
    )
    parser.add_argument("--root", type=Path, default=Path("/workspace/models"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("/opt/ltx-stack/models-manifest.json"),
    )
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    selected = [
        entry for entry in manifest["files"] if args.profile in entry["profiles"]
    ]
    if not selected:
        raise RuntimeError(f"Manifest has no files for profile {args.profile!r}")

    args.root.mkdir(parents=True, exist_ok=True)
    missing = []
    for entry in selected:
        destination = args.root / entry["path"]
        if is_valid(destination, entry["bytes"], entry["sha256"]):
            print(f"OK      {entry['path']}")
        else:
            missing.append(entry)

    if missing and not os.environ.get("HF_TOKEN"):
        print(
            "HF_TOKEN is required for missing gated LTX-2.5 files. "
            "Configure it as a Runpod secret; never paste it into logs or chat.",
            file=sys.stderr,
        )
        return 2

    for entry in missing:
        print(f"FETCH   {entry['path']}")
        hf_hub_download(
            repo_id=manifest["source_repository"],
            filename=entry["path"],
            token=os.environ.get("HF_TOKEN"),
            local_dir=args.root,
        )
        destination = args.root / entry["path"]
        if not is_valid(destination, entry["bytes"], entry["sha256"]):
            raise RuntimeError(f"Integrity check failed for {entry['path']}")
        print(f"VERIFIED {entry['path']}")

    print(f"Profile {args.profile!r} is complete and verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
