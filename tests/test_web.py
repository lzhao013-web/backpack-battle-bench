import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import HttpUrl

from backpack_bench.io import atomic_write_yaml
from backpack_bench.plugins import PluginRegistry
from backpack_bench.runner import dry_run_summary, resolve_plan
from backpack_bench.schemas import (
    ModelProfile,
    ModelsConfig,
    ProviderLimits,
    RunPlan,
)
from backpack_bench.web import create_app

ROOT = Path(__file__).resolve().parents[1]
REAL_ASYNC_CLIENT = httpx.AsyncClient


def openai_stream_response(answer: str, output_tokens: int = 20) -> httpx.Response:
    midpoint = max(1, len(answer) // 2)
    events = [
        {
            "id": "web-mock",
            "choices": [{"delta": {"content": answer[:midpoint]}, "finish_reason": None}],
        },
        {
            "id": "web-mock",
            "choices": [{"delta": {"content": answer[midpoint:]}, "finish_reason": None}],
        },
        {
            "id": "web-mock",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "web-mock",
            "choices": [],
            "usage": {"prompt_tokens": 100, "completion_tokens": output_tokens},
        },
    ]
    content = "\n\n".join(f"data: {json.dumps(event)}" for event in events)
    content += "\n\ndata: [DONE]\n\n"
    return httpx.Response(
        200,
        content=content,
        headers={"Content-Type": "text/event-stream"},
        request=httpx.Request("POST", "https://mock.test/stream"),
    )


class WebFakeAsyncClient:
    prompt_answers: dict[str, str] = {}
    requests: list[dict[str, Any]] = []

    def __init__(self, **_: object) -> None:
        pass

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        json: dict[str, Any],
    ) -> AsyncIterator[httpx.Response]:
        assert method == "POST"
        assert json["stream"] is True
        self.requests.append({"url": url, "headers": headers, "json": json})
        prompt = json["messages"][0]["content"]
        yield openai_stream_response(self.prompt_answers[prompt])

    async def aclose(self) -> None:
        pass


class WebSlowAsyncClient:
    calls = 0

    def __init__(self, **_: object) -> None:
        pass

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        json: dict[str, Any],
    ) -> AsyncIterator[httpx.Response]:
        WebSlowAsyncClient.calls += 1
        await asyncio.sleep(30)
        raise AssertionError("slow request should be cancelled")
        yield openai_stream_response("{}")

    async def aclose(self) -> None:
        pass


