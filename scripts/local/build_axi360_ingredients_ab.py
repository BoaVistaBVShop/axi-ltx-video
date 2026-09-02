#!/usr/bin/env python3
"""Build the five-scene Axi360 baseline-versus-Ingredients ComfyUI job."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


SCENE_RE = re.compile(r"(?=SCENE\s+(\d+)\s+—)")
VIDEO_PROMPT_MARKER = "LTX-2.5 PRO VIDEO PROMPT"
SEPARATOR = "────────────────────────────────"
DELIVERY_DURATIONS = {1: 6.0, 2: 8.0, 3: 6.0, 4: 8.0, 5: 6.0}
GRAPH_DURATIONS = {1: 6.32, 2: 8.0, 3: 6.32, 4: 8.0, 5: 6.32}
EXPECTED_FRAMES = {1: 153, 2: 201, 3: 153, 4: 201, 5: 153}
NEGATIVE = (
    "text, letters, words, captions, subtitles, generated logo, watermark, signature, "
    "interface, UI, geographic map, extra people, additional characters, duplicated objects, "
    "malformed objects, extra limbs, deformed hands, distorted faces, inconsistent colors, "
    "style changes, camera shake, abrupt movement, flicker, morphing, melting objects, "
    "photorealism, dark lighting, clutter, cuts not requested by the prompt"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload:
        raise ValueError(f"expected a non-empty JSON object: {path}")
    return payload


def parse_source(text: str) -> tuple[str, dict[int, str]]:
    if SEPARATOR not in text:
        raise ValueError("prompt source is missing its section separator")
    global_style = text.split(SEPARATOR, 1)[0].strip()
    global_style = global_style.replace("GLOBAL VISUAL STYLE — USE FOR EVERY START FRAME", "").strip()
    sections = SCENE_RE.split(text)
    prompts: dict[int, str] = {}
    for index in range(1, len(sections), 2):
        scene = int(sections[index])
        body = sections[index + 1]
        if VIDEO_PROMPT_MARKER not in body:
            raise ValueError(f"scene {scene} is missing {VIDEO_PROMPT_MARKER!r}")
        prompt = body.split(VIDEO_PROMPT_MARKER, 1)[1].split(SEPARATOR, 1)[0].strip()
        prompts[scene] = f"{global_style}\n\n{prompt}"
    if set(prompts) != set(DELIVERY_DURATIONS):
        raise ValueError(f"expected scenes 1-5, found {sorted(prompts)}")
    return global_style, prompts


def remove(prompt: dict, *keys: str) -> None:
    for key in keys:
        prompt.pop(key, None)


def disable_api_and_enhancement(prompt: dict) -> None:
    for switch in ("5014:5558", "5014:5560"):
        if switch in prompt:
            prompt[switch]["inputs"]["on_true"] = prompt[switch]["inputs"]["on_false"]
    if "5014:5556" in prompt:
        prompt["5014:5556"]["inputs"]["switch"] = False
        prompt["5014:5556"]["inputs"]["on_true"] = prompt["5014:5556"]["inputs"]["on_false"]
    remove(prompt, "5014:5504", "5014:5505", "5014:5545", "5014:5546", "5014:5549", "5014:5555")


def disable_audio_output(prompt: dict) -> None:
    create_video = prompt["5518:4849"]["inputs"]
    create_video.pop("audio", None)


def build_baseline(template: dict, *, scene: int, positive: str, image: str,
                   output_prefix: str, seed: int) -> dict:
    prompt = json.loads(json.dumps(template))
    prompt["5508"]["inputs"]["value"] = positive
    prompt["5509"]["inputs"]["value"] = NEGATIVE
    prompt["2004"]["inputs"]["image"] = image
    prompt["5511"]["inputs"]["value"] = 25
    prompt["5512"]["inputs"]["value"] = GRAPH_DURATIONS[scene]
    prompt["5014:5506"]["inputs"]["value"] = True
    disable_api_and_enhancement(prompt)
    prompt["5514:3059"]["inputs"].update(width=544, height=960)
    prompt["5514:3159"]["inputs"]["strength"] = 0.7
    prompt["5516:4832"]["inputs"]["noise_seed"] = seed
    prompt["4852"]["inputs"]["filename_prefix"] = output_prefix
    disable_audio_output(prompt)
    return prompt


def build_ingredients(template: dict, *, scene: int, positive: str, image: str,
                      output_prefix: str, seed: int) -> dict:
    prompt = json.loads(json.dumps(template))
    prompt["5508"]["inputs"]["value"] = positive
    prompt["5509"]["inputs"]["value"] = NEGATIVE
    prompt["2004"]["inputs"]["image"] = image
    prompt["9007"]["inputs"]["value"] = 25
    prompt["9008"]["inputs"]["value"] = GRAPH_DURATIONS[scene]
    disable_api_and_enhancement(prompt)
    prompt["5004:5606"]["inputs"]["strength_model"] = 1.3
    prompt["5014:4990"]["inputs"].update(
        {"resize_type": "scale shorter dimension", "resize_type.shorter_size": 544}
    )
    prompt["5516:9017"]["inputs"]["noise_seed"] = seed
    prompt["4852"]["inputs"]["filename_prefix"] = output_prefix
    disable_audio_output(prompt)
    return prompt


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build(args: argparse.Namespace) -> dict:
    prompt_source = args.prompt_source.resolve()
    baseline_template = args.baseline_template.resolve()
    ingredients_template = args.ingredients_template.resolve()
    inputs_dir = args.inputs_dir.resolve()
    output_dir = args.output_dir.resolve()
    source_text = prompt_source.read_text(encoding="utf-8")
    _global_style, prompts = parse_source(source_text)
    baseline = load_json(baseline_template)
    ingredients = load_json(ingredients_template)

    outputs = []
    scenes = []
    for scene in range(1, 6):
        image_path = inputs_dir / f"scene-{scene:02d}-start-544x960.png"
        if not image_path.is_file():
            raise FileNotFoundError(image_path)
        remote_image = f"axi360-ab-scene-{scene:02d}.png"
        scene_record = {
            "scene": scene,
            "source_image": str(image_path),
            "source_image_sha256": sha256(image_path),
            "remote_image": remote_image,
            "requested_delivery_seconds": DELIVERY_DURATIONS[scene],
            "graph_duration_seconds": GRAPH_DURATIONS[scene],
            "expected_generated_frames": EXPECTED_FRAMES[scene],
            "expected_generated_seconds": EXPECTED_FRAMES[scene] / 25,
        }
        for arm, builder, template in (
            ("A-baseline", build_baseline, baseline),
            ("B-ingredients", build_ingredients, ingredients),
        ):
            slug = "baseline" if arm == "A-baseline" else "ingredients"
            prompt_path = output_dir / f"scene-{scene:02d}-{slug}.json"
            prefix = f"{args.job_id}/scene-{scene:02d}-{slug}"
            payload = builder(
                template,
                scene=scene,
                positive=prompts[scene],
                image=remote_image,
                output_prefix=prefix,
                seed=args.seed,
            )
            write_json(prompt_path, payload)
            outputs.append({
                "scene": scene,
                "arm": arm,
                "prompt_json": str(prompt_path),
                "prompt_sha256": sha256(prompt_path),
                "output_prefix": prefix,
            })
        scenes.append(scene_record)

    manifest = {
        "schema_version": 1,
        "job_id": args.job_id,
        "model": "LTX-2.5 distilled BF16",
        "ab_question": "Does Ingredients 1.3 improve reference preservation and control?",
        "ab_arms": {
            "A": "axi360_ingredients_ab_baseline",
            "B": "axi360_ingredients_ab_variant",
        },
        "locked": {
            "seed": args.seed,
            "fps": 25,
            "stage_1_resolution": [544, 960],
            "delivery_resolution": [1080, 1920],
            "audio_output": False,
            "prompt_enhance": False,
            "video_vae": "ltx-2.5-video-vae-bf16.safetensors",
        },
        "known_workflow_difference": (
            "B repeats the scene start frame as an Ingredients guide; this compares workflows, "
            "not an isolated generic quality improvement."
        ),
        "prompt_source": str(prompt_source),
        "prompt_source_sha256": sha256(prompt_source),
        "template_sources": {
            "baseline": {"path": str(baseline_template), "sha256": sha256(baseline_template)},
            "ingredients": {"path": str(ingredients_template), "sha256": sha256(ingredients_template)},
        },
        "scenes": scenes,
        "outputs": outputs,
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--job-id", required=True)
    result.add_argument("--prompt-source", type=Path, required=True)
    result.add_argument("--inputs-dir", type=Path, required=True)
    result.add_argument("--baseline-template", type=Path, required=True)
    result.add_argument("--ingredients-template", type=Path, required=True)
    result.add_argument("--output-dir", type=Path, required=True)
    result.add_argument("--seed", type=int, default=3602501)
    return result


if __name__ == "__main__":
    manifest = build(parser().parse_args())
    print(json.dumps({
        "status": "built",
        "job_id": manifest["job_id"],
        "scenes": len(manifest["scenes"]),
        "prompts": len(manifest["outputs"]),
    }))
