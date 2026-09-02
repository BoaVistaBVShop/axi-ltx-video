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
        allowed_validation_states = {
            "configured_not_gpu_validated",
            "gpu_smoke_validated",
            "gpu_smoke_validated_with_workflow_deviation",
        }
        for name, profile in lora_profiles.items():
            with self.subTest(profile=name):
                self.assertTrue(profile["requires_user_confirmation_per_scene"])
                self.assertIn(profile["validation_status"], allowed_validation_states)
                if profile["validation_status"].startswith("gpu_smoke_validated"):
                    self.assertIn("last_validation_job", profile)

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

    def test_axi360_ingredients_ab_is_comparable_and_distilled(self) -> None:
        baseline = self.generation["profiles"]["axi360_ingredients_ab_baseline"]
        variant = self.generation["profiles"]["axi360_ingredients_ab_variant"]

        self.assertEqual(variant["ab_baseline_profile"], "axi360_ingredients_ab_baseline")
        self.assertEqual(baseline["official_model_variant"], "LTX-2.5 distilled BF16")
        self.assertEqual(variant["official_model_variant"], baseline["official_model_variant"])
        self.assertEqual(variant["lora_strength"], 1.3)
        for key in ("stage_1_width", "stage_1_height", "delivery_width",
                    "delivery_height", "fps", "audio_output", "seed"):
            with self.subTest(setting=key):
                self.assertEqual(baseline["job_defaults"][key], variant["job_defaults"][key])
        self.assertFalse(baseline["job_defaults"]["audio_output"])
        self.assertEqual(baseline["job_defaults"]["fps"], 25)


if __name__ == "__main__":
    unittest.main()