class GatedStreamResponse:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.status_code = 200
        self.headers = httpx.Headers({"Content-Type": "text/event-stream"})

    async def aiter_lines(self) -> AsyncIterator[str]:
        midpoint = max(1, len(self.answer) // 2)
        first = {
            "id": "live-token-mock",
            "choices": [{"delta": {"content": self.answer[:midpoint]}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(first)}"
        WebLiveTokenAsyncClient.first_chunk = True
        while not WebLiveTokenAsyncClient.release:
            await asyncio.sleep(0.01)
        events = [
            {
                "id": "live-token-mock",
                "choices": [{"delta": {"content": self.answer[midpoint:]}, "finish_reason": None}],
            },
            {
                "id": "live-token-mock",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
            {
                "id": "live-token-mock",
                "choices": [],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        ]
        for event in events:
            yield f"data: {json.dumps(event)}"
        yield "data: [DONE]"


class WebLiveTokenAsyncClient:
    prompt_answers: dict[str, str] = {}
    first_chunk = False
    release = False

    def __init__(self, **_: object) -> None:
        pass

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        json: dict[str, Any],
    ) -> AsyncIterator[httpx.Response]:
        prompt = json["messages"][0]["content"]
        yield cast(httpx.Response, GatedStreamResponse(self.prompt_answers[prompt]))

    async def aclose(self) -> None:
        pass


def test_web_scenario_lab_uses_real_validator() -> None:
    async def exercise() -> None:
        app = create_app(ROOT)
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as client,
        ):
            root = await client.get("/")
            assert root.status_code == 200
            assert "Backpack Battle Bench" in root.text
            script = (await client.get("/assets/app.js")).text
            styles = await client.get("/assets/styles.css")
            assert styles.status_code == 200
            assert 'addEventListener("pointerdown"' in script
            assert 'addEventListener("contextmenu"' in script
            assert "rotateActiveDrag" in script
            assert "function effectPreview" in script
            assert "renderEffectPreview" in script
            assert "ray_stat_bonus" in script
            assert ".board-cell.is-effect-range" in styles.text
            assert "效果范围" in root.text
            assert "API_HISTORY_STORAGE_KEY" in script
            assert "saveCurrentApiHistory" in script
            assert "受 run.yaml 全局并发" in script
            assert "async function stopRun" in script
            assert '$("#stop-run").addEventListener("click", stopRun)' in script
            assert "async function deleteRun" in script
            assert '$("#delete-run").addEventListener("click", deleteRun)' in script
            assert "delete-run" in root.text
            assert "function startRunStream" in script
            assert "new EventSource" in script
            assert "run-jobs-table" in root.text
            assert ".button-danger" in styles.text
            assert ".job-stream-wrap" in styles.text
            html_ids = set(re.findall(r'id="([^"]+)"', root.text))
            queried_ids = set(re.findall(r'\$\("#([^"]+)"\)', script))
            assert queried_ids <= html_ids
            suites = (await client.get("/api/suites")).json()
            assert {suite["id"] for suite in suites} == {
                "smoke-v1",
                "core-v1",
                "ladder-v1",
                "ladder-v2",
            }
            detail = (await client.get("/api/suites/smoke-v1/scenarios/mixed-3x3")).json()
            assert detail["oracle"]["optimal_attack"] == 21
            assert len(detail["instances"]) == 10
            sword = next(item for item in detail["instances"] if item["item_id"] == "iron_sword_1")
            gem = next(item for item in detail["instances"] if item["item_id"] == "gem_1")
            assert sword["category_label"] == "武器"
            assert sword["stats_zh"] == {"攻击": 5}
            assert "紧挨在它每个格子左侧和右侧" in gem["effect_descriptions"][0]
            assert "初始局部方向" not in gem["effect_descriptions"][0]
            assert "结算" not in gem["effect_descriptions"][0]
            assert "weapon" not in gem["effect_descriptions"][0]
            assert "attack" not in gem["effect_descriptions"][0]
            evaluation = (
                await client.post(
                    "/api/evaluate",
                    json={
                        "suite_id": "smoke-v1",
                        "scenario_id": "mixed-3x3",
                        "placements": detail["oracle"]["witness"]["placements"],
                    },
                )
            ).json()
            assert evaluation["valid"]
            assert evaluation["actual_attack"] == 21
            configs_text = (await client.get("/api/run-configs")).text
            assert "api_key_env" not in configs_text

    asyncio.run(exercise())


def test_all_scenarios_run_config_expands_to_120_jobs() -> None:
    plan = resolve_plan(
        ROOT / "configs" / "run.all.yaml",
        PluginRegistry(load_external=False),
    )
    summary = dry_run_summary(plan)
    assert summary["suite_id"] == "core-v1"
    assert summary["scenarios"] == 20
    assert summary["jobs"] == 120


def test_ladder_run_config_expands_to_30_jobs() -> None:
    plan = resolve_plan(
        ROOT / "configs" / "run.ladder.yaml",
        PluginRegistry(load_external=False),
    )
    summary = dry_run_summary(plan)
    assert summary["suite_id"] == "ladder-v1"
    assert summary["scenarios"] == 5
    assert summary["jobs"] == 30
    assert summary["concurrency"] == 5


def test_expanded_ladder_run_config_expands_to_90_jobs() -> None:
    plan = resolve_plan(
        ROOT / "configs" / "run.ladder-v2.yaml",
        PluginRegistry(load_external=False),
    )
    summary = dry_run_summary(plan)
    assert summary["suite_id"] == "ladder-v2"
    assert summary["scenarios"] == 15
    assert summary["jobs"] == 90
    assert summary["concurrency"] == 5


def test_web_can_start_and_report_mock_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "configs"
    models = ModelsConfig(
        profiles=[
            ModelProfile(
                id="web-mock",
                protocol="openai_chat",
                base_url=HttpUrl("https://mock.test/v1"),
                model="mock-model",
                auth_mode="none",
                limits=ProviderLimits(concurrency=2, retries=0),
            )
        ]
    )
    models_path = config_dir / "models.yaml"
    atomic_write_yaml(models_path, models)
    run = RunPlan(
        id="web-integration",
        suite=str(ROOT / "suites" / "smoke-v1.yaml"),
        models="models.yaml",
        trials=1,
        concurrency=2,
        database="../data/results.sqlite3",
        artifacts="../data/artifacts",
        reports="../data/reports",
    )
    run_path = config_dir / "run.yaml"
    atomic_write_yaml(run_path, run)
    resolved = resolve_plan(run_path, PluginRegistry(load_external=False))
    WebFakeAsyncClient.prompt_answers = {
        item.prompt: json.dumps(item.oracle.witness.model_dump(mode="json"), ensure_ascii=False)
        for item in resolved.suite.scenarios
        if item.oracle.witness is not None
    }
    WebFakeAsyncClient.requests = []
    monkeypatch.setattr("backpack_bench.runner.httpx.AsyncClient", WebFakeAsyncClient)

    async def exercise() -> None:
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as client,
        ):
            configs = (await client.get("/api/run-configs")).json()
            assert len(configs) == 1
            assert configs[0]["key_ready"]
            config_id = configs[0]["id"]
            started = await client.post(f"/api/run-configs/{config_id}/runs")
            assert started.status_code == 202
            run_id = started.json()["run_id"]

            payload: dict[str, Any] | None = None
            for _ in range(100):
                response = await client.get(f"/api/run-configs/{config_id}/runs/{run_id}")
                assert response.status_code == 200
                payload = response.json()
                if payload["status"] == "completed":
                    break
                await asyncio.sleep(0.02)
            assert payload is not None
            assert payload["status"] == "completed"
            assert payload["progress"]["total"] == 4
            assert payload["progress"]["completed"] == 4
            assert len(payload["jobs"]) == 4
            assert all(job["status"] == "completed" for job in payload["jobs"])
            assert all(job["output_tokens"] == 20 for job in payload["jobs"])
            assert all(not job["output_tokens_estimated"] for job in payload["jobs"])
            assert payload["report"]["profiles"][0]["overall_score"] == 100
            events = await client.get(f"/api/run-configs/{config_id}/runs/{run_id}/events")
            assert events.status_code == 200
            assert events.headers["content-type"].startswith("text/event-stream")
            data_line = next(line for line in events.text.splitlines() if line.startswith("data: "))
            snapshot = json.loads(data_line.removeprefix("data: "))
            assert snapshot["status"] == "completed"
            assert len(snapshot["jobs"]) == 4
            assert all(job["output_tokens"] == 20 for job in snapshot["jobs"])
            csv = await client.get(f"/api/run-configs/{config_id}/runs/{run_id}/report?format=csv")
            assert csv.status_code == 200
            html = await client.get(
                f"/api/run-configs/{config_id}/runs/{run_id}/report?format=html"
            )
            assert html.status_code == 200
            assert "<table>" in html.text
            assert "单题与 Trial 明细" in html.text
            assert "mixed-3x3" in html.text
            assert "验证明细" in html.text
            assert (tmp_path / "data" / "artifacts" / run_id).is_dir()
            assert (tmp_path / "data" / "reports" / run_id).is_dir()
            deleted = await client.delete(f"/api/run-configs/{config_id}/runs/{run_id}")
            assert deleted.status_code == 200
            assert deleted.json()["deleted"] is True
            assert not (tmp_path / "data" / "artifacts" / run_id).exists()
            assert not (tmp_path / "data" / "reports" / run_id).exists()
            assert (
                await client.get(f"/api/run-configs/{config_id}/runs/{run_id}")
            ).status_code == 404
            assert (await client.get(f"/api/run-configs/{config_id}/runs")).json() == []

    asyncio.run(exercise())


def test_web_can_interrupt_an_active_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "configs"
    models = ModelsConfig(
        profiles=[
            ModelProfile(
                id="web-slow",
                protocol="openai_chat",
                base_url=HttpUrl("https://slow.test/v1"),
                model="slow-model",
                auth_mode="none",
                limits=ProviderLimits(concurrency=2, retries=0),
            )
        ]
    )
    atomic_write_yaml(config_dir / "models.yaml", models)
    run = RunPlan(
        id="web-interrupt",
        suite=str(ROOT / "suites" / "smoke-v1.yaml"),
        models="models.yaml",
        trials=2,
        concurrency=2,
        database="../data/results.sqlite3",
        artifacts="../data/artifacts",
        reports="../data/reports",
    )
    atomic_write_yaml(config_dir / "run.yaml", run)
    WebSlowAsyncClient.calls = 0
    monkeypatch.setattr("backpack_bench.runner.httpx.AsyncClient", WebSlowAsyncClient)

    async def exercise() -> None:
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as client,
        ):
            config_id = (await client.get("/api/run-configs")).json()[0]["id"]
            started = await client.post(f"/api/run-configs/{config_id}/runs")
            assert started.status_code == 202
            run_id = started.json()["run_id"]
            for _ in range(100):
                if WebSlowAsyncClient.calls:
                    break
                await asyncio.sleep(0.01)
            assert WebSlowAsyncClient.calls > 0

            active_delete = await client.delete(f"/api/run-configs/{config_id}/runs/{run_id}")
            assert active_delete.status_code == 409

            stream_task = asyncio.create_task(
                client.get(f"/api/run-configs/{config_id}/runs/{run_id}/events")
            )
            await asyncio.sleep(0.05)
            stopped = await client.post(f"/api/run-configs/{config_id}/runs/{run_id}/stop")
            assert stopped.status_code == 202
            assert stopped.json()["status"] == "interrupted"
            stream = await asyncio.wait_for(stream_task, timeout=2)
            snapshots = [
                json.loads(line.removeprefix("data: "))
                for line in stream.text.splitlines()
                if line.startswith("data: ")
            ]
            assert snapshots[0]["status"] == "running"
            assert snapshots[0]["progress"]["running"] > 0
            assert snapshots[-1]["status"] == "interrupted"

            payload = (await client.get(f"/api/run-configs/{config_id}/runs/{run_id}")).json()
            assert payload["status"] == "interrupted"
            assert payload["progress"]["running"] == 0
            assert payload["progress"]["pending"] == payload["progress"]["total"]
            assert len(payload["jobs"]) == 8
            assert all(job["status"] == "pending" for job in payload["jobs"])
            assert all(job["output_tokens"] is None for job in payload["jobs"])
            assert payload["report"] is not None
            assert (tmp_path / "data" / "reports" / run_id / "report.html").is_file()

            stopped_again = await client.post(f"/api/run-configs/{config_id}/runs/{run_id}/stop")
            assert stopped_again.status_code == 409

    asyncio.run(exercise())


def test_web_streams_live_output_token_estimates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "configs"
    models = ModelsConfig(
        profiles=[
            ModelProfile(
                id="live-token-model",
                protocol="openai_chat",
                base_url=HttpUrl("https://live.test/v1"),
                model="live-model",
                auth_mode="none",
                limits=ProviderLimits(concurrency=1, retries=0),
            )
        ]
    )
    atomic_write_yaml(config_dir / "models.yaml", models)
    run = RunPlan(
        id="web-live-tokens",
        suite=str(ROOT / "suites" / "smoke-v1.yaml"),
        models="models.yaml",
        trials=1,
        concurrency=1,
        database="../data/results.sqlite3",
        artifacts="../data/artifacts",
        reports="../data/reports",
    )
    run_path = config_dir / "run.yaml"
    atomic_write_yaml(run_path, run)
    resolved = resolve_plan(run_path, PluginRegistry(load_external=False))
    WebLiveTokenAsyncClient.prompt_answers = {
        item.prompt: json.dumps(item.oracle.witness.model_dump(mode="json"), ensure_ascii=False)
        for item in resolved.suite.scenarios
        if item.oracle.witness is not None
    }
    WebLiveTokenAsyncClient.first_chunk = False
    WebLiveTokenAsyncClient.release = False
    monkeypatch.setattr("backpack_bench.runner.httpx.AsyncClient", WebLiveTokenAsyncClient)

    async def exercise() -> None:
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as client,
        ):
            config_id = (await client.get("/api/run-configs")).json()[0]["id"]
            started = await client.post(f"/api/run-configs/{config_id}/runs")
            run_id = started.json()["run_id"]
            for _ in range(100):
                if WebLiveTokenAsyncClient.first_chunk:
                    break
                await asyncio.sleep(0.01)
            if not WebLiveTokenAsyncClient.first_chunk:
                WebLiveTokenAsyncClient.release = True
            assert WebLiveTokenAsyncClient.first_chunk

            live_payload: dict[str, Any] | None = None
            for _ in range(100):
                live_payload = (
                    await client.get(f"/api/run-configs/{config_id}/runs/{run_id}")
                ).json()
                running = [job for job in live_payload["jobs"] if job["status"] == "running"]
                if running and running[0]["output_tokens"]:
                    break
                await asyncio.sleep(0.01)
            assert live_payload is not None
            running = [job for job in live_payload["jobs"] if job["status"] == "running"]
            if not running or not running[0]["output_tokens"]:
                WebLiveTokenAsyncClient.release = True
            assert running[0]["output_tokens"] > 0
            if running[0]["output_tokens_estimated"] is not True:
                WebLiveTokenAsyncClient.release = True
            assert running[0]["output_tokens_estimated"] is True

            stream_task = asyncio.create_task(
                client.get(f"/api/run-configs/{config_id}/runs/{run_id}/events")
            )
            await asyncio.sleep(0.3)
            WebLiveTokenAsyncClient.release = True
            stream = await asyncio.wait_for(stream_task, timeout=3)
            snapshots = [
                json.loads(line.removeprefix("data: "))
                for line in stream.text.splitlines()
                if line.startswith("data: ")
            ]
            assert any(
                job["output_tokens_estimated"] and job["output_tokens"] > 0
                for snapshot in snapshots
                for job in snapshot["jobs"]
                if job["status"] == "running"
            )
            assert snapshots[-1]["status"] == "completed"
            assert all(
                job["output_tokens"] == 20 and not job["output_tokens_estimated"]
                for job in snapshots[-1]["jobs"]
            )

    asyncio.run(exercise())


