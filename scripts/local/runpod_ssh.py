#!/usr/bin/env python3
"""SSH-only local control plane for disposable axi-ltx-video Runpod Pods."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_ROOT = PROJECT_ROOT / ".runpod"
POD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
SAFE_REMOTE_PATH_RE = re.compile(r"^/workspace/[A-Za-z0-9._/-]{1,1000}$")
RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,255}$")
GPU_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._():+/-]{2,255}$")
SESSION_ID_RE = re.compile(r"^[a-f0-9]{32}$")
REDACT_KEYS = {"token", "password", "secret", "authorization", "hf_token", "runpod_api_key"}


class OrchestrationError(RuntimeError):
    pass


class CommandError(OrchestrationError):
    def __init__(self, action: str, returncode: int, stderr: str = "") -> None:
        self.action = action
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{action} failed with exit code {returncode}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def redact(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in REDACT_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class AuditLog:
    def __init__(self, pod_id: str | None) -> None:
        name = pod_id or "local"
        self.path = STATE_ROOT / "audit" / f"{name}.jsonl"

    def emit(self, event: str, **fields) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {"timestamp": utc_now(), "event": event, **redact(fields)}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def executable_from_env(name: str, env_name: str) -> str | None:
    configured = os.environ.get(env_name)
    if configured and Path(configured).is_file():
        return configured
    discovered = shutil.which(name)
    if discovered:
        return discovered
    return None


def find_runpodctl() -> str:
    found = executable_from_env("runpodctl", "AXI_RUNPODCTL")
    if found:
        return found
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidate = Path(local_app_data) / "Programs" / "runpodctl" / "runpodctl.exe"
        if candidate.is_file():
            return str(candidate)
    raise OrchestrationError("runpodctl not found; set AXI_RUNPODCTL to its absolute path")


def find_executable(name: str, env_name: str) -> str:
    found = executable_from_env(name, env_name)
    if not found:
        raise OrchestrationError(f"{name} not found; set {env_name} to its absolute path")
    return found


def validate_pod_id(value: str) -> str:
    if not POD_ID_RE.fullmatch(value):
        raise OrchestrationError("invalid pod id")
    return value


def validate_job_id(value: str) -> str:
    if not JOB_ID_RE.fullmatch(value):
        raise OrchestrationError("invalid job id")
    return value


def validate_profile(value: str) -> str:
    if not PROFILE_RE.fullmatch(value):
        raise OrchestrationError("invalid profile name")
    profiles_path = PROJECT_ROOT / "config" / "generation-profiles.json"
    profiles = json.loads(profiles_path.read_text(encoding="utf-8"))["profiles"]
    if value not in profiles:
        raise OrchestrationError(f"profile {value!r} is not configured")
    return value


def validate_basename(value: str) -> str:
    if not SAFE_FILENAME_RE.fullmatch(value) or Path(value).name != value or value in {".", ".."}:
        raise OrchestrationError("filename must be a basename")
    return value


def validate_heartbeat_path(value: str) -> str:
    if not SAFE_REMOTE_PATH_RE.fullmatch(value) or ".." in Path(value).parts:
        raise OrchestrationError("heartbeat path must be an absolute path below /workspace")
    return value


def run_process(
    args: list[str],
    *,
    action: str,
    audit: AuditLog,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    audit.emit("command_started", action=action, executable=Path(args[0]).name)
    result = subprocess.run(
        args,
        input=input_bytes,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        timeout=timeout,
        check=False,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
    )
    audit.emit("command_finished", action=action, returncode=result.returncode)
    if result.returncode != 0:
        stderr = ""
        if capture and result.stderr:
            stderr = result.stderr.decode("utf-8", errors="replace")[-2000:]
        raise CommandError(action, result.returncode, stderr)
    return result


def run_json(args: list[str], *, action: str, audit: AuditLog, timeout: float = 30) -> dict:
    result = run_process(args, action=action, audit=audit, timeout=timeout)
    try:
        return json.loads(result.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OrchestrationError(f"{action} returned invalid JSON") from exc


@dataclass(frozen=True)
class SshInfo:
    host: str
    port: int
    user: str
    key_path: Path

    @classmethod
    def from_payload(cls, payload: dict) -> "SshInfo":
        host = payload.get("ip") or payload.get("host") or payload.get("hostname")
        port = payload.get("port") or payload.get("ssh_port") or payload.get("sshPort")
        key = payload.get("ssh_key") or payload.get("sshKey") or {}
        key_path = key.get("path") if isinstance(key, dict) else None
        key_path = key_path or payload.get("key_path") or payload.get("keyPath")
        user = payload.get("user") or payload.get("username") or "root"
        if not isinstance(host, str) or not SAFE_HOST_RE.fullmatch(host):
            raise OrchestrationError("ssh info contains an invalid host")
        try:
            parsed_port = int(port)
        except (TypeError, ValueError) as exc:
            raise OrchestrationError("ssh info contains an invalid port") from exc
        if parsed_port < 1 or parsed_port > 65535:
            raise OrchestrationError("ssh info port is out of range")
        if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,31}", user):
            raise OrchestrationError("ssh info contains an invalid user")
        if not isinstance(key_path, str) or not key_path:
            raise OrchestrationError("ssh info did not provide an identity file")
        path = Path(key_path).expanduser()
        if not path.is_file():
            raise OrchestrationError(f"ssh identity file does not exist: {path}")
        return cls(host=host, port=parsed_port, user=user, key_path=path)


class PodSsh:
    def __init__(self, pod_id: str) -> None:
        self.pod_id = validate_pod_id(pod_id)
        self.audit = AuditLog(self.pod_id)
        self.runpodctl = find_runpodctl()
        self.ssh = find_executable("ssh", "AXI_SSH")
        self.ssh_keyscan = find_executable("ssh-keyscan", "AXI_SSH_KEYSCAN")
        self.known_hosts = STATE_ROOT / "known_hosts" / self.pod_id
        self.host_metadata = self.known_hosts.with_suffix(".json")

    def resolve(self) -> SshInfo:
        payload = run_json(
            [self.runpodctl, "ssh", "info", self.pod_id],
            action="ssh_info",
            audit=self.audit,
        )
        info = SshInfo.from_payload(payload)
        self.audit.emit("ssh_info_resolved", host=info.host, port=info.port, user=info.user)
        return info

    def ensure_known_host(self, info: SshInfo) -> None:
        expected = {"host": info.host, "port": info.port}
        if self.known_hosts.is_file() and self.host_metadata.is_file():
            current = json.loads(self.host_metadata.read_text(encoding="utf-8"))
            if current != expected:
                raise OrchestrationError(
                    "SSH endpoint changed for this pod id; remove its isolated known-hosts "
                    "record only after verifying the new endpoint"
                )
            return
        try:
            result = run_process(
                [self.ssh_keyscan, "-T", "10", "-p", str(info.port), info.host],
                action="ssh_keyscan",
                audit=self.audit,
                timeout=15,
            )
        except (CommandError, subprocess.TimeoutExpired):
            if os.name != "nt":
                raise
            wsl = find_executable("wsl", "AXI_WSL")
            result = run_process(
                [wsl, "-d", "Ubuntu", "--", "ssh-keyscan", "-T", "10", "-p",
                 str(info.port), info.host],
                action="ssh_keyscan_wsl_fallback",
                audit=self.audit,
                timeout=20,
            )
        lines = [
            line for line in result.stdout.decode("utf-8", errors="strict").splitlines()
            if line and not line.startswith("#")
        ]
        if not lines:
            raise OrchestrationError("ssh-keyscan returned no host keys")
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.known_hosts.with_suffix(".tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, self.known_hosts)
        write_json_atomic(self.host_metadata, expected)
        self.audit.emit("ssh_host_key_recorded", algorithms=len(lines))

    def base_args(self, info: SshInfo) -> list[str]:
        return [
            self.ssh,
            "-i", str(info.key_path),
            "-p", str(info.port),
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f'UserKnownHostsFile="{self.known_hosts}"',
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
        ]

    @staticmethod
    def destination(info: SshInfo) -> str:
        return f"{info.user}@{info.host}"

    def run(self, info: SshInfo, remote_command: str, *, action: str, capture: bool = True,
            timeout: float | None = None) -> subprocess.CompletedProcess:
        self.ensure_known_host(info)
        return run_process(
            [*self.base_args(info), self.destination(info), remote_command],
            action=action,
            audit=self.audit,
            capture=capture,
            timeout=timeout,
        )

    def upload(self, info: SshInfo, local_path: Path, remote_path: str) -> None:
        """Upload one validated file through the same isolated SSH trust boundary."""
        remote_path = validate_heartbeat_path(remote_path)
        if not local_path.is_file():
            raise OrchestrationError(f"local upload file not found: {local_path}")
        self.ensure_known_host(info)
        scp = find_executable("scp", "AXI_SCP")
        command = [
            scp,
            "-i", str(info.key_path),
            "-P", str(info.port),
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f'UserKnownHostsFile="{self.known_hosts}"',
            "-o", "ConnectTimeout=10",
            str(local_path),
            f"{self.destination(info)}:{remote_path}",
        ]
        run_process(command, action="ssh_upload", audit=self.audit, timeout=600)
        self.audit.emit(
            "ssh_upload_completed",
            local_name=local_path.name,
            remote_path=remote_path,
            bytes=local_path.stat().st_size,
        )


def choose_local_port(requested: int) -> int:
    if requested:
        if requested < 1024 or requested > 65535:
            raise OrchestrationError("local port must be between 1024 and 65535")
        return requested
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


@contextmanager
def ssh_tunnel(pod: PodSsh, info: SshInfo, local_port: int) -> Iterator[int]:
    pod.ensure_known_host(info)
    local_port = choose_local_port(local_port)
    log_path = STATE_ROOT / "tunnels" / f"{pod.pod_id}-{local_port}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            [
                *pod.base_args(info),
                "-N",
                "-o", "ExitOnForwardFailure=yes",
                "-L", f"127.0.0.1:{local_port}:127.0.0.1:8188",
                pod.destination(info),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_handle,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
        )
        pod.audit.emit("tunnel_started", local_port=local_port, pid=process.pid)
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise OrchestrationError(f"SSH tunnel exited early; inspect {log_path}")
                try:
                    with socket.create_connection(("127.0.0.1", local_port), timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.25)
            else:
                raise OrchestrationError("SSH tunnel did not open its local port")
            yield local_port
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            pod.audit.emit("tunnel_stopped", local_port=local_port, returncode=process.returncode)


def http_json(method: str, url: str, payload: dict | None = None, timeout: float = 10) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, method=method)
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-1000:]
        raise OrchestrationError(f"ComfyUI HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise OrchestrationError(f"ComfyUI request failed: {type(exc).__name__}") from exc


def wait_for_ssh(
    pod: PodSsh,
    timeout_seconds: int,
    heartbeat: Callable[[], None] | None = None,
) -> SshInfo:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if heartbeat:
            heartbeat()
        try:
            info = pod.resolve()
            pod.run(
                info,
                "bash -lc 'set -euo pipefail; test -d /workspace; "
                "test -x /opt/ltx-stack/download_models.py; nvidia-smi -L >/dev/null'",
                action="ssh_probe",
                timeout=20,
            )
            return info
        except (OrchestrationError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            if heartbeat:
                heartbeat()
            time.sleep(5)
    raise OrchestrationError(f"SSH readiness timed out: {last_error}")


def verify_comfyui(pod: PodSsh, info: SshInfo, timeout_seconds: int, local_port: int = 0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    with ssh_tunnel(pod, info, local_port) as port:
        while time.monotonic() < deadline:
            try:
                stats = http_json("GET", f"http://127.0.0.1:{port}/system_stats", timeout=5)
                pod.audit.emit("comfyui_ready", local_port=port)
                return stats
            except OrchestrationError as exc:
                last_error = exc
                time.sleep(3)
    raise OrchestrationError(f"ComfyUI readiness timed out: {last_error}")


def command_doctor(_args: argparse.Namespace) -> int:
    audit = AuditLog(None)
    stack = json.loads((PROJECT_ROOT / "config" / "stack.json").read_text(encoding="utf-8"))
    runpodctl = find_runpodctl()
    run_process([runpodctl, "user"], action="runpod_auth_check", audit=audit, timeout=30)
    checks = {
        "runpodctl": runpodctl,
        "runpod_authenticated": True,
        "ssh": find_executable("ssh", "AXI_SSH"),
        "ssh_keyscan": find_executable("ssh-keyscan", "AXI_SSH_KEYSCAN"),
        "image": stack["image"]["published"],
        "registry_public": stack["image"].get("registry_visibility") == "PUBLIC",
        "ssh_only": stack["runpod"]["access"]["pod_operations"] == "SSH_ONLY",
    }
    audit.emit("doctor_completed", checks=checks)
    print(json.dumps(checks, indent=2))
    return 0


def command_readiness(args: argparse.Namespace) -> int:
    pod = PodSsh(args.pod_id)
    info = wait_for_ssh(pod, args.ssh_timeout)
    stats = verify_comfyui(pod, info, args.comfy_timeout, args.local_port)
    print(json.dumps({"status": "ready", "pod_id": pod.pod_id, "system_stats": stats}, indent=2))
    return 0


def command_bootstrap(args: argparse.Namespace) -> int:
    profile = validate_profile(args.profile)
    pod = PodSsh(args.pod_id)
    info = wait_for_ssh(pod, args.ssh_timeout)
    command = f"python3.12 /opt/ltx-stack/bootstrap.py --profile {profile}"
    pod.run(info, command, action="bootstrap", capture=False, timeout=args.bootstrap_timeout)
    stats = verify_comfyui(pod, info, args.comfy_timeout, args.local_port)
    print(json.dumps({"status": "bootstrapped", "pod_id": pod.pod_id,
                      "profile": profile, "system_stats": stats}, indent=2))
    return 0


def command_tunnel(args: argparse.Namespace) -> int:
    pod = PodSsh(args.pod_id)
    info = wait_for_ssh(pod, args.ssh_timeout)
    with ssh_tunnel(pod, info, args.local_port) as port:
        print(json.dumps({"status": "tunnel_ready", "url": f"http://127.0.0.1:{port}"}))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0


def load_prompt_payload(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    prompt = payload.get("prompt", payload)
    if not isinstance(prompt, dict) or not prompt:
        raise OrchestrationError("prompt payload must contain a non-empty JSON object")
    request_payload = {"prompt": prompt}
    if isinstance(payload.get("extra_data"), dict):
        request_payload["extra_data"] = payload["extra_data"]
    request_payload["client_id"] = str(uuid.uuid4())
    return request_payload


def poll_history(base_url: str, prompt_id: str, timeout_seconds: int,
                 state_path: Path, state: dict) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        history = http_json("GET", f"{base_url}/history/{prompt_id}", timeout=10)
        record = history.get(prompt_id)
        if isinstance(record, dict):
            status = record.get("status", {})
            if status.get("status_str") == "error":
                state.update({"status": "error", "failed_at": utc_now()})
                write_json_atomic(state_path, state)
                raise OrchestrationError(f"generation failed in ComfyUI: {prompt_id}")
            if status.get("completed") is True:
                state.update({"status": "completed", "completed_at": utc_now()})
                write_json_atomic(state_path, state)
                return record
        time.sleep(3)
    state.update({"status": "submitted_wait_timeout", "last_checked_at": utc_now()})
    write_json_atomic(state_path, state)
    raise OrchestrationError(
        f"generation is still running; resume the same job id instead of submitting again: {prompt_id}"
    )


def command_submit(args: argparse.Namespace) -> int:
    job_id = validate_job_id(args.job_id)
    prompt_path = Path(args.prompt_json).resolve()
    if not prompt_path.is_file():
        raise OrchestrationError(f"prompt JSON not found: {prompt_path}")
    pod = PodSsh(args.pod_id)
    state_path = STATE_ROOT / "jobs" / f"{job_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else None
    if state and state.get("pod_id") != pod.pod_id:
        raise OrchestrationError("job id is already bound to another pod")
    if state and state.get("status") == "completed":
        print(json.dumps(state, indent=2))
        return 0

    info = wait_for_ssh(pod, args.ssh_timeout)
    telemetry = GpuTelemetry(pod, info, job_id, args.metrics_interval)
    telemetry.start()
    try:
        with ssh_tunnel(pod, info, args.local_port) as port:
            base_url = f"http://127.0.0.1:{port}"
            if state and state.get("prompt_id"):
                prompt_id = state["prompt_id"]
                pod.audit.emit("generation_resumed", job_id=job_id, prompt_id=prompt_id)
            else:
                response = http_json(
                    "POST", f"{base_url}/prompt", load_prompt_payload(prompt_path), timeout=30
                )
                prompt_id = response.get("prompt_id")
                if not isinstance(prompt_id, str) or not prompt_id:
                    raise OrchestrationError("ComfyUI did not return a prompt_id")
                state = {
                    "schema_version": 1,
                    "job_id": job_id,
                    "pod_id": pod.pod_id,
                    "prompt_id": prompt_id,
                    "status": "submitted",
                    "submitted_at": utc_now(),
                }
                write_json_atomic(state_path, state)
                pod.audit.emit("generation_submitted", job_id=job_id, prompt_id=prompt_id)
            record = poll_history(base_url, prompt_id, args.wait_timeout, state_path, state)
    finally:
        telemetry.stop()
    print(json.dumps({"status": "completed", "job_id": job_id,
                      "prompt_id": prompt_id, "outputs": record.get("outputs", {})}, indent=2))
    return 0


def command_finalize(args: argparse.Namespace) -> int:
    job_id = validate_job_id(args.job_id)
    filename = validate_basename(args.filename)
    pod = PodSsh(args.pod_id)
    info = wait_for_ssh(pod, args.ssh_timeout)
    command = (
        f"python3.12 /opt/ltx-stack/finalize_output.py --job-id {job_id} "
        f"--filename {filename}"
    )
    result = pod.run(info, command, action="finalize_output", timeout=args.timeout)
    sys.stdout.buffer.write(result.stdout)
    return 0


GPU_METRIC_FIELDS = [
    "timestamp", "name", "utilization.gpu", "utilization.memory",
    "memory.used", "memory.total", "temperature.gpu", "power.draw", "power.limit",
]


def query_gpu_metrics(pod: PodSsh, info: SshInfo, *, action: str, timeout: int = 60) -> list[dict]:
    command = (
        "nvidia-smi --query-gpu=" + ",".join(GPU_METRIC_FIELDS)
        + " --format=csv,noheader,nounits"
    )
    result = pod.run(info, command, action=action, timeout=timeout)
    rows = []
    for line in result.stdout.decode("utf-8", errors="strict").splitlines():
        values = [value.strip() for value in line.split(",")]
        if len(values) != len(GPU_METRIC_FIELDS):
            raise OrchestrationError("nvidia-smi returned an unexpected metrics row")
        rows.append(dict(zip(GPU_METRIC_FIELDS, values, strict=True)))
    return rows


def metric_summary(samples: list[dict]) -> dict:
    numeric_fields = [
        "utilization.gpu", "utilization.memory", "memory.used", "memory.total",
        "temperature.gpu", "power.draw", "power.limit",
    ]
    summary: dict[str, dict[str, float]] = {}
    for field in numeric_fields:
        values = []
        for sample in samples:
            try:
                values.append(float(sample[field]))
            except (KeyError, TypeError, ValueError):
                continue
        if values:
            summary[field] = {
                "min": round(min(values), 3),
                "max": round(max(values), 3),
                "mean": round(sum(values) / len(values), 3),
            }
    memory_percent = []
    for sample in samples:
        try:
            used = float(sample["memory.used"])
            total = float(sample["memory.total"])
            if total > 0:
                memory_percent.append(used * 100.0 / total)
        except (KeyError, TypeError, ValueError):
            continue
    if memory_percent:
        summary["memory.used_percent"] = {
            "min": round(min(memory_percent), 3),
            "max": round(max(memory_percent), 3),
            "mean": round(sum(memory_percent) / len(memory_percent), 3),
        }
    return summary


class GpuTelemetry:
    """Continuously sample GPU usage for exactly one submitted job."""

    def __init__(self, pod: PodSsh, info: SshInfo, job_id: str, interval_seconds: int) -> None:
        if interval_seconds < 2 or interval_seconds > 60:
            raise OrchestrationError("metrics interval must be between 2 and 60 seconds")
        self.pod = pod
        self.info = info
        self.job_id = validate_job_id(job_id)
        self.interval_seconds = interval_seconds
        self.started_at = utc_now()
        self.samples: list[dict] = []
        self.errors: list[str] = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"gpu-{self.job_id}", daemon=True)
        self.path = STATE_ROOT / "metrics" / f"{self.job_id}.json"

    def start(self) -> None:
        self.thread.start()
        self.pod.audit.emit(
            "gpu_telemetry_started", job_id=self.job_id, interval_seconds=self.interval_seconds
        )

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                rows = query_gpu_metrics(
                    self.pod, self.info, action="gpu_metrics_sample", timeout=30
                )
                observed_at = utc_now()
                for row in rows:
                    row["observed_at"] = observed_at
                    self.samples.append(row)
            except (OrchestrationError, subprocess.TimeoutExpired) as exc:
                self.errors.append(type(exc).__name__)
            self.stop_event.wait(self.interval_seconds)

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=35)
        payload = {
            "schema_version": 1,
            "job_id": self.job_id,
            "pod_id": self.pod.pod_id,
            "started_at": self.started_at,
            "stopped_at": utc_now(),
            "interval_seconds": self.interval_seconds,
            "sample_count": len(self.samples),
            "summary": metric_summary(self.samples),
            "samples": self.samples,
            "sampling_errors": self.errors,
        }
        write_json_atomic(self.path, payload)
        self.pod.audit.emit(
            "gpu_telemetry_stopped",
            job_id=self.job_id,
            sample_count=len(self.samples),
            report=str(self.path),
        )


def command_upload_input(args: argparse.Namespace) -> int:
    local_path = Path(args.local_file).resolve()
    remote_name = validate_basename(args.remote_name or local_path.name)
    pod = PodSsh(args.pod_id)
    info = wait_for_ssh(pod, args.ssh_timeout)
    remote_root = "/workspace/runpod-slim/ComfyUI/input"
    pod.run(
        info,
        f"bash -lc 'set -euo pipefail; mkdir -p -- {remote_root}'",
        action="prepare_input_directory",
        timeout=30,
    )
    remote_path = f"{remote_root}/{remote_name}"
    pod.upload(info, local_path, remote_path)
    print(json.dumps({
        "status": "uploaded",
        "pod_id": pod.pod_id,
        "remote_name": remote_name,
        "bytes": local_path.stat().st_size,
    }, indent=2))
    return 0


def command_metrics(args: argparse.Namespace) -> int:
    """Read a bounded GPU utilization snapshot over SSH without exposing secrets."""
    pod = PodSsh(args.pod_id)
    info = wait_for_ssh(pod, args.ssh_timeout)
    rows = query_gpu_metrics(pod, info, action="gpu_metrics", timeout=args.timeout)
    print(json.dumps({"status": "ok", "gpus": rows}, indent=2))
    return 0


def command_ltx_runtime_check(args: argparse.Namespace) -> int:
    """Report the pinned LTX node source and its critical Kornia compatibility surface."""
    pod = PodSsh(args.pod_id)
    info = wait_for_ssh(pod, args.ssh_timeout)
    command = (
        "bash -lc 'set -euo pipefail; "
        "test -f /workspace/runpod-slim/ComfyUI/custom_nodes/ComfyUI-LTXVideo/iclora.py; "
        "grep -q LTXICLoRALoaderModelOnly "
        "/workspace/runpod-slim/ComfyUI/custom_nodes/ComfyUI-LTXVideo/iclora.py; "
        "python3.12 -c \"import json,kornia; "
        "from kornia.geometry.transform import pyramid; "
        "print(json.dumps({\\\"kornia_version\\\":kornia.__version__,"
        "\\\"pyramid_has_pad\\\":hasattr(pyramid,\\\"pad\\\")}))\"'"
    )
    result = pod.run(info, command, action="ltx_runtime_check", timeout=args.timeout)
    sys.stdout.buffer.write(result.stdout)
    return 0


def command_repair_ltx_kornia(args: argparse.Namespace) -> int:
    """Apply the versioned upstream compatibility repair to the running Pod over SSH."""
    pod = PodSsh(args.pod_id)
    info = wait_for_ssh(pod, args.ssh_timeout)
    repair_script = PROJECT_ROOT / "scripts" / "pod" / "repair_ltx_kornia.py"
    encoded = base64.b64encode(repair_script.read_bytes()).decode("ascii")
    remote_path = (
        "/workspace/runpod-slim/ComfyUI/custom_nodes/"
        "ComfyUI-LTXVideo/pyramid_blending.py"
    )
    command = (
        "python3.12 -c \"import base64;"
        f"exec(compile(base64.b64decode('{encoded}'),'<repair_ltx_kornia>','exec'))\" "
        f"--path {remote_path}"
    )
    result = pod.run(info, command, action="repair_ltx_kornia", timeout=args.timeout)
    sys.stdout.buffer.write(result.stdout)
    return 0


def command_stage_output(args: argparse.Namespace) -> int:
    """Move one validated ComfyUI output into a job's persistent staging area."""
    job_id = validate_job_id(args.job_id)
    source_filename = validate_basename(args.source_filename)
    target_filename = validate_basename(args.target_filename)
    pod = PodSsh(args.pod_id)
    info = wait_for_ssh(pod, args.ssh_timeout)
    source_primary = f"/workspace/runpod-slim/ComfyUI/output/{job_id}/{source_filename}"
    source_fallback = f"/workspace/ComfyUI/output/{job_id}/{source_filename}"
    destination_dir = f"/workspace/jobs/{job_id}/output-staging"
    destination = f"{destination_dir}/{target_filename}"
    command = (
        "bash -lc 'set -euo pipefail; "
        f"src={source_primary}; src_fallback={source_fallback}; "
        f"dst_dir={destination_dir}; dst={destination}; "
        "if test ! -f \"$src\" && test -f \"$src_fallback\"; then src=$src_fallback; fi; "
        "if test -f \"$dst\"; then test ! -e \"$src\"; exit 0; fi; "
        "test -f \"$src\"; mkdir -p \"$dst_dir\"; mv -- \"$src\" \"$dst\"'"
    )
    pod.run(info, command, action="stage_output", timeout=args.timeout)
    print(json.dumps({"status": "staged", "job_id": job_id,
                      "filename": target_filename}, indent=2))
    return 0


