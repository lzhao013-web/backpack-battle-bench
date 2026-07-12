"""Build the standalone GitHub Pages leaderboard and public result snapshots."""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from PIL import Image

from backpack_bench.io import atomic_write_json, atomic_write_text
from backpack_bench.plugins import PluginRegistry
from backpack_bench.prompt import render_visual_prompt
from backpack_bench.reporting import build_run_report
from backpack_bench.storage import Storage
from backpack_bench.suite import ResolvedSuite, load_suite

TRACKS = ("text", "visual_shape", "visual_full")
VISUAL_MODES: tuple[Literal["visual_shape", "visual_full"], ...] = (
    "visual_shape",
    "visual_full",
)
SNAPSHOT_VERSION = 1
SITE_DATA_VERSION = 2


def _public_scenario_result(value: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "scenario_id",
        "title",
        "difficulty",
        "tags",
        "weight",
        "oracle_attack",
        "trials",
        "valid_rate",
        "attack_mean",
        "attack_best",
        "attack_worst",
        "attack_stddev",
        "ratio_mean",
        "ratio_best_of_3",
        "optimal_hit_rate",
        "error_counts",
    )
    return {key: value[key] for key in keys}


def _public_profile_result(
    profile: dict[str, Any],
    run: dict[str, Any],
    trials: int,
    expected_scenarios: int,
) -> dict[str, Any]:
    reasons: list[str] = []
    expected_jobs = expected_scenarios * trials
    if trials < 3:
        reasons.append("每道题少于 3 次 Trial")
    scenario_results = profile.get("scenarios", [])
    if len(scenario_results) != expected_scenarios:
        reasons.append("未覆盖当前题集全部题目")
    if int(profile["jobs"]) != expected_jobs:
        reasons.append("Job 数量不完整")
    if any(int(item["trials"]) != trials for item in scenario_results):
        reasons.append("各题 Trial 数量不一致")
    aggregate_keys = (
        "scenarios",
        "jobs",
        "overall_score",
        "best_of_3_score",
        "valid_rate",
        "optimal_hit_rate",
        "attack_mean",
        "attack_best",
        "attack_worst",
        "attack_stddev",
        "latency_p50_ms",
        "latency_p95_ms",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "estimated_cost",
        "retry_rate",
        "truncation_rate",
        "error_counts",
    )
    return {
        **{
            key: len(scenario_results) if key == "scenarios" else profile[key]
            for key in aggregate_keys
        },
        "profile_hash": profile["profile_hash"],
        "profile_id": profile["profile_id"],
        "display_name": profile["display_name"],
        "protocol": profile["protocol"],
        "model": profile["model"],
        "thinking_effort": profile["thinking_effort"],
        "run_id": run["run_id"],
        "plan_id": run["plan_id"],
        "started_at": run["started_at"],
        "completed_at": run["completed_at"],
        "trials": trials,
        "eligible": not reasons,
        "eligibility_reasons": reasons,
        "groups": profile["groups"],
        "scenario_results": [_public_scenario_result(item) for item in scenario_results],
    }


