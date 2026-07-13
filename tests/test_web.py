import asyncio
import io
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from PIL import Image
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
from backpack_bench.storage import Storage
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


class WebRerunAsyncClient:
    prompt_answers: dict[str, str] = {}
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
        WebRerunAsyncClient.calls += 1
        prompt = json["messages"][0]["content"]
        answer = "not-json" if WebRerunAsyncClient.calls == 1 else self.prompt_answers[prompt]
        yield openai_stream_response(answer)

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


class HeartbeatStreamResponse:
    status_code = 200
    headers = httpx.Headers({"Content-Type": "text/event-stream"})

    async def aiter_lines(self) -> AsyncIterator[str]:
        while True:
            yield ": heartbeat"
            await asyncio.sleep(0.01)


class WebHeartbeatAsyncClient:
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
        WebHeartbeatAsyncClient.calls += 1
        yield cast(httpx.Response, HeartbeatStreamResponse())

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
            assert '<option value="xhigh">xhigh</option>' in root.text
            assert '<option value="text">纯文字</option>' in root.text
            assert 'id="text-prompt-panel"' in root.text
            assert "查看发送给模型的完整提示词" not in root.text
            assert '<input id="api-json-mode" type="checkbox" checked>' in root.text
            script = (await client.get("/assets/app.js")).text
            styles = await client.get("/assets/styles.css")
            assert styles.status_code == 200
            assert 'addEventListener("pointerdown"' in script
            assert 'addEventListener("contextmenu"' in script
            assert "rotateActiveDrag" in script
            assert "function effectPreview" in script
            assert "function itemImageUrl" in script
            assert "cell-item-image-viewport" in script
            assert "itemImageUrl(instance, rotation)" in script
            assert "renderEffectPreview" in script
            assert "ray_stat_bonus" in script
            assert ".board-cell.is-effect-range" in styles.text
            assert ".cell-item-image-viewport" in styles.text
            assert ".drag-ghost img" in styles.text
            assert "效果范围" in root.text
            assert "单次请求总超时（秒）" in root.text
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
            assert "Best-of-3" in root.text
            assert "<th>得分</th><th>耗时</th>" in root.text
            assert ".button-danger" in styles.text
            assert ".job-stream-wrap" in styles.text
            html_ids = set(re.findall(r'id="([^"]+)"', root.text))
            queried_ids = set(re.findall(r'\$\("#([^"]+)"\)', script))
            assert queried_ids <= html_ids
            suites = (await client.get("/api/suites")).json()
            assert {suite["id"] for suite in suites} == {"smoke-v1", "ladder-v2"}
            statuses = {suite["id"]: suite["visual_pack"]["status"] for suite in suites}
            assert statuses == {"smoke-v1": "placeholder", "ladder-v2": "final"}
            detail = (await client.get("/api/suites/smoke-v1/scenarios/packing-3x3")).json()
            assert "占用格偏移" not in detail["visual_prompts"]["visual_full"]
            assert "每种物品的占用格、允许旋转、类别" in detail["visual_prompts"]["visual_full"]
            assert "类别：武器；基础属性：攻击=6" in detail["visual_prompts"]["visual_shape"]
            assert detail["oracle"]["optimal_attack"] == 18
            assert len(detail["instances"]) == 8
            sword = next(item for item in detail["instances"] if item["item_id"] == "great_blade_1")
            assert sword["category_label"] == "武器"
            assert sword["stats_zh"] == {"攻击": 6}
            assert sword["catalog_id"] == "great_blade"
            assert sword["image_url"].endswith("/items/great_blade/image")
            image = await client.get(sword["image_url"])
            assert image.status_code == 200
            assert image.content.startswith(b"\x89PNG")
            rotated_image = await client.get(f"{sword['image_url']}?rotation=90")
            assert rotated_image.status_code == 200
            with Image.open(io.BytesIO(image.content)) as original:
                original_size = original.size
            with Image.open(io.BytesIO(rotated_image.content)) as rotated:
                assert rotated.size == original_size[::-1]
            assert (await client.get(f"{sword['image_url']}?rotation=180")).status_code == 400
            card = await client.get(sword["card_url"])
            assert card.status_code == 200
            assert card.content.startswith(b"\x89PNG")
            sheet = await client.get(detail["sheet_url"])
            assert sheet.status_code == 200
            assert sheet.content.startswith(b"\x89PNG")
            evaluation = (
                await client.post(
                    "/api/evaluate",
                    json={
                        "suite_id": "smoke-v1",
                        "scenario_id": "packing-3x3",
                        "placements": detail["oracle"]["witness"]["placements"],
                    },
                )
            ).json()
            assert evaluation["valid"]
            assert evaluation["actual_attack"] == 18
            configs_text = (await client.get("/api/run-configs")).text
            assert "api_key_env" not in configs_text

    asyncio.run(exercise())


