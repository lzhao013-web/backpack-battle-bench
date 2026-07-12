import json
import re
from pathlib import Path

import pytest

from backpack_bench.plugins import PluginRegistry
from backpack_bench.static_site import build_static_site, export_results_snapshot
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
