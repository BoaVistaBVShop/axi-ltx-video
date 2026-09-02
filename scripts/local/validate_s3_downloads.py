#!/usr/bin/env python3
"""Validate S3-downloaded Runpod outputs before publishing them locally."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ffprobe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--incoming", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    args.destination.mkdir(parents=True, exist_ok=True)
    records = []
    for video in sorted(args.incoming.glob("*.mp4")):
        qc_path = video.with_name(video.name + ".qc.json")
        if not qc_path.is_file():
            raise RuntimeError(f"missing QC sidecar for {video.name}")
        remote_qc = json.loads(qc_path.read_text(encoding="utf-8"))
        actual_sha256 = sha256(video)
        if actual_sha256 != remote_qc["sha256"]:
            raise RuntimeError(f"SHA-256 mismatch for {video.name}")
        if video.stat().st_size != remote_qc["bytes"]:
            raise RuntimeError(f"size mismatch for {video.name}")

        probe = ffprobe(video)
        duration = float(probe["format"]["duration"])
        if abs(duration - float(remote_qc["duration_seconds"])) > 0.02:
            raise RuntimeError(f"duration mismatch for {video.name}")
        video_stream = next(
            (stream for stream in probe["streams"] if stream.get("codec_type") == "video"),
            None,
        )
        if video_stream is None:
            raise RuntimeError(f"no video stream in {video.name}")

        destination = args.destination / video.name
        destination_qc = args.destination / qc_path.name
        if destination.exists():
            if sha256(destination) != actual_sha256:
                raise RuntimeError(f"refusing to overwrite different output {destination}")
            video.unlink()
        else:
            shutil.move(str(video), destination)
        if destination_qc.exists():
            if destination_qc.read_bytes() != qc_path.read_bytes():
                raise RuntimeError(f"refusing to overwrite different QC file {destination_qc}")
            qc_path.unlink()
        else:
            shutil.move(str(qc_path), destination_qc)

        records.append({
            "filename": video.name,
            "sha256": actual_sha256,
            "bytes": destination.stat().st_size,
            "duration_seconds": duration,
            "video_codec": video_stream.get("codec_name"),
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "fps": video_stream.get("avg_frame_rate"),
            "status": "received",
        })

    report = {"schema_version": 1, "status": "received", "outputs": records}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=args.report.parent, delete=False
    ) as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(args.report)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