def delete_pod(pod_id: str, *, audit: AuditLog, retries: int = 5) -> None:
    runpodctl = find_runpodctl()
    delay = 2
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            run_process(
                [runpodctl, "pod", "delete", pod_id],
                action="pod_delete",
                audit=audit,
                timeout=30,
            )
            audit.emit("pod_delete_requested", attempt=attempt)
            return
        except (CommandError, subprocess.TimeoutExpired) as exc:
            if isinstance(exc, CommandError):
                try:
                    error_payload = json.loads(exc.stderr)
                except json.JSONDecodeError:
                    error_payload = {}
                if error_payload.get("code") == "not_found":
                    audit.emit("pod_already_absent")
                    return
            last_error = exc
            audit.emit("pod_delete_retry", attempt=attempt, error=type(exc).__name__)
            if attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 20)
    raise OrchestrationError(f"failed to delete pod after {retries} attempts: {last_error}")


def validate_resource_id(value: str, label: str) -> str:
    if not RESOURCE_ID_RE.fullmatch(value):
        raise OrchestrationError(f"invalid {label}")
    return value


def validate_gpu_id(value: str) -> str:
    if not GPU_ID_RE.fullmatch(value):
        raise OrchestrationError("invalid gpu id")
    return value


def validate_session_id(value: str) -> str:
    if not SESSION_ID_RE.fullmatch(value):
        raise OrchestrationError("invalid session id")
    return value


