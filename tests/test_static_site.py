import json
import re
from pathlib import Path

import pytest

from backpack_bench.io import atomic_write_json
from backpack_bench.plugins import PluginRegistry
from backpack_bench.static_site import (
    aggregate_run_snapshots,
    build_static_site,
    export_results_snapshot,
)
from backpack_bench.suite import load_suite

ROOT = Path(__file__).resolve().parents[1]


def test_static_leaderboard_builds_without_the_web_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = load_suite(ROOT / "suites" / "smoke-v1.yaml", PluginRegistry(), verify=True)
    monkeypatch.setattr(
        "backpack_bench.static_site._load_public_suites",
        lambda _workspace, _registry: [smoke],
    )
    snapshot = tmp_path / "results.json"
    snapshot.write_text('{"schema_version":1,"tracks":[]}', encoding="utf-8")
    output = build_static_site(ROOT, tmp_path / "pages", snapshot)
    index = (output / "index.html").read_text(encoding="utf-8")
    script = (output / "app.js").read_text(encoding="utf-8")
    data = json.loads((output / "data.json").read_text(encoding="utf-8"))

    assert "二维空间理解排行榜" in index
    assert "全部题目预览与试玩" in index
    assert "官方排名" not in index
    assert 'data-sort="overall_score"' in index
    assert 'data-sort="completed_at"' in index
    assert "function openPlayground" in script
    assert "function evaluatePlay" in script
    assert 'fetch("./data.json")' in script
    html_ids = set(re.findall(r'id="([^"]+)"', index))
    queried_ids = set(re.findall(r'(?<!\$)\$\("#([^"]+)"\)', script))
    assert queried_ids <= html_ids
    assert [(suite["id"], len(suite["scenarios"])) for suite in data["suites"]] == [("smoke-v1", 1)]
    scenario = data["suites"][0]["scenarios"][0]
    assert scenario["oracle_attack"] == 18
    assert scenario["oracle_witness"]["placements"]
    assert (output / scenario["sheets"]["visual_full"]).is_file()
    assert "你正在完成一个纯文字二维空间规划题" in scenario["text_prompt"]
    assert (
        (output / scenario["text_prompt_url"])
        .read_text(encoding="utf-8")
        .startswith(scenario["text_prompt"])
    )
    assert set(scenario["visual_prompts"]) == {"visual_shape", "visual_full"}
    for mode, prompt in scenario["visual_prompts"].items():
        assert "你正在完成一个视觉二维空间规划题" in prompt
        assert (
            (output / scenario["visual_prompt_urls"][mode])
            .read_text(encoding="utf-8")
            .startswith(prompt)
        )
    assert "selected.visual_prompts?.[mode]" in script
    assert "与图片一起发送的文字提示词" in script
    for instance in scenario["instances"]:
        assert all((output / path).is_file() for path in instance["images"].values())


def test_empty_public_snapshot_has_all_current_tracks(tmp_path: Path) -> None:
    output = export_results_snapshot(
        ROOT,
        tmp_path / "results.sqlite3",
        tmp_path / "results.json",
    )
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    assert snapshot["schema_version"] == 1
    assert len(snapshot["tracks"]) == 6
    assert all(track["entries"] == [] for track in snapshot["tracks"])
    assert "api_key" not in output.read_text(encoding="utf-8").lower()


def _public_entry(
    profile_hash: str,
    run_id: str,
    completed_at: str,
    score: float,
) -> dict[str, object]:
    return {
        "profile_hash": profile_hash,
        "run_id": run_id,
        "completed_at": completed_at,
        "eligible": True,
        "overall_score": score,
        "valid_rate": 1.0,
        "optimal_hit_rate": score,
    }


def _run_snapshot(
    smoke_hash: str,
    run_id: str,
    entry: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "suite_id": "smoke-v1",
        "suite_hash": smoke_hash,
        "prompt_mode": "text",
        "entries": [entry],
    }


def test_aggregate_keeps_runs_from_multiple_machines_and_legacy_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = load_suite(ROOT / "suites" / "smoke-v1.yaml", PluginRegistry(), verify=True)
    monkeypatch.setattr(
        "backpack_bench.static_site._load_public_suites",
        lambda _workspace, _registry: [smoke],
    )
    machine_a = tmp_path / "machine-a"
    machine_b = tmp_path / "machine-b"
    baseline = tmp_path / "legacy.json"
    atomic_write_json(
        baseline,
        {
            "schema_version": 1,
            "tracks": [
                {
                    "suite_id": "smoke-v1",
                    "suite_hash": smoke.suite_hash,
                    "prompt_mode": "text",
                    "entries": [_public_entry("legacy-profile", "legacy-run", "2026-07-10", 0.4)],
                }
            ],
        },
    )
    run_a = "20260711T000000Z_machine_a"
    run_b = "20260712T000000Z_machine_b"
    atomic_write_json(
        machine_a / f"{run_a}.json",
        _run_snapshot(
            smoke.suite_hash,
            run_a,
            _public_entry("profile-a", run_a, "2026-07-11", 0.7),
        ),
    )
    atomic_write_json(
        machine_b / f"{run_b}.json",
        _run_snapshot(
            smoke.suite_hash,
            run_b,
            _public_entry("profile-b", run_b, "2026-07-12", 0.9),
        ),
    )

    output = aggregate_run_snapshots(
        ROOT,
        [machine_a, machine_b],
        tmp_path / "aggregated.json",
        baseline,
    )
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    text_track = next(track for track in snapshot["tracks"] if track["prompt_mode"] == "text")

    assert [entry["run_id"] for entry in text_track["entries"]] == [
        run_b,
        run_a,
        "legacy-run",
    ]
    assert [entry["official_rank"] for entry in text_track["entries"]] == [1, 2, 3]


def test_aggregate_uses_the_latest_eligible_run_for_each_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = load_suite(ROOT / "suites" / "smoke-v1.yaml", PluginRegistry(), verify=True)
    monkeypatch.setattr(
        "backpack_bench.static_site._load_public_suites",
        lambda _workspace, _registry: [smoke],
    )
    runs = tmp_path / "runs"
    old_run = "20260711T000000Z_old"
    new_run = "20260712T000000Z_new"
    atomic_write_json(
        runs / f"{old_run}.json",
        _run_snapshot(
            smoke.suite_hash,
            old_run,
            _public_entry("same-profile", old_run, "2026-07-11", 0.9),
        ),
    )
    atomic_write_json(
        runs / f"{new_run}.json",
        _run_snapshot(
            smoke.suite_hash,
            new_run,
            _public_entry("same-profile", new_run, "2026-07-12", 0.8),
        ),
    )

    output = aggregate_run_snapshots(ROOT, [runs], tmp_path / "aggregated.json")
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    text_track = next(track for track in snapshot["tracks"] if track["prompt_mode"] == "text")

    assert len(text_track["entries"]) == 1
    assert text_track["entries"][0]["run_id"] == new_run
