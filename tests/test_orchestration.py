from __future__ import annotations

from argparse import Namespace
from contextlib import nullcontext
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


orchestrator = load_module("runpod_ssh", "scripts/local/runpod_ssh.py")
bootstrap = load_module("bootstrap", "scripts/pod/bootstrap.py")


class SshSafetyTests(unittest.TestCase):
    def test_parses_ssh_info_and_uses_strict_isolated_host_checking(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key"
            key.write_text("fixture", encoding="utf-8")
            info = orchestrator.SshInfo.from_payload(
                {
                    "ip": "203.0.113.10",
                    "port": 22022,
                    "ssh_key": {"path": str(key)},
                }
            )
            pod = object.__new__(orchestrator.PodSsh)
            pod.ssh = "ssh"
            pod.known_hosts = Path(directory) / "known_hosts"
            args = pod.base_args(info)
            rendered = " ".join(str(item) for item in args)
            self.assertIn("StrictHostKeyChecking=yes", rendered)
            self.assertIn(f'UserKnownHostsFile="{pod.known_hosts}"', rendered)
            self.assertNotIn("StrictHostKeyChecking=no", rendered)
            self.assertNotIn("root@203.0.113.10", rendered)
            self.assertEqual(pod.destination(info), "root@203.0.113.10")

    def test_rejects_shell_injection_surfaces(self):
        for value in ["video.mp4;rm", "../video.mp4", "a b.mp4", "$(id).mp4"]:
            with self.subTest(value=value):
                with self.assertRaises(orchestrator.OrchestrationError):
                    orchestrator.validate_basename(value)
        for value in ["/tmp/heartbeat", "/workspace/a;rm", "/workspace/../root/x"]:
            with self.subTest(value=value):
                with self.assertRaises(orchestrator.OrchestrationError):
                    orchestrator.validate_heartbeat_path(value)

    def test_teardown_requires_exact_pod_confirmation(self):
        args = Namespace(pod_id="abc123", confirm_pod_id="different", reason="manual")
        with mock.patch.object(orchestrator, "delete_pod") as delete:
            with self.assertRaises(orchestrator.OrchestrationError):
                orchestrator.command_teardown(args)
            delete.assert_not_called()

    def test_redacts_nested_secret_fields(self):
        payload = {"token": "x", "nested": {"HF_TOKEN": "y", "safe": "ok"}}
        self.assertEqual(
            orchestrator.redact(payload),
            {"token": "[REDACTED]", "nested": {"HF_TOKEN": "[REDACTED]", "safe": "ok"}},
        )

    def test_deadline_requires_timezone(self):
        with self.assertRaises(orchestrator.OrchestrationError):
            orchestrator.parse_deadline("2026-09-01T12:00:00")
        parsed = orchestrator.parse_deadline("2026-09-01T12:00:00-03:00")
        self.assertEqual(parsed.isoformat(), "2026-09-01T15:00:00+00:00")

    def test_prompt_loader_accepts_only_json_content_and_adds_client_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompt.json"
            path.write_text(json.dumps({"prompt": {"1": {"class_type": "Test"}}}), encoding="utf-8")
            payload = orchestrator.load_prompt_payload(path)
            self.assertEqual(payload["prompt"]["1"]["class_type"], "Test")
            self.assertTrue(payload["client_id"])

    def test_delete_treats_already_absent_pod_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = orchestrator.AuditLog("abc123")
            audit.path = Path(directory) / "audit.jsonl"
            error = orchestrator.CommandError(
                "pod_delete", 1, json.dumps({"code": "not_found", "error": "not found"})
            )
            with mock.patch.object(orchestrator, "find_runpodctl", return_value="runpodctl"), \
                 mock.patch.object(orchestrator, "run_process", side_effect=error):
                orchestrator.delete_pod("abc123", audit=audit)
            self.assertIn("pod_already_absent", audit.path.read_text(encoding="utf-8"))

    def test_gpu_metric_summary_reports_peak_mean_and_vram_percent(self):
        summary = orchestrator.metric_summary([
            {"utilization.gpu": "20", "utilization.memory": "10", "memory.used": "25",
             "memory.total": "100", "temperature.gpu": "40", "power.draw": "100",
             "power.limit": "500"},
            {"utilization.gpu": "100", "utilization.memory": "80", "memory.used": "75",
             "memory.total": "100", "temperature.gpu": "60", "power.draw": "400",
             "power.limit": "500"},
        ])
        self.assertEqual(summary["utilization.gpu"]["max"], 100.0)
        self.assertEqual(summary["utilization.gpu"]["mean"], 60.0)
        self.assertEqual(summary["memory.used_percent"]["max"], 75.0)


class GuardedCreationTests(unittest.TestCase):
    def _approval(self, root: Path, *, deadline: datetime, gpu_id: str = "gpu.fixture") -> Path:
        path = root / "authorizations" / "approval.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "authorization_id": "approval-fixture",
                    "authorized": True,
                    "one_time": True,
                    "approved_at": datetime.now(timezone.utc).isoformat(),
                    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    "constraints": {
                        "template_id": "template.fixture",
                        "network_volume_id": "volume.fixture",
                        "gpu_id": gpu_id,
                        "data_center_id": "EU-RO-1",
                        "cloud": "SECURE",
                        "deadline": deadline.isoformat(),
                        "max_hourly_usd": 1.25,
                        "max_total_usd": 5.0,
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def _args(self, approval: Path, deadline: datetime) -> Namespace:
        return Namespace(
            template_id="template.fixture",
            network_volume_id="volume.fixture",
            gpu_id="gpu.fixture",
            data_center_id="EU-RO-1",
            deadline=deadline.isoformat(),
            hourly_usd=0.99,
            authorization_file=str(approval),
            create_timeout=30,
            ssh_timeout=30,
        )

    def test_refuses_mismatched_authorization_before_guardian_or_create(self):
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / ".runpod"
            deadline = datetime.now(timezone.utc) + timedelta(minutes=30)
            approval = self._approval(state_root, deadline=deadline, gpu_id="other.gpu")
            with mock.patch.object(orchestrator, "STATE_ROOT", state_root), \
                 mock.patch.object(orchestrator, "start_guardian") as start_guardian, \
                 mock.patch.object(orchestrator, "run_json") as run_json:
                with self.assertRaisesRegex(orchestrator.OrchestrationError, "mismatch: gpu_id"):
                    orchestrator.command_guarded_create(self._args(approval, deadline))
            start_guardian.assert_not_called()
            run_json.assert_not_called()

    def test_guardian_is_ready_before_create_and_id_is_persisted_before_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / ".runpod"
            deadline = datetime.now(timezone.utc) + timedelta(minutes=30)
            approval = self._approval(state_root, deadline=deadline)
            events = []

            def fake_start(intent_path):
                self.assertEqual(orchestrator.read_json_object(intent_path, "intent")["status"], "arming")
                events.append("guardian_started")
                return 4321

            def fake_guard_ready(intent_path, pid):
                self.assertEqual(pid, 4321)
                orchestrator.touch_session(intent_path, guardian_pid=pid,
                                           guardian_ready_at=orchestrator.utc_now())
                events.append("guardian_ready")

            def fake_run_json(command, **_kwargs):
                self.assertIn("pod", command)
                self.assertIn("create", command)
                disk_flag = command.index("--container-disk-in-gb")
                self.assertEqual(command[disk_flag + 1], "150")
                events.append("create")
                return {"id": "pod123"}

            def fake_wait(_pod, _timeout, heartbeat=None):
                intent = next((state_root / "sessions").glob("*.json"))
                state = orchestrator.read_json_object(intent, "intent")
                self.assertEqual(state["status"], "waiting_ssh")
                self.assertEqual(state["pod_ids"], ["pod123"])
                self.assertIsNotNone(heartbeat)
                heartbeat()
                events.append("ssh_wait")
                return object()

            with mock.patch.object(orchestrator, "STATE_ROOT", state_root), \
                 mock.patch.object(orchestrator, "start_guardian", side_effect=fake_start), \
                 mock.patch.object(orchestrator, "wait_guardian_ready", side_effect=fake_guard_ready), \
                 mock.patch.object(orchestrator, "find_runpodctl", return_value="runpodctl"), \
                 mock.patch.object(orchestrator, "live_resource_preflight", return_value={
                     "template_id": "template.fixture", "network_volume_id": "volume.fixture",
                 }), \
                 mock.patch.object(orchestrator, "live_gpu_preflight", return_value={
                     "gpu_id": "gpu.fixture", "data_center_id": "EU-RO-1",
                     "secure_hourly_usd": 0.99, "stock_status": "Low",
                 }), \
                 mock.patch.object(orchestrator, "find_executable", return_value="fixture"), \
                 mock.patch.object(orchestrator, "run_json", side_effect=fake_run_json), \
                 mock.patch.object(orchestrator, "wait_for_ssh", side_effect=fake_wait), \
                 redirect_stdout(io.StringIO()):
                result = orchestrator.command_guarded_create(self._args(approval, deadline))

            self.assertEqual(result, 0)
            self.assertLess(events.index("guardian_ready"), events.index("create"))
            self.assertLess(events.index("create"), events.index("ssh_wait"))
            state = orchestrator.read_json_object(
                next((state_root / "sessions").glob("*.json")), "intent"
            )
            self.assertEqual(state["status"], "ssh_ready")
            consumed = orchestrator.read_json_object(approval, "approval")
            self.assertEqual(consumed["consumed_by_session"], state["session_id"])

    def test_guardian_discovers_and_deletes_owned_pod_after_parent_lease_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / ".runpod"
            session_id = "a" * 32
            intent = state_root / "sessions" / f"{session_id}.json"
            orchestrator.write_json_atomic(
                intent,
                {
                    "schema_version": 1,
                    "session_id": session_id,
                    "name": f"axi-ltx-{session_id}",
                    "status": "creating",
                    "deadline": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
                    "parent_heartbeat_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
                    "pod_ids": [],
                },
            )
            args = Namespace(intent_file=str(intent), poll_seconds=1, parent_lease_seconds=30)
            with mock.patch.object(orchestrator, "STATE_ROOT", state_root), \
                 mock.patch.object(orchestrator, "find_runpodctl", return_value="runpodctl"), \
                 mock.patch.object(orchestrator, "discover_owned_pod_ids", return_value=["pod123"]), \
                 mock.patch.object(orchestrator, "delete_pod") as delete:
                result = orchestrator.command_guard_intent(args)
            self.assertEqual(result, 0)
            delete.assert_called_once_with("pod123", audit=mock.ANY)
            state = orchestrator.read_json_object(intent, "intent")
            self.assertEqual(state["status"], "clean")
            self.assertEqual(state["deleted_pod_ids"], ["pod123"])

    def test_create_command_has_ssh_but_does_not_wait_inside_runpodctl(self):
        deadline = datetime.now(timezone.utc) + timedelta(minutes=30)
        args = self._args(Path("approval.json"), deadline)
        command = orchestrator.build_pod_create_args(args, "axi-ltx-" + "b" * 32, "runpodctl")
        self.assertIn("--ssh", command)
        self.assertIn("22/tcp", command)
        self.assertNotIn("--wait", command)
        self.assertEqual(command[command.index("--cloud-type") + 1], "SECURE")

    def test_live_gpu_preflight_checks_price_and_data_center_stock(self):
        payload = [
            {
                "gpuId": "NVIDIA GeForce RTX 5090",
                "secureCloud": True,
                "securePricePerHr": 0.99,
                "dataCenterAvailability": [
                    {"dataCenterId": "EU-RO-1", "stockStatus": "Low"}
                ],
            }
        ]
        with mock.patch.object(orchestrator, "run_json", return_value=payload):
            snapshot = orchestrator.live_gpu_preflight(
                "NVIDIA GeForce RTX 5090", "EU-RO-1", 0.99,
                runpodctl="runpodctl", audit=mock.Mock(),
            )
            self.assertEqual(snapshot["stock_status"], "Low")
            with self.assertRaisesRegex(orchestrator.OrchestrationError, "price changed"):
                orchestrator.live_gpu_preflight(
                    "NVIDIA GeForce RTX 5090", "EU-RO-1", 0.98,
                    runpodctl="runpodctl", audit=mock.Mock(),
                )

    def test_live_resource_preflight_pins_image_volume_location_and_size(self):
        stack = json.loads((ROOT / "config" / "stack.json").read_text(encoding="utf-8"))
        responses = [
            {
                "id": "template.fixture",
                "imageName": stack["image"]["published"],
                "containerDiskInGb": 150,
            },
            {
                "id": "volume.fixture",
                "dataCenterId": "EU-RO-1",
                "size": 200,
                "type": "STANDARD",
            },
        ]
        with mock.patch.object(orchestrator, "run_json", side_effect=responses):
            snapshot = orchestrator.live_resource_preflight(
                "template.fixture", "volume.fixture", "EU-RO-1",
                runpodctl="runpodctl", audit=mock.Mock(),
            )
        self.assertEqual(snapshot["image"], stack["image"]["published"])
        self.assertEqual(snapshot["volume_data_center_id"], "EU-RO-1")
        self.assertEqual(snapshot["volume_size_gb"], 200)

    def test_deadline_cannot_exceed_approved_total_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / ".runpod"
            deadline = datetime.now(timezone.utc) + timedelta(hours=10)
            approval = self._approval(state_root, deadline=deadline)
            with mock.patch.object(orchestrator, "STATE_ROOT", state_root), \
                 mock.patch.object(orchestrator, "start_guardian") as start_guardian:
                with self.assertRaisesRegex(orchestrator.OrchestrationError, "total cost ceiling"):
                    orchestrator.command_guarded_create(self._args(approval, deadline))
            start_guardian.assert_not_called()

    def test_create_payload_must_resolve_to_at_most_one_id(self):
        self.assertEqual(orchestrator.pod_id_from_create({"pod": {"id": "pod123"}}), "pod123")
        with self.assertRaises(orchestrator.OrchestrationError):
            orchestrator.pod_id_from_create({"pods": [{"id": "pod123"}, {"id": "pod456"}]})


class BootstrapTests(unittest.TestCase):
    def test_pid1_environment_parser_does_not_require_logging(self):
        blob = b"PATH=/usr/bin\0HF_TOKEN=hf_fixture\0EMPTY=\0"
        self.assertEqual(bootstrap.parse_environ_blob(blob, "HF_TOKEN"), "hf_fixture")
        self.assertIsNone(bootstrap.parse_environ_blob(blob, "MISSING"))

    def test_all_generation_profiles_resolve(self):
        profiles = json.loads(
            (ROOT / "config" / "generation-profiles.json").read_text(encoding="utf-8")
        )
        for profile_name in profiles["profiles"]:
            with self.subTest(profile=profile_name):
                model_profile, workflow = bootstrap.resolve_profile(profiles, profile_name)
                self.assertTrue(model_profile)
                self.assertTrue(workflow.endswith(".json"))

    def _stack_fixture(self, root: Path) -> None:
        profiles = {
            "profiles": {
                "preview": {
                    "model_profile": "preview",
                    "workflow": "workflow.json",
                }
            }
        }
        (root / "generation-profiles.json").write_text(json.dumps(profiles), encoding="utf-8")
        (root / "models-manifest.json").write_text("{}", encoding="utf-8")
        (root / "workflows-manifest.json").write_text("{}", encoding="utf-8")

    def test_marker_is_written_only_after_every_validator_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stack = root / "stack"
            workspace = root / "workspace"
            stack.mkdir()
            self._stack_fixture(stack)
            calls = []

            def fake_run(command, **kwargs):
                calls.append((command, dict(kwargs.get("env", {}))))

            with mock.patch.object(bootstrap, "exclusive_lock", return_value=nullcontext()), \
                 mock.patch.object(bootstrap, "run_checked", side_effect=fake_run), \
                 mock.patch.dict(bootstrap.os.environ, {"HF_TOKEN": "hf_fixture", "OTHER_SECRET": "no"}):
                    with redirect_stdout(io.StringIO()):
                        result = bootstrap.main(
                            ["--profile", "preview", "--stack-root", str(stack),
                             "--workspace", str(workspace), "--allow-non-mount", "--skip-healthcheck"]
                        )
            self.assertEqual(result, 0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][1]["HF_TOKEN"], "hf_fixture")
            self.assertNotIn("HF_TOKEN", calls[1][1])
            self.assertNotIn("OTHER_SECRET", calls[0][1])
            marker = workspace / ".axi-ltx" / "bootstrap" / "preview.json"
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["status"], "ready")

            marker.unlink()
            with mock.patch.object(bootstrap, "exclusive_lock", return_value=nullcontext()), \
                 mock.patch.object(bootstrap, "run_checked", side_effect=RuntimeError("failed")):
                with self.assertRaises(RuntimeError):
                    bootstrap.main(
                        ["--profile", "preview", "--stack-root", str(stack),
                         "--workspace", str(workspace), "--allow-non-mount", "--skip-healthcheck"]
                    )
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