def test_expanded_ladder_run_config_expands_to_90_jobs() -> None:
    plan = resolve_plan(
        ROOT / "configs" / "run.ladder-v2.yaml",
        PluginRegistry(load_external=False),
    )
    summary = dry_run_summary(plan)
    assert summary["suite_id"] == "ladder-v2"
    assert summary["scenarios"] == 15
    assert summary["jobs"] == 90
    assert summary["concurrency"] == 10


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
            assert payload["progress"]["total"] == 1
            assert payload["progress"]["completed"] == 1
            assert len(payload["jobs"]) == 1
            assert all(job["status"] == "completed" for job in payload["jobs"])
            assert all(job["output_tokens"] > 20 for job in payload["jobs"])
            assert all(job["output_tokens_estimated"] for job in payload["jobs"])
            assert all(job["actual_attack"] == job["oracle_attack"] for job in payload["jobs"])
            assert all(job["latency_ms"] >= 0 for job in payload["jobs"])
            assert payload["report"]["profiles"][0]["overall_score"] == 100
            assert payload["report"]["profiles"][0]["best_of_3_score"] == 100
            events = await client.get(f"/api/run-configs/{config_id}/runs/{run_id}/events")
            assert events.status_code == 200
            assert events.headers["content-type"].startswith("text/event-stream")
            data_line = next(line for line in events.text.splitlines() if line.startswith("data: "))
            snapshot = json.loads(data_line.removeprefix("data: "))
            assert snapshot["status"] == "completed"
            assert len(snapshot["jobs"]) == 1
            assert all(job["output_tokens"] > 20 for job in snapshot["jobs"])
            summary_path = next(
                (tmp_path / "data" / "artifacts" / run_id).glob("*/attempt_001/summary.json")
            )
            usage = json.loads(summary_path.read_text(encoding="utf-8"))["usage"]
            assert usage["api_output_tokens"] == 20
            assert usage["stream_output_tokens_peak"] > usage["api_output_tokens"]
            assert usage["completion_tokens"] == usage["stream_output_tokens_peak"]
            csv = await client.get(f"/api/run-configs/{config_id}/runs/{run_id}/report?format=csv")
            assert csv.status_code == 200
            html = await client.get(
                f"/api/run-configs/{config_id}/runs/{run_id}/report?format=html"
            )
            assert html.status_code == 200
            assert "<table>" in html.text
            assert "单题与 Trial 明细" in html.text
            assert "packing-3x3" in html.text
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
            assert len(payload["jobs"]) == 2
            assert all(job["status"] == "pending" for job in payload["jobs"])
            assert all(job["output_tokens"] is None for job in payload["jobs"])
            assert payload["report"] is not None
            assert (tmp_path / "data" / "reports" / run_id / "report.html").is_file()

            stopped_again = await client.post(f"/api/run-configs/{config_id}/runs/{run_id}/stop")
            assert stopped_again.status_code == 409

    asyncio.run(exercise())


def test_web_can_run_multiple_benchmarks_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "configs"
    models = ModelsConfig(
        profiles=[
            ModelProfile(
                id="web-parallel",
                protocol="openai_chat",
                base_url=HttpUrl("https://parallel.test/v1"),
                model="parallel-model",
                auth_mode="none",
                limits=ProviderLimits(concurrency=1, retries=0),
            )
        ]
    )
    atomic_write_yaml(config_dir / "models.yaml", models)
    atomic_write_yaml(
        config_dir / "run.yaml",
        RunPlan(
            id="web-parallel-runs",
            suite=str(ROOT / "suites" / "smoke-v1.yaml"),
            models="models.yaml",
            trials=1,
            concurrency=1,
            database="../data/results.sqlite3",
            artifacts="../data/artifacts",
            reports="../data/reports",
        ),
    )
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
            first = await client.post(f"/api/run-configs/{config_id}/runs")
            second = await client.post(f"/api/run-configs/{config_id}/runs")
            assert first.status_code == 202
            assert second.status_code == 202
            run_ids = {first.json()["run_id"], second.json()["run_id"]}
            assert len(run_ids) == 2

            for _ in range(100):
                if WebSlowAsyncClient.calls >= 2:
                    break
                await asyncio.sleep(0.01)
            assert WebSlowAsyncClient.calls >= 2
            runs = (await client.get(f"/api/run-configs/{config_id}/runs")).json()
            active = {run["run_id"] for run in runs if run["status"] == "running"}
            assert active == run_ids

            for run_id in run_ids:
                stopped = await client.post(f"/api/run-configs/{config_id}/runs/{run_id}/stop")
                assert stopped.status_code == 202
                assert stopped.json()["status"] == "interrupted"

    asyncio.run(exercise())


