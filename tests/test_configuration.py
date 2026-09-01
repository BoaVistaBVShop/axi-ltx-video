from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_json(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


class ConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.models = load_json("config/models-manifest.json")
        cls.workflows = load_json("config/workflows-manifest.json")
        cls.generation = load_json("config/generation-profiles.json")

    def test_every_generation_profile_has_models_and_workflow(self) -> None:
        model_profiles = {
            profile
            for entry in self.models["files"]
            for profile in entry["profiles"]
        }
        workflows = {entry["path"] for entry in self.workflows["files"]}

        for name, profile in self.generation["profiles"].items():
            with self.subTest(profile=name):
                self.assertIn(profile["model_profile"], model_profiles)
                workflow = profile.get("workflow")
                if workflow is None and "inherits" in profile:
                    workflow = self.generation["profiles"][profile["inherits"]]["workflow"]
                self.assertIn(workflow, workflows)

    def test_lora_is_opt_in_per_scene(self) -> None:
        policy = self.generation["common"]["lora_ab_policy"]
        self.assertTrue(policy["ask_user_before_scene_submission"])
        self.assertTrue(policy["decision_required_per_scene"])
        self.assertFalse(policy["default_enabled"])
        self.assertTrue(policy["never_auto_select_lora"])

        lora_profiles = {
            name: profile
            for name, profile in self.generation["profiles"].items()
            if name.startswith("lora_")
        }
        self.assertEqual(len(lora_profiles), 4)
        for name, profile in lora_profiles.items():
            with self.subTest(profile=name):
                self.assertTrue(profile["requires_user_confirmation_per_scene"])
                self.assertEqual(
                    profile["validation_status"], "configured_not_gpu_validated"
                )

    def test_lora_artifacts_are_integrity_pinned(self) -> None:
        loras = [
            entry for entry in self.models["files"] if entry.get("kind") == "ic_lora"
        ]
        self.assertEqual(len(loras), 4)
        for entry in loras:
            with self.subTest(path=entry["path"]):
                self.assertGreater(entry["bytes"], 0)
                self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(entry["path"].startswith("loras/"))
                self.assertEqual(entry["workflow_base"], "LTX-2.5 distilled BF16")

        for entry in self.workflows["files"]:
            with self.subTest(path=entry["path"]):
                self.assertGreater(entry["bytes"], 0)
                self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
                self.assertIn(self.workflows["source_commit"], entry["url"])


if __name__ == "__main__":
    unittest.main()
