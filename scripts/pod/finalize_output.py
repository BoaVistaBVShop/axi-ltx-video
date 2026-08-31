#!/usr/bin/env python3
"""Validate one completed video and atomically publish it into a job's ready dir."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone


JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def sha256_file(path: Path, chunk_size: int = 16 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def probe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--filename", required=True)
    parser.add_argument("--jobs-root", type=Path, default=Path("/workspace/jobs"))
    args = parser.parse_args()

    if not JOB_ID_RE.fullmatch(args.job_id):
        raise ValueError("Invalid job id")
    if Path(args.filename).name != args.filename:
        raise ValueError("filename must be a basename, not a path")

    job_dir = args.jobs_root / args.job_id
    staging_dir = job_dir / "output-staging"
    ready_dir = job_dir / "ready"
    source = staging_dir / args.filename
    if not source.is_file() or source.stat().st_size <= 0:
        raise FileNotFoundError(f"Missing or empty staged output: {source}")

    metadata = probe(source)
    video_streams = [s for s in metadata.get("streams", []) if s.get("codec_type") == "video"]
    duration = float(metadata.get("format", {}).get("duration") or 0)
    if not video_streams or duration <= 0:
        raise RuntimeError("ffprobe did not find a valid video stream and duration")

    checksum = sha256_file(source)
    size = source.stat().st_size
    ready_dir.mkdir(parents=True, exist_ok=True)
    destination = ready_dir / source.name
    if destination.exists():
        raise FileExistsError(f"Ready output already exists: {destination}")
    os.replace(source, destination)

    report = {
        "schema_version": 1,
        "job_id": args.job_id,
        "filename": destination.name,
        "bytes": size,
        "sha256": checksum,
        "duration_seconds": duration,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "streams": metadata["streams"],
        "format": metadata["format"],
    }
    report_path = ready_dir / f"{destination.name}.qc.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    with (job_dir / "checksums.sha256").open("a", encoding="utf-8") as handle:
        handle.write(f"{checksum}  ready/{destination.name}\n")
    print(json.dumps({"status": "ready", **report}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