def test_web_can_rerun_one_zero_score_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "configs"
    models = ModelsConfig(
        profiles=[
            ModelProfile(
                id="web-rerun-zero",
                protocol="openai_chat",
                base_url=HttpUrl("https://rerun.test/v1"),
                model="rerun-model",
                auth_mode="none",
                limits=ProviderLimits(concurrency=1, retries=0),
            )
        ]
    )
    atomic_write_yaml(config_dir / "models.yaml", models)
    run_path = config_dir / "run.yaml"
    atomic_write_yaml(
        run_path,
        RunPlan(
            id="web-rerun-zero",
            suite=str(ROOT / "suites" / "smoke-v1.yaml"),
            models="models.yaml",
            trials=2,
            concurrency=1,
            database="../data/results.sqlite3",
            artifacts="../data/artifacts",
            reports="../data/reports",
        ),
    )
    resolved = resolve_plan(run_path, PluginRegistry(load_external=False))
    WebRerunAsyncClient.prompt_answers = {
        item.prompt: json.dumps(item.oracle.witness.model_dump(mode="json"), ensure_ascii=False)
        for item in resolved.suite.scenarios
        if item.oracle.witness is not None
    }
    WebRerunAsyncClient.calls = 0
    monkeypatch.setattr("backpack_bench.runner.httpx.AsyncClient", WebRerunAsyncClient)

    async def wait_for_completion(
        client: httpx.AsyncClient, config_id: str, run_id: str
    ) -> dict[str, Any]:
        for _ in range(100):
            payload = (await client.get(f"/api/run-configs/{config_id}/runs/{run_id}")).json()
            if payload["status"] == "completed":
                return cast(dict[str, Any], payload)
            await asyncio.sleep(0.02)
        raise AssertionError("run did not complete")

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
            first = await wait_for_completion(client, config_id, run_id)
            assert first["progress"]["valid"] == 1
            zero_jobs = [job for job in first["jobs"] if job["actual_attack"] == 0]
            assert len(zero_jobs) == 1
            job_id = zero_jobs[0]["job_id"]

            rerun = await client.post(
                f"/api/run-configs/{config_id}/runs/{run_id}/jobs/{job_id}/rerun"
            )
            assert rerun.status_code == 202
            assert rerun.json()["job_id"] == job_id
            second = await wait_for_completion(client, config_id, run_id)
            assert second["progress"]["valid"] == 2
            assert all(job["actual_attack"] != 0 for job in second["jobs"])
            assert second["report"]["profiles"][0]["overall_score"] == 100
            assert WebRerunAsyncClient.calls == 3

            storage = Storage(tmp_path / "data" / "results.sqlite3")
            try:
                attempts = storage.connection.execute(
                    """
                    SELECT j.job_id, COUNT(a.attempt_id) AS attempts
                    FROM jobs j
                    JOIN attempts a ON a.job_id=j.job_id
                    WHERE j.run_id=?
                    GROUP BY j.job_id
                    ORDER BY attempts
                    """,
                    (run_id,),
                ).fetchall()
                assert [row["attempts"] for row in attempts] == [1, 2]
            finally:
                storage.close()

            no_zeros = await client.post(
                f"/api/run-configs/{config_id}/runs/{run_id}/jobs/{job_id}/rerun"
            )
            assert no_zeros.status_code == 409

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
                job["output_tokens"] > 20 and job["output_tokens_estimated"]
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
                        "params": {"thinking_effort": "xhigh"},
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
            assert payload["progress"]["total"] == 1
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
        request["json"]["reasoning_effort"] == "xhigh" for request in WebFakeAsyncClient.requests
    )
    assert all("max_tokens" not in request["json"] for request in WebFakeAsyncClient.requests)
    for path in (tmp_path / "data").rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


def test_web_runtime_timeout_is_a_total_stream_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "configs"
    atomic_write_yaml(
        config_dir / "models.yaml",
        ModelsConfig(
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
        ),
    )
    atomic_write_yaml(
        config_dir / "run.yaml",
        RunPlan(
            id="web-total-timeout",
            suite=str(ROOT / "suites" / "smoke-v1.yaml"),
            models="models.yaml",
            trials=1,
            database="../data/results.sqlite3",
            artifacts="../data/artifacts",
            reports="../data/reports",
        ),
    )
    WebHeartbeatAsyncClient.calls = 0
    monkeypatch.setattr("backpack_bench.runner.httpx.AsyncClient", WebHeartbeatAsyncClient)

    async def exercise() -> None:
        app = create_app(tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as client,
        ):
            config_id = (await client.get("/api/run-configs")).json()[0]["id"]
            started = await client.post(
                f"/api/run-configs/{config_id}/runs",
                json={
                    "profile": {
                        "protocol": "openai_chat",
                        "base_url": "https://heartbeat.test/v1",
                        "model": "heartbeat-model",
                        "auth_mode": "none",
                        "limits": {
                            "timeout_seconds": 0.05,
                            "concurrency": 1,
                            "retries": 0,
                        },
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
                await asyncio.sleep(0.01)
            assert payload is not None
            assert payload["status"] == "completed"
            assert WebHeartbeatAsyncClient.calls == 1
            assert payload["jobs"][0]["error_type"] == "api_timeout"
            assert 20 <= payload["jobs"][0]["latency_ms"] < 1000
            summary_path = next(
                (tmp_path / "data" / "artifacts" / run_id).glob("*/attempt_001/summary.json")
            )
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            assert summary["error_type"] == "api_timeout"
            assert "total timeout of 0.05 seconds" in summary["error_message"]
            run_artifacts = tmp_path / "data" / "artifacts" / run_id
            assert not [
                path for path in run_artifacts.iterdir() if path.name.endswith(".response.tmp")
            ]

    asyncio.run(exercise())
