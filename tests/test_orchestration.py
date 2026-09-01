from __future__ import annotations

from argparse import Namespace
from contextlib import nullcontext
from contextlib import redirect_stdout
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
            self.assertIn(f"UserKnownHostsFile={pod.known_hosts}", rendered)
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