def _select_latest_profiles(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates.sort(key=lambda item: str(item.get("completed_at") or ""), reverse=True)
    latest_any: dict[str, dict[str, Any]] = {}
    latest_eligible: dict[str, dict[str, Any]] = {}
    for entry in candidates:
        key = str(entry["profile_hash"])
        latest_any.setdefault(key, entry)
        if entry["eligible"]:
            latest_eligible.setdefault(key, entry)
    selected = [latest_eligible.get(key, entry) for key, entry in latest_any.items()]
    selected.sort(
        key=lambda item: (
            not bool(item["eligible"]),
            -float(item["overall_score"]),
            -float(item["valid_rate"]),
            -float(item["optimal_hit_rate"]),
            str(item["profile_hash"]),
        )
    )
    rank = 0
    for entry in selected:
        if entry["eligible"]:
            rank += 1
            entry["official_rank"] = rank
        else:
            entry["official_rank"] = None
    return selected


def _load_public_suites(workspace: Path, registry: PluginRegistry) -> list[ResolvedSuite]:
    return [
        load_suite(path, registry, verify=True)
        for path in sorted((workspace / "suites").glob("*.yaml"))
    ]


def export_results_snapshot(workspace: Path, database: Path, output: Path) -> Path:
    """Export aggregate-only leaderboard data; model outputs and credentials stay private."""
    workspace = workspace.resolve()
    registry = PluginRegistry()
    suites = _load_public_suites(workspace, registry)
    tracks: list[dict[str, Any]] = []
    storage = Storage(database.resolve())
    try:
        for suite in suites:
            for prompt_mode in TRACKS:
                benchmark_id = (
                    suite.spec.id if prompt_mode == "text" else f"{suite.spec.id}@{prompt_mode}"
                )
                candidates: list[dict[str, Any]] = []
                for run in storage.latest_completed_runs(benchmark_id):
                    if str(run["suite_hash"]) != suite.suite_hash:
                        continue
                    try:
                        config = json.loads(str(run["config_json"]))
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(config, dict):
                        continue
                    trials = int(config.get("trials", 1) or 1)
                    report = build_run_report(storage, str(run["run_id"]))
                    candidates.extend(
                        _public_profile_result(
                            profile,
                            run,
                            trials,
                            len(suite.scenarios),
                        )
                        for profile in report["profiles"]
                    )
                tracks.append(
                    {
                        "suite_id": suite.spec.id,
                        "suite_hash": suite.suite_hash,
                        "prompt_mode": prompt_mode,
                        "entries": _select_latest_profiles(candidates),
                    }
                )
    finally:
        storage.close()
    atomic_write_json(output.resolve(), {"schema_version": SNAPSHOT_VERSION, "tracks": tracks})
    return output.resolve()


def _copy_asset(source: Path, output_root: Path, relative: str) -> str:
    destination = output_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return relative.replace("\\", "/")


def _copy_rotated_item_assets(
    source: Path,
    output_root: Path,
    asset_root: str,
    item_id: str,
    rotations: Sequence[int],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for rotation in rotations:
        relative = f"{asset_root}/items/{item_id}-{rotation}.png"
        destination = output_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if rotation == 0:
            shutil.copy2(source, destination)
        else:
            transpose = {
                90: Image.Transpose.ROTATE_270,
                180: Image.Transpose.ROTATE_180,
                270: Image.Transpose.ROTATE_90,
            }[rotation]
            with Image.open(source) as image:
                image.convert("RGBA").transpose(transpose).save(
                    destination,
                    format="PNG",
                    optimize=True,
                )
        result[str(rotation)] = relative.replace("\\", "/")
    return result


def _suite_site_data(
    suite: ResolvedSuite,
    output_root: Path,
    registry: PluginRegistry,
) -> dict[str, Any]:
    asset_root = f"assets/{suite.spec.id}/{suite.visual_pack.pack_hash[:12]}"
    item_paths: dict[str, dict[str, str]] = {}
    used_item_ids = {
        str(item.catalog_id)
        for resolved in suite.scenarios
        for item in resolved.scenario.items
        if item.catalog_id is not None
    }
    for item in suite.catalog.items:
        if item.id not in used_item_ids:
            continue
        _, source = suite.visual_pack.asset(item.id)
        item_paths[item.id] = _copy_rotated_item_assets(
            source,
            output_root,
            asset_root,
            item.id,
            item.rotations,
        )
    scenarios: list[dict[str, Any]] = []
    for resolved in suite.scenarios:
        scenario = resolved.scenario
        text_prompt_path = f"{asset_root}/scenarios/text/{scenario.id}.txt"
        atomic_write_text(output_root / text_prompt_path, resolved.prompt + "\n")
        visual_prompts = {
            mode: render_visual_prompt(scenario, registry, mode) for mode in VISUAL_MODES
        }
        visual_prompt_urls: dict[str, str] = {}
        for mode, prompt in visual_prompts.items():
            prompt_path = f"{asset_root}/scenarios/{mode}/{scenario.id}.txt"
            atomic_write_text(output_root / prompt_path, prompt + "\n")
            visual_prompt_urls[mode] = prompt_path
        sheets = {
            mode: _copy_asset(
                suite.visual_pack.scenario_sheet(
                    scenario.id,
                    resolved.entry.scenario_hash,
                    mode,
                ),
                output_root,
                f"{asset_root}/scenarios/{mode}/{scenario.id}.png",
            )
            for mode in VISUAL_MODES
        }
        instances = [
            {
                "item_id": item_id,
                "type_id": item.id,
                "catalog_id": item.catalog_id,
                "display_name": item.display_name,
                "category": item.category,
                "shape": item.shape,
                "rotations": item.rotations,
                "stats": item.stats,
                "effects": [effect.model_dump(mode="json") for effect in item.effects],
                "images": item_paths[str(item.catalog_id)],
            }
            for item_id, item in scenario.expanded_item_ids().items()
        ]
        scenarios.append(
            {
                "id": scenario.id,
                "title": scenario.title,
                "difficulty": scenario.difficulty,
                "tags": scenario.tags,
                "board": scenario.board.model_dump(mode="json", exclude_none=True),
                "valid_cells": [list(cell) for cell in sorted(scenario.board.valid_cells())],
                "instances": instances,
                "objective": scenario.objective.model_dump(mode="json"),
                "oracle_attack": int(resolved.oracle.optimal_attack or 0),
                "oracle_witness": (
                    resolved.oracle.witness.model_dump(mode="json")
                    if resolved.oracle.witness is not None
                    else None
                ),
                "scenario_hash": resolved.entry.scenario_hash,
                "text_prompt": resolved.prompt,
                "text_prompt_url": text_prompt_path,
                "visual_prompts": visual_prompts,
                "visual_prompt_urls": visual_prompt_urls,
                "sheets": sheets,
            }
        )
    return {
        "id": suite.spec.id,
        "title": suite.spec.title,
        "version": suite.spec.version,
        "suite_hash": suite.suite_hash,
        "visual_pack": {
            "id": suite.visual_pack.spec.id,
            "hash": suite.visual_pack.pack_hash,
            "status": suite.visual_pack.spec.status,
        },
        "scenarios": scenarios,
    }


def build_static_site(
    workspace: Path,
    output: Path,
    snapshot: Path | None = None,
) -> Path:
    """Build a backend-free directory suitable for GitHub Pages."""
    workspace = workspace.resolve()
    output = output.resolve()
    source = Path(__file__).with_name("leaderboard_static")
    output.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "app.js", "styles.css"):
        shutil.copy2(source / name, output / name)
    registry = PluginRegistry()
    suites = [
        _suite_site_data(suite, output, registry)
        for suite in _load_public_suites(workspace, registry)
    ]
    snapshot_path = (snapshot or workspace / "leaderboard" / "results.json").resolve()
    if snapshot_path.is_file():
        result_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    else:
        result_data = {"schema_version": SNAPSHOT_VERSION, "tracks": []}
    current_hashes = {suite["id"]: suite["suite_hash"] for suite in suites}
    tracks = [
        track
        for track in result_data.get("tracks", [])
        if current_hashes.get(track.get("suite_id")) == track.get("suite_hash")
    ]
    atomic_write_json(
        output / "data.json",
        {
            "schema_version": SITE_DATA_VERSION,
            "suites": suites,
            "leaderboard_tracks": tracks,
            "eligibility_policy": {
                "minimum_trials": 3,
                "complete_suite": True,
                "current_suite_hash_only": True,
                "official_metric": "overall_score",
            },
        },
    )
    return output
