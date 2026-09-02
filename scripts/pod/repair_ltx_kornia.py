#!/usr/bin/env python3
"""Apply the upstream Kornia 0.8.3 compatibility repair to ComfyUI-LTXVideo."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile


DEFAULT_PATH = Path(
    "/workspace/runpod-slim/ComfyUI/custom_nodes/ComfyUI-LTXVideo/pyramid_blending.py"
)


def repair(path: Path) -> dict[str, object]:
    source = path.read_text(encoding="utf-8")
    before_sha256 = hashlib.sha256(source.encode()).hexdigest()
    if "import torch.nn.functional as F" not in source:
        raise RuntimeError("expected torch.nn.functional alias F is missing")

    lines = source.splitlines(keepends=True)
    removed_imports = sum(line.strip() == "pad," for line in lines)
    repaired = "".join(line for line in lines if line.strip() != "pad,")
    repaired, replaced_calls = re.subn(r"(?<![\w.])pad\(", "F.pad(", repaired)

    if "from kornia.geometry.transform.pyramid" not in repaired:
        raise RuntimeError("expected Kornia pyramid import is missing")
    if re.search(r"(?<![\w.])pad\(", repaired):
        raise RuntimeError("an unqualified pad call remains after repair")
    if "F.pad(" not in repaired:
        raise RuntimeError("no repaired F.pad call was found")

    changed = repaired != source
    if changed:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            handle.write(repaired)
            temporary = Path(handle.name)
        temporary.chmod(path.stat().st_mode)
        temporary.replace(path)

    after = path.read_text(encoding="utf-8")
    return {
        "status": "repaired" if changed else "already_repaired",
        "path": str(path),
        "removed_imports": removed_imports,
        "replaced_calls": replaced_calls,
        "before_sha256": before_sha256,
        "after_sha256": hashlib.sha256(after.encode()).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args()
    print(json.dumps(repair(args.path), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