def read_json_object(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OrchestrationError(f"could not read {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise OrchestrationError(f"{label} must contain a JSON object")
    return payload


@contextmanager
def file_lock(path: Path, timeout_seconds: float = 10) -> Iterator[None]:
    """Use an OS-released advisory lock so a crashed process cannot strand the guardian."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    with lock_path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise OrchestrationError(f"timed out waiting for state lock: {path.name}")
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def update_json_locked(path: Path, mutator: Callable[[dict], None]) -> dict:
    with file_lock(path):
        state = read_json_object(path, "state")
        mutator(state)
        write_json_atomic(path, state)
        return state


def resolve_state_file(value: str, category: str) -> Path:
    root = (STATE_ROOT / category).resolve()
    path = Path(value).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise OrchestrationError(f"file must be below {root}") from exc
    if path.suffix.lower() != ".json":
        raise OrchestrationError("state file must use the .json extension")
    return path


def authorization_constraints(
    path: Path,
    *,
    template_id: str,
    network_volume_id: str,
    gpu_id: str,
    data_center_id: str,
    deadline: datetime,
    hourly_usd: float,
) -> dict:
    authorization = read_json_object(path, "billable authorization")
    if authorization.get("schema_version") != 1 or authorization.get("authorized") is not True:
        raise OrchestrationError("billable authorization is not an approved schema-v1 record")
    if authorization.get("one_time") is not True:
        raise OrchestrationError("billable authorization must be one-time")
    if authorization.get("consumed_at") or authorization.get("consumed_by_session"):
        raise OrchestrationError("billable authorization has already been consumed")
    expires_at = parse_deadline(str(authorization.get("expires_at", "")))
    if expires_at <= datetime.now(timezone.utc):
        raise OrchestrationError("billable authorization has expired")
    constraints = authorization.get("constraints")
    if not isinstance(constraints, dict):
        raise OrchestrationError("billable authorization has no constraints object")
    expected = {
        "template_id": template_id,
        "network_volume_id": network_volume_id,
        "gpu_id": gpu_id,
        "data_center_id": data_center_id,
        "cloud": "SECURE",
    }
    for key, value in expected.items():
        if constraints.get(key) != value:
            raise OrchestrationError(f"billable authorization mismatch: {key}")
    approved_deadline = parse_deadline(str(constraints.get("deadline", "")))
    if deadline != approved_deadline:
        raise OrchestrationError("billable authorization mismatch: deadline")
    try:
        max_hourly = float(constraints["max_hourly_usd"])
        max_total = float(constraints["max_total_usd"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OrchestrationError("billable authorization has invalid cost limits") from exc
    if max_hourly <= 0 or max_total <= 0 or hourly_usd <= 0:
        raise OrchestrationError("billable cost limits must be positive")
    if hourly_usd > max_hourly:
        raise OrchestrationError("requested hourly price exceeds the approved ceiling")
    projected_ceiling = hourly_usd * max(
        0.0, (deadline - datetime.now(timezone.utc)).total_seconds() / 3600
    )
    if projected_ceiling > max_total:
        raise OrchestrationError("deadline would exceed the approved total cost ceiling")
    return authorization


def consume_authorization(path: Path, session_id: str, expected: dict) -> None:
    def mutate(authorization: dict) -> None:
        if authorization != expected:
            raise OrchestrationError("billable authorization changed after validation")
        if authorization.get("consumed_at") or authorization.get("consumed_by_session"):
            raise OrchestrationError("billable authorization was consumed concurrently")
        authorization["consumed_at"] = utc_now()
        authorization["consumed_by_session"] = session_id

    update_json_locked(path, mutate)


def iter_pod_records(payload) -> Iterator[dict]:
    if isinstance(payload, list):
        for item in payload:
            yield from iter_pod_records(item)
    elif isinstance(payload, dict):
        if any(key in payload for key in ("id", "podId", "pod_id")):
            yield payload
        for key in ("pod", "pods", "items", "data", "results"):
            if key in payload:
                yield from iter_pod_records(payload[key])


def record_pod_id(record: dict) -> str | None:
    value = record.get("id") or record.get("podId") or record.get("pod_id")
    if isinstance(value, str) and POD_ID_RE.fullmatch(value):
        return value
    return None


def pod_id_from_create(payload: dict) -> str | None:
    ids = {pod_id for record in iter_pod_records(payload) if (pod_id := record_pod_id(record))}
    if len(ids) > 1:
        raise OrchestrationError("pod create returned more than one pod id")
    return next(iter(ids), None)


def discover_owned_pod_ids(name: str, *, runpodctl: str, audit: AuditLog) -> list[str]:
    payload = run_json(
        [runpodctl, "pod", "list", "--all", "--name", name],
        action="guard_discover_pods",
        audit=audit,
        timeout=30,
    )
    owned: set[str] = set()
    for record in iter_pod_records(payload):
        if record.get("name") == name:
            pod_id = record_pod_id(record)
            if pod_id:
                owned.add(pod_id)
    return sorted(owned)


def live_gpu_preflight(
    gpu_id: str,
    data_center_id: str,
    expected_hourly_usd: float,
    *,
    runpodctl: str,
    audit: AuditLog,
) -> dict:
    payload = run_json(
        [runpodctl, "gpu", "list", "--include-unavailable"],
        action="live_gpu_preflight",
        audit=audit,
        timeout=60,
    )
    records = payload if isinstance(payload, list) else payload.get("gpus", payload.get("data", []))
    if not isinstance(records, list):
        raise OrchestrationError("live GPU catalog returned an unexpected shape")
    matches = [record for record in records if isinstance(record, dict)
               and (record.get("gpuId") or record.get("id")) == gpu_id]
    if len(matches) != 1:
        raise OrchestrationError("approved GPU was not uniquely found in the live catalog")
    gpu = matches[0]
    if gpu.get("secureCloud") is not True:
        raise OrchestrationError("approved GPU is not available in Secure Cloud")
    try:
        live_price = float(gpu["securePricePerHr"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OrchestrationError("live Secure Cloud GPU price is unavailable") from exc
    if abs(live_price - expected_hourly_usd) > 0.0001:
        raise OrchestrationError(
            f"live hourly price changed to US$ {live_price:.4f}; new approval is required"
        )
    availability = gpu.get("dataCenterAvailability")
    if not isinstance(availability, list):
        raise OrchestrationError("live GPU catalog has no data-center availability")
    locations = [item for item in availability if isinstance(item, dict)
                 and item.get("dataCenterId") == data_center_id]
    if len(locations) != 1 or str(locations[0].get("stockStatus", "")).lower() == "none":
        raise OrchestrationError("approved GPU currently has no stock in the volume data center")
    return {"gpu_id": gpu_id, "data_center_id": data_center_id,
            "secure_hourly_usd": live_price, "stock_status": locations[0].get("stockStatus")}


def unwrap_record(payload, keys: tuple[str, ...], label: str) -> dict:
    record = payload
    if isinstance(record, dict):
        for key in keys:
            if isinstance(record.get(key), dict):
                record = record[key]
                break
    if not isinstance(record, dict):
        raise OrchestrationError(f"live {label} returned an unexpected shape")
    return record


def live_resource_preflight(
    template_id: str,
    network_volume_id: str,
    data_center_id: str,
    *,
    runpodctl: str,
    audit: AuditLog,
) -> dict:
    stack = read_json_object(PROJECT_ROOT / "config" / "stack.json", "stack config")
    template = unwrap_record(
        run_json([runpodctl, "template", "get", template_id],
                 action="live_template_preflight", audit=audit, timeout=30),
        ("template", "data"),
        "template",
    )
    returned_template_id = template.get("id") or template.get("templateId")
    if returned_template_id != template_id:
        raise OrchestrationError("live template id does not match the approved template")
    image = template.get("imageName") or template.get("image") or template.get("containerImage")
    pinned_image = stack["image"]["published"]
    if image != pinned_image:
        raise OrchestrationError("live template is not pinned to the approved image digest")
    disk = template.get("containerDiskInGb", template.get("containerDiskGb"))
    try:
        disk_gb = int(disk)
    except (TypeError, ValueError) as exc:
        raise OrchestrationError("live template did not report container disk size") from exc
    minimum_disk = int(stack["runpod"]["container_disk_gb"])
    if disk_gb < minimum_disk:
        raise OrchestrationError("live template container disk is below the pinned minimum")

    volume = unwrap_record(
        run_json([runpodctl, "network-volume", "get", network_volume_id],
                 action="live_volume_preflight", audit=audit, timeout=30),
        ("networkVolume", "volume", "data"),
        "network volume",
    )
    returned_volume_id = volume.get("id") or volume.get("networkVolumeId")
    if returned_volume_id != network_volume_id:
        raise OrchestrationError("live network volume id does not match the approved volume")
    volume_dc = volume.get("dataCenterId") or volume.get("data_center_id")
    if volume_dc != data_center_id:
        raise OrchestrationError("network volume is not in the approved data center")
    try:
        size_gb = int(volume.get("size", volume.get("sizeInGb")))
    except (TypeError, ValueError) as exc:
        raise OrchestrationError("live network volume did not report its size") from exc
    minimum_size = int(stack["runpod"]["network_volume"]["size_gb"])
    if size_gb < minimum_size:
        raise OrchestrationError("live network volume is smaller than the pinned minimum")
    volume_type = volume.get("type") or volume.get("volumeType")
    expected_type = stack["runpod"]["network_volume"]["type"]
    if volume_type is not None and str(volume_type).upper() != expected_type:
        raise OrchestrationError("live network volume storage tier does not match the pinned stack")
    return {
        "template_id": template_id,
        "image": image,
        "container_disk_gb": disk_gb,
        "network_volume_id": network_volume_id,
        "volume_data_center_id": volume_dc,
        "volume_size_gb": size_gb,
        "volume_type": str(volume_type).upper() if volume_type is not None else "UNREPORTED",
    }


def session_path(session_id: str) -> Path:
    return STATE_ROOT / "sessions" / f"{validate_session_id(session_id)}.json"


def touch_session(path: Path, **fields) -> dict:
    def mutate(state: dict) -> None:
        state.update(fields)

    return update_json_locked(path, mutate)


def start_guardian(intent_path: Path) -> int:
    log_path = STATE_ROOT / "guards" / f"{intent_path.stem}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), "guard-intent",
               "--intent-file", str(intent_path)]
    creationflags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            close_fds=True,
            start_new_session=start_new_session,
            creationflags=creationflags,
        )
    return process.pid


def wait_guardian_ready(intent_path: Path, pid: int, timeout_seconds: int = 15) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = read_json_object(intent_path, "creation intent")
        if state.get("guardian_pid") == pid and state.get("guardian_ready_at"):
            return
        time.sleep(0.1)
    raise OrchestrationError("guardian did not acknowledge readiness; pod creation refused")


def build_pod_create_args(args: argparse.Namespace, name: str, runpodctl: str) -> list[str]:
    stack = read_json_object(PROJECT_ROOT / "config" / "stack.json", "stack config")
    container_disk_gb = int(stack["runpod"]["container_disk_gb"])
    return [
        runpodctl, "pod", "create",
        "--name", name,
        "--template-id", args.template_id,
        "--container-disk-in-gb", str(container_disk_gb),
        "--gpu-id", args.gpu_id,
        "--gpu-count", "1",
        "--cloud-type", "SECURE",
        "--data-center-ids", args.data_center_id,
        "--network-volume-id", args.network_volume_id,
        "--volume-mount-path", "/workspace",
        "--ports", "22/tcp",
        "--ssh",
    ]


def command_guarded_create(args: argparse.Namespace) -> int:
    template_id = validate_resource_id(args.template_id, "template id")
    volume_id = validate_resource_id(args.network_volume_id, "network volume id")
    gpu_id = validate_gpu_id(args.gpu_id)
    data_center = validate_resource_id(args.data_center_id, "data center id")
    deadline = parse_deadline(args.deadline)
    if deadline <= datetime.now(timezone.utc):
        raise OrchestrationError("deadline must be in the future")
    if args.ssh_timeout < 1 or args.create_timeout < 1:
        raise OrchestrationError("timeouts must be positive")
    authorization_path = resolve_state_file(args.authorization_file, "authorizations")
    if not authorization_path.is_file():
        raise OrchestrationError(f"billable authorization not found: {authorization_path}")
    authorization = authorization_constraints(
        authorization_path,
        template_id=template_id,
        network_volume_id=volume_id,
        gpu_id=gpu_id,
        data_center_id=data_center,
        deadline=deadline,
        hourly_usd=args.hourly_usd,
    )

    session_id = uuid.uuid4().hex
    name = f"axi-ltx-{session_id}"
    intent_path = session_path(session_id)
    audit = AuditLog(f"session-{session_id}")
    runpodctl = find_runpodctl()
    live_resources = live_resource_preflight(
        template_id,
        volume_id,
        data_center,
        runpodctl=runpodctl,
        audit=audit,
    )
    live_snapshot = live_gpu_preflight(
        gpu_id,
        data_center,
        args.hourly_usd,
        runpodctl=runpodctl,
        audit=audit,
    )
    intent = {
        "schema_version": 1,
        "session_id": session_id,
        "name": name,
        "status": "arming",
        "created_at": utc_now(),
        "parent_heartbeat_at": utc_now(),
        "deadline": deadline.isoformat(),
        "authorization_file": str(authorization_path),
        "template_id": template_id,
        "network_volume_id": volume_id,
        "gpu_id": gpu_id,
        "data_center_id": data_center,
        "cloud": "SECURE",
        "hourly_usd": args.hourly_usd,
        "live_gpu_snapshot": live_snapshot,
        "live_resource_snapshot": live_resources,
        "live_checked_at": utc_now(),
        "pod_ids": [],
    }
    write_json_atomic(intent_path, intent)
    audit.emit("creation_intent_persisted", session_id=session_id, name=name,
               deadline=deadline.isoformat())
    try:
        guardian_pid = start_guardian(intent_path)
        touch_session(intent_path, guardian_pid=guardian_pid)
        wait_guardian_ready(intent_path, guardian_pid)
        consume_authorization(authorization_path, session_id, authorization)
    except (OrchestrationError, OSError, subprocess.SubprocessError):
        touch_session(intent_path, status="completed", completed_at=utc_now(),
                      completion_reason="pre_create_failure")
        raise
    touch_session(intent_path, status="creating", parent_heartbeat_at=utc_now(),
                  authorization_consumed_at=utc_now())
    audit.emit("guardian_armed_before_create", guardian_pid=guardian_pid)

    create_args = build_pod_create_args(args, name, runpodctl)
    pod_id: str | None = None
    try:
        payload = run_json(create_args, action="guarded_pod_create", audit=audit,
                           timeout=args.create_timeout)
        pod_id = pod_id_from_create(payload)
        if not pod_id:
            discovered = discover_owned_pod_ids(name, runpodctl=runpodctl, audit=audit)
            if len(discovered) == 1:
                pod_id = discovered[0]
            elif len(discovered) > 1:
                touch_session(intent_path, pod_ids=discovered, status="delete_requested",
                              delete_reason="duplicate_owned_pods")
                raise OrchestrationError("creation produced duplicate owned pods; guardian will delete them")
            else:
                raise OrchestrationError("pod create returned no id; guardian remains armed for discovery")
        pod_id = validate_pod_id(pod_id)
        touch_session(intent_path, pod_ids=[pod_id], status="waiting_ssh",
                      pod_id_persisted_at=utc_now(), parent_heartbeat_at=utc_now())
        audit.emit("pod_id_persisted", pod_id=pod_id)

        def heartbeat() -> None:
            touch_session(intent_path, parent_heartbeat_at=utc_now())

        pod = PodSsh(pod_id)
        wait_for_ssh(pod, args.ssh_timeout, heartbeat=heartbeat)
        touch_session(intent_path, status="ssh_ready", ssh_ready_at=utc_now(),
                      parent_heartbeat_at=utc_now())
        print(json.dumps({"status": "ssh_ready", "session_id": session_id,
                          "pod_id": pod_id, "guardian_pid": guardian_pid,
                          "deadline": deadline.isoformat()}, indent=2))
        return 0
    except (OrchestrationError, subprocess.TimeoutExpired) as exc:
        state = read_json_object(intent_path, "creation intent")
        known_ids = list(state.get("pod_ids") or [])
        if pod_id and pod_id not in known_ids:
            known_ids.append(pod_id)
        touch_session(intent_path, status="delete_requested", pod_ids=known_ids,
                      delete_reason=type(exc).__name__, parent_heartbeat_at=utc_now())
        audit.emit("guarded_create_failed_cleanup_requested", error=type(exc).__name__)
        for owned_id in known_ids:
            delete_pod(validate_pod_id(owned_id), audit=audit)
        raise


def command_guard_intent(args: argparse.Namespace) -> int:
    intent_path = resolve_state_file(args.intent_file, "sessions")
    state = read_json_object(intent_path, "creation intent")
    session_id = validate_session_id(str(state.get("session_id", "")))
    if intent_path != session_path(session_id).resolve():
        raise OrchestrationError("intent filename does not match its session id")
    name = str(state.get("name", ""))
    if name != f"axi-ltx-{session_id}":
        raise OrchestrationError("intent contains an invalid owned pod name")
    deadline = parse_deadline(str(state.get("deadline", "")))
    if args.poll_seconds < 1 or args.parent_lease_seconds < 10:
        raise OrchestrationError("guardian timing values are invalid")
    audit = AuditLog(f"session-{session_id}")
    runpodctl = find_runpodctl()
    touch_session(intent_path, guardian_pid=os.getpid(), guardian_ready_at=utc_now())
    audit.emit("intent_guardian_ready", deadline=deadline.isoformat())

    while True:
        state = read_json_object(intent_path, "creation intent")
        status = state.get("status")
        if status in {"completed", "clean"}:
            audit.emit("intent_guardian_stopped", status=status)
            return 0
        owned_ids = {
            validate_pod_id(value) for value in state.get("pod_ids", [])
            if isinstance(value, str) and POD_ID_RE.fullmatch(value)
        }
        try:
            discovered = discover_owned_pod_ids(name, runpodctl=runpodctl, audit=audit)
            newly_found = set(discovered) - owned_ids
            if newly_found:
                owned_ids.update(newly_found)
                touch_session(intent_path, pod_ids=sorted(owned_ids),
                              guardian_discovered_at=utc_now())
                audit.emit("guardian_discovered_owned_pods", pod_ids=sorted(newly_found))
        except (OrchestrationError, subprocess.TimeoutExpired) as exc:
            audit.emit("guardian_discovery_failed", error=type(exc).__name__)

        now = datetime.now(timezone.utc)
        if status in {"arming", "creating", "waiting_ssh"}:
            try:
                parent_heartbeat = parse_deadline(str(state.get("parent_heartbeat_at", "")))
            except (OrchestrationError, ValueError):
                parent_heartbeat = datetime.fromtimestamp(0, timezone.utc)
            if (now - parent_heartbeat).total_seconds() >= args.parent_lease_seconds:
                status = "delete_requested"
                touch_session(intent_path, status=status, delete_reason="parent_lease_expired")
                audit.emit("guardian_parent_lease_expired")
        if now >= deadline:
            status = "delete_requested"
            touch_session(intent_path, status=status, delete_reason="deadline")

        if status == "delete_requested" and owned_ids:
            failures: list[str] = []
            for pod_id in sorted(owned_ids):
                try:
                    delete_pod(pod_id, audit=audit)
                except OrchestrationError:
                    failures.append(pod_id)
            if not failures:
                touch_session(intent_path, status="clean", deleted_pod_ids=sorted(owned_ids),
                              cleaned_at=utc_now())
                audit.emit("guardian_cleanup_completed", pod_ids=sorted(owned_ids))
                return 0
        elif status == "delete_requested" and now >= deadline:
            touch_session(intent_path, status="clean", cleaned_at=utc_now(),
                          cleanup_note="no owned pod discovered by deadline")
            return 0
        remaining = max(1, (deadline - now).total_seconds())
        time.sleep(max(1, min(args.poll_seconds, remaining)))


def command_teardown(args: argparse.Namespace) -> int:
    pod_id = validate_pod_id(args.pod_id)
    if args.confirm_pod_id != pod_id:
        raise OrchestrationError("--confirm-pod-id must exactly match --pod-id")
    audit = AuditLog(pod_id)
    audit.emit("teardown_authorized", reason=args.reason)
    delete_pod(pod_id, audit=audit)
    sessions_root = STATE_ROOT / "sessions"
    if sessions_root.is_dir():
        for candidate in sessions_root.glob("*.json"):
            try:
                state = read_json_object(candidate, "creation intent")
            except OrchestrationError:
                continue
            if pod_id in state.get("pod_ids", []):
                touch_session(candidate, status="completed", completed_at=utc_now(),
                              completion_reason=args.reason)
    print(json.dumps({"status": "delete_requested", "pod_id": pod_id, "reason": args.reason}))
    return 0


def parse_deadline(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError) as exc:
        raise OrchestrationError("deadline must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise OrchestrationError("deadline must include a timezone")
    return parsed.astimezone(timezone.utc)


def remote_mtime(pod: PodSsh, info: SshInfo, path: str) -> int | None:
    result = pod.run(
        info,
        f"stat -c %Y -- {path} 2>/dev/null || true",
        action="heartbeat_read",
        timeout=20,
    )
    text = result.stdout.decode("utf-8", errors="strict").strip()
    return int(text) if text.isdigit() else None


def command_watchdog(args: argparse.Namespace) -> int:
    pod_id = validate_pod_id(args.pod_id)
    deadline = parse_deadline(args.deadline)
    heartbeat = validate_heartbeat_path(args.heartbeat_path) if args.heartbeat_path else None
    if args.poll_seconds < 1:
        raise OrchestrationError("--poll-seconds must be positive")
    if args.idle_minutes is not None and args.idle_minutes < 1:
        raise OrchestrationError("--idle-minutes must be positive")
    if args.idle_minutes is not None and heartbeat is None:
        raise OrchestrationError("--idle-minutes requires --heartbeat-path")
    audit = AuditLog(pod_id)
    pod = PodSsh(pod_id)
    audit.emit("watchdog_armed", deadline=deadline.isoformat(), idle_minutes=args.idle_minutes,
               heartbeat_path=heartbeat)
    reason = "deadline"
    try:
        while datetime.now(timezone.utc) < deadline:
            if heartbeat and args.idle_minutes is not None:
                try:
                    info = pod.resolve()
                    mtime = remote_mtime(pod, info, heartbeat)
                    if mtime is not None:
                        idle_seconds = time.time() - mtime
                        if idle_seconds >= args.idle_minutes * 60:
                            reason = "idle_timeout"
                            break
                except OrchestrationError as exc:
                    audit.emit("watchdog_probe_failed", error=type(exc).__name__)
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            time.sleep(max(1, min(args.poll_seconds, remaining)))
    except KeyboardInterrupt:
        reason = "watchdog_interrupted_fail_safe"
    audit.emit("watchdog_triggered", reason=reason)
    delete_pod(pod_id, audit=audit)
    print(json.dumps({"status": "delete_requested", "pod_id": pod_id, "reason": reason}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="validate local tools and pinned config")
    doctor.set_defaults(func=command_doctor)

    guarded_create = subparsers.add_parser(
        "guarded-create",
        help="arm an independent guardian before creating one authorized Pod",
    )
    guarded_create.add_argument("--template-id", required=True)
    guarded_create.add_argument("--network-volume-id", required=True)
    guarded_create.add_argument("--gpu-id", required=True)
    guarded_create.add_argument("--data-center-id", required=True)
    guarded_create.add_argument("--deadline", required=True,
                                help="exact approved ISO-8601 timestamp with timezone")
    guarded_create.add_argument("--hourly-usd", required=True, type=float,
                                help="live hourly price observed immediately before creation")
    guarded_create.add_argument("--authorization-file", required=True,
                                help="one-time approval JSON below .runpod/authorizations")
    guarded_create.add_argument("--create-timeout", type=int, default=120)
    guarded_create.add_argument("--ssh-timeout", type=int, default=900)
    guarded_create.set_defaults(func=command_guarded_create)

    guard_intent = subparsers.add_parser(
        "guard-intent", help="internal detached guardian for a persisted creation intent"
    )
    guard_intent.add_argument("--intent-file", required=True)
    guard_intent.add_argument("--poll-seconds", type=int, default=15)
    guard_intent.add_argument("--parent-lease-seconds", type=int, default=180)
    guard_intent.set_defaults(func=command_guard_intent)

    readiness = subparsers.add_parser("readiness", help="prove SSH and ComfyUI through a tunnel")
    readiness.add_argument("--pod-id", required=True)
    readiness.add_argument("--ssh-timeout", type=int, default=600)
    readiness.add_argument("--comfy-timeout", type=int, default=600)
    readiness.add_argument("--local-port", type=int, default=0)
    readiness.set_defaults(func=command_readiness)

    bootstrap = subparsers.add_parser("bootstrap", help="prepare one persistent LTX profile")
    bootstrap.add_argument("--pod-id", required=True)
    bootstrap.add_argument("--profile", required=True)
    bootstrap.add_argument("--ssh-timeout", type=int, default=600)
    bootstrap.add_argument("--bootstrap-timeout", type=int, default=21600)
    bootstrap.add_argument("--comfy-timeout", type=int, default=600)
    bootstrap.add_argument("--local-port", type=int, default=0)
    bootstrap.set_defaults(func=command_bootstrap)

    tunnel = subparsers.add_parser("tunnel", help="keep a local-only ComfyUI SSH tunnel open")
    tunnel.add_argument("--pod-id", required=True)
    tunnel.add_argument("--local-port", type=int, default=18188)
    tunnel.add_argument("--ssh-timeout", type=int, default=600)
    tunnel.set_defaults(func=command_tunnel)

    submit = subparsers.add_parser("submit", help="submit or resume one ComfyUI API prompt")
    submit.add_argument("--pod-id", required=True)
    submit.add_argument("--job-id", required=True)
    submit.add_argument("--prompt-json", required=True)
    submit.add_argument("--ssh-timeout", type=int, default=600)
    submit.add_argument("--wait-timeout", type=int, default=7200)
    submit.add_argument("--local-port", type=int, default=0)
    submit.add_argument("--metrics-interval", type=int, default=5)
    submit.set_defaults(func=command_submit)

    upload_input = subparsers.add_parser(
        "upload-input", help="upload one validated ComfyUI input through SSH"
    )
    upload_input.add_argument("--pod-id", required=True)
    upload_input.add_argument("--local-file", required=True)
    upload_input.add_argument("--remote-name")
    upload_input.add_argument("--ssh-timeout", type=int, default=600)
    upload_input.set_defaults(func=command_upload_input)

    metrics = subparsers.add_parser("metrics", help="read one GPU utilization snapshot over SSH")
    metrics.add_argument("--pod-id", required=True)
    metrics.add_argument("--ssh-timeout", type=int, default=600)
    metrics.add_argument("--timeout", type=int, default=60)
    metrics.set_defaults(func=command_metrics)

    ltx_runtime = subparsers.add_parser(
        "ltx-runtime-check", help="verify LTX node source and Kornia compatibility over SSH"
    )
    ltx_runtime.add_argument("--pod-id", required=True)
    ltx_runtime.add_argument("--ssh-timeout", type=int, default=600)
    ltx_runtime.add_argument("--timeout", type=int, default=60)
    ltx_runtime.set_defaults(func=command_ltx_runtime_check)

    repair_ltx = subparsers.add_parser(
        "repair-ltx-kornia", help="apply the pinned LTX/Kornia compatibility repair over SSH"
    )
    repair_ltx.add_argument("--pod-id", required=True)
    repair_ltx.add_argument("--ssh-timeout", type=int, default=600)
    repair_ltx.add_argument("--timeout", type=int, default=120)
    repair_ltx.set_defaults(func=command_repair_ltx_kornia)

    stage_output = subparsers.add_parser(
        "stage-output", help="move one ComfyUI output into persistent job staging"
    )
    stage_output.add_argument("--pod-id", required=True)
    stage_output.add_argument("--job-id", required=True)
    stage_output.add_argument("--source-filename", required=True)
    stage_output.add_argument("--target-filename", required=True)
    stage_output.add_argument("--ssh-timeout", type=int, default=600)
    stage_output.add_argument("--timeout", type=int, default=600)
    stage_output.set_defaults(func=command_stage_output)

    finalize = subparsers.add_parser("finalize", help="validate and atomically publish an output")
    finalize.add_argument("--pod-id", required=True)
    finalize.add_argument("--job-id", required=True)
    finalize.add_argument("--filename", required=True)
    finalize.add_argument("--ssh-timeout", type=int, default=600)
    finalize.add_argument("--timeout", type=int, default=600)
    finalize.set_defaults(func=command_finalize)

    watchdog = subparsers.add_parser("watchdog", help="delete a pod at deadline or idle timeout")
    watchdog.add_argument("--pod-id", required=True)
    watchdog.add_argument("--deadline", required=True, help="ISO-8601 timestamp with timezone")
    watchdog.add_argument("--poll-seconds", type=int, default=30)
    watchdog.add_argument("--idle-minutes", type=int)
    watchdog.add_argument("--heartbeat-path")
    watchdog.set_defaults(func=command_watchdog)

    teardown = subparsers.add_parser("teardown", help="explicitly delete one exact pod")
    teardown.add_argument("--pod-id", required=True)
    teardown.add_argument("--confirm-pod-id", required=True)
    teardown.add_argument("--reason", required=True, choices=["job-complete", "failed", "manual"])
    teardown.set_defaults(func=command_teardown)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OrchestrationError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
