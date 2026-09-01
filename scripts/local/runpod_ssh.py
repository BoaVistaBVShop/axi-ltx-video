#!/usr/bin/env python3
"""SSH-only local control plane for disposable axi-ltx-video Runpod Pods."""

from __future__ import annotations

import argparse
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
import time
from typing import Iterator
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
        result = run_process(
            [self.ssh_keyscan, "-T", "10", "-p", str(info.port), info.host],
            action="ssh_keyscan",
            audit=self.audit,
            timeout=15,
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
            "-o", f"UserKnownHostsFile={self.known_hosts}",
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


def wait_for_ssh(pod: PodSsh, timeout_seconds: int) -> SshInfo:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
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
            if status.get("completed") is True or record.get("outputs"):
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
    with ssh_tunnel(pod, info, args.local_port) as port:
        base_url = f"http://127.0.0.1:{port}"
        if state and state.get("prompt_id"):
            prompt_id = state["prompt_id"]
            pod.audit.emit("generation_resumed", job_id=job_id, prompt_id=prompt_id)
        else:
            response = http_json("POST", f"{base_url}/prompt", load_prompt_payload(prompt_path), timeout=30)
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


def command_teardown(args: argparse.Namespace) -> int:
    pod_id = validate_pod_id(args.pod_id)
    if args.confirm_pod_id != pod_id:
        raise OrchestrationError("--confirm-pod-id must exactly match --pod-id")
    audit = AuditLog(pod_id)
    audit.emit("teardown_authorized", reason=args.reason)
    delete_pod(pod_id, audit=audit)
    print(json.dumps({"status": "delete_requested", "pod_id": pod_id, "reason": args.reason}))
    return 0


def parse_deadline(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
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
    submit.set_defaults(func=command_submit)

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
