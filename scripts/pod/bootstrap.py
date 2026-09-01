#!/usr/bin/env python3
"""Idempotently prepare and validate one LTX profile on the network volume."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
try:
    import fcntl
except ModuleNotFoundError:  # Allows Windows-side unit tests; the script runs on Linux Pods.
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Iterator


DEFAULT_STACK_ROOT = Path("/opt/ltx-stack")
DEFAULT_WORKSPACE = Path("/workspace")
PASSTHROUGH_ENV = {
    "PATH",
    "HOME",
    "HF_HOME",
    "HUGGINGFACE_HUB_CACHE",
    "TMPDIR",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
}


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def parse_environ_blob(blob: bytes, name: str) -> str | None:
    prefix = name.encode("ascii") + b"="
    for item in blob.split(b"\0"):
        if item.startswith(prefix):
            return item[len(prefix) :].decode("utf-8")
    return None


def secret_from_pid1(name: str, environ_path: Path = Path("/proc/1/environ")) -> str | None:
    try:
        return parse_environ_blob(environ_path.read_bytes(), name)
    except (FileNotFoundError, PermissionError, OSError):
        return None


def resolve_profile(generation_profiles: dict, profile_name: str) -> tuple[str, str]:
    profiles = generation_profiles.get("profiles", {})
    if profile_name not in profiles:
        choices = ", ".join(sorted(profiles))
        raise ValueError(f"unknown generation profile {profile_name!r}; choose one of: {choices}")
    profile = profiles[profile_name]
    model_profile = profile.get("model_profile")
    workflow = profile.get("workflow")
    if not model_profile:
        raise ValueError(f"profile {profile_name!r} has no model_profile")
    if not workflow:
        inherited = profile.get("inherits")
        if not inherited or inherited not in profiles:
            raise ValueError(f"profile {profile_name!r} has no workflow or valid inheritance")
        workflow = profiles[inherited].get("workflow")
    if not workflow:
        raise ValueError(f"profile {profile_name!r} resolves to no workflow")
    return model_profile, workflow


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    if fcntl is None:
        raise RuntimeError("bootstrap locking requires Linux fcntl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_checked(command: list[str], *, env: dict[str, str] | None = None) -> None:
    # Never log the environment: it can contain HF_TOKEN.
    print(f"RUN     {Path(command[0]).name}")
    subprocess.run(command, check=True, env=env)


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def ensure_workspace(workspace: Path, allow_non_mount: bool) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    if not allow_non_mount and not os.path.ismount(workspace):
        raise RuntimeError(f"{workspace} is not a mounted persistent volume")
    probe = workspace / ".axi-ltx" / ".write-probe"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("ok\n", encoding="utf-8")
    probe.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, help="generation profile key")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--stack-root", type=Path, default=DEFAULT_STACK_ROOT)
    parser.add_argument("--allow-non-mount", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-healthcheck", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    ensure_workspace(args.workspace, args.allow_non_mount)
    generation_path = args.stack_root / "generation-profiles.json"
    models_path = args.stack_root / "models-manifest.json"
    workflows_path = args.stack_root / "workflows-manifest.json"
    required = [generation_path, models_path, workflows_path]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing pinned stack files: " + ", ".join(missing))

    generation_profiles = json.loads(generation_path.read_text(encoding="utf-8"))
    model_profile, workflow = resolve_profile(generation_profiles, args.profile)
    state_root = args.workspace / ".axi-ltx"
    marker_path = state_root / "bootstrap" / f"{args.profile}.json"

    with exclusive_lock(state_root / "bootstrap.lock"):
        token = os.environ.get("HF_TOKEN") or secret_from_pid1("HF_TOKEN")
        child_env = {key: value for key, value in os.environ.items() if key in PASSTHROUGH_ENV}
        if token:
            child_env["HF_TOKEN"] = token
        else:
            child_env.pop("HF_TOKEN", None)

        run_checked(
            [
                sys.executable,
                str(args.stack_root / "download_models.py"),
                "--profile",
                model_profile,
                "--root",
                str(args.workspace / "models"),
                "--manifest",
                str(models_path),
            ],
            env=child_env,
        )
        token = None
        child_env.pop("HF_TOKEN", None)
        run_checked(
            [
                sys.executable,
                str(args.stack_root / "download_workflows.py"),
                "--profile",
                model_profile,
                "--root",
                str(args.workspace / "workflows"),
                "--manifest",
                str(workflows_path),
            ],
            env=child_env,
        )
        if not args.skip_healthcheck:
            run_checked(["/usr/local/bin/ltx-healthcheck"], env=child_env)

        marker = {
            "schema_version": 1,
            "status": "ready",
            "generation_profile": args.profile,
            "model_profile": model_profile,
            "workflow": workflow,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "manifests": {
                "models_sha256": sha256_file(models_path),
                "workflows_sha256": sha256_file(workflows_path),
                "generation_profiles_sha256": sha256_file(generation_path),
            },
        }
        write_json_atomic(marker_path, marker)
        print(json.dumps(marker, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