def test_web_runtime_api_profile_uses_ephemeral_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "configs"
    models = ModelsConfig(
        profiles=[
            ModelProfile(
                id="unused-static-profile",
                protocol="openai_chat",
                base_url=HttpUrl("https://unused.test/v1"),
                model="unused-model",
                auth_mode="none",
                limits=ProviderLimits(retries=0),
            )
        ]
    )
    models_path = config_dir / "models.yaml"
    atomic_write_yaml(models_path, models)
    run = RunPlan(
        id="web-runtime-profile",
        suite=str(ROOT / "suites" / "smoke-v1.yaml"),
        models="models.yaml",
        trials=1,
        database="../data/results.sqlite3",
        artifacts="../data/artifacts",
        reports="../data/reports",
    )
    run_path = config_dir / "run.yaml"
    atomic_write_yaml(run_path, run)
    resolved = resolve_plan(run_path, PluginRegistry(load_external=False))
    WebFakeAsyncClient.prompt_answers = {
        item.prompt: json.dumps(item.oracle.witness.model_dump(mode="json"), ensure_ascii=False)
        for item in resolved.suite.scenarios
        if item.oracle.witness is not None
    }
    WebFakeAsyncClient.requests = []
    monkeypatch.setattr("backpack_bench.runner.httpx.AsyncClient", WebFakeAsyncClient)
    secret = "browser-only-test-secret"

    async def exercise() -> None:
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as client,
        ):
            config_id = (await client.get("/api/run-configs")).json()[0]["id"]
            missing_key = await client.post(
                f"/api/run-configs/{config_id}/runs",
                json={
                    "profile": {
                        "protocol": "openai_chat",
                        "base_url": "https://runtime.test/v1",
                        "model": "runtime-reasoner",
                    }
                },
            )
            assert missing_key.status_code == 400
            assert "API Key" in missing_key.text
            started = await client.post(
                f"/api/run-configs/{config_id}/runs",
                json={
                    "profile": {
                        "display_name": "Browser Reasoner",
                        "protocol": "openai_chat",
                        "base_url": "https://runtime.test/v1",
                        "model": "runtime-reasoner",
                        "api_key": secret,
                        "params": {"thinking_effort": "high"},
                        "limits": {"concurrency": 2, "retries": 0},
                    }
                },
            )
            assert started.status_code == 202
            run_id = started.json()["run_id"]
            payload: dict[str, Any] | None = None
            for _ in range(100):
                payload = (await client.get(f"/api/run-configs/{config_id}/runs/{run_id}")).json()
                if payload["status"] == "completed":
                    break
                await asyncio.sleep(0.02)
            assert payload is not None
            assert payload["status"] == "completed"
            assert payload["progress"]["total"] == 4
            assert payload["profiles"][0]["model"] == "runtime-reasoner"
            assert secret not in json.dumps(payload)
            assert "api_key_env" not in json.dumps(payload)
            listed = (await client.get(f"/api/run-configs/{config_id}/runs")).json()
            assert listed[0]["run_id"] == run_id
            assert listed[0]["profiles"][0]["model"] == "runtime-reasoner"

    asyncio.run(exercise())
    assert WebFakeAsyncClient.requests
    assert all(
        request["headers"]["Authorization"] == f"Bearer {secret}"
        for request in WebFakeAsyncClient.requests
    )
    assert all(
        request["json"]["reasoning_effort"] == "high" for request in WebFakeAsyncClient.requests
    )
    assert all("max_tokens" not in request["json"] for request in WebFakeAsyncClient.requests)
    for path in (tmp_path / "data").rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
