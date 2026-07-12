import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import HttpUrl

from backpack_bench.io import atomic_write_yaml
from backpack_bench.plugins import PluginRegistry
from backpack_bench.providers import adapter_for
from backpack_bench.reporting import build_run_report, group_report_view
from backpack_bench.runner import build_jobs, execute_plan, resolve_plan
from backpack_bench.schemas import (
    ModelProfile,
    ModelsConfig,
    ProviderLimits,
    RequestParams,
    RunPlan,
)
from backpack_bench.storage import Storage

ROOT = Path(__file__).resolve().parents[1]


def test_visual_run_builds_image_backed_jobs() -> None:
    plan = resolve_plan(
        ROOT / "configs" / "run.visual-smoke.yaml",
        PluginRegistry(load_external=False),
    )
    jobs = build_jobs(plan, "visual-test")
    assert plan.spec.prompt_mode == "visual_full"
    assert len(jobs) == 6
    job = jobs[0]
    assert job.prompt_image is not None
    assert Path(job.prompt_image.path).read_bytes().startswith(b"\x89PNG")
    assert "占用格偏移" not in job.prompt
    assert "每种物品的占用格、允许旋转、类别、基础攻击和效果均只在图片" in job.prompt
    body = adapter_for(job.profile).body(job.profile, job.prompt, job.prompt_image)
    assert isinstance(body["messages"][0]["content"], list)


def json_text(value: Any) -> str:
    return json.dumps(value)


def stream_response(value: dict[str, Any], anthropic: bool = False) -> httpx.Response:
    events: list[dict[str, Any]] = []
    if anthropic:
        events.append(
            {
                "type": "message_start",
                "message": {
                    "id": value["id"],
                    "usage": {"input_tokens": value.get("usage", {}).get("input_tokens", 0)},
                },
            }
        )
        for block in value.get("content", []):
            if block.get("type") == "text":
                events.append(
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": block["text"]},
                    }
                )
            elif block.get("type") == "thinking":
                events.append(
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "thinking_delta", "thinking": block["thinking"]},
                    }
                )
        events.append(
            {
                "type": "message_delta",
                "delta": {"stop_reason": value.get("stop_reason")},
                "usage": {"output_tokens": value.get("usage", {}).get("output_tokens", 0)},
            }
        )
        lines = [f"event: {event['type']}\ndata: {json.dumps(event)}\n" for event in events]
    else:
        choice = value["choices"][0]
        message = choice["message"]
        delta = {
            key: item
            for key, item in {
                "content": message.get("content"),
                "reasoning_content": message.get("reasoning_content"),
            }.items()
            if item is not None
        }
        events.extend(
            [
                {
                    "id": value["id"],
                    "choices": [{"delta": delta, "finish_reason": None}],
                },
                {
                    "id": value["id"],
                    "choices": [{"delta": {}, "finish_reason": choice.get("finish_reason")}],
                },
                {"id": value["id"], "choices": [], "usage": value.get("usage", {})},
            ]
        )
        lines = [f"data: {json.dumps(event)}\n" for event in events]
        lines.append("data: [DONE]\n")
    return httpx.Response(
        200,
        content="\n".join(lines),
        headers={"Content-Type": "text/event-stream"},
        request=httpx.Request("POST", "https://mock.test/stream"),
    )


class FakeAsyncClient:
    prompt_answers: dict[str, str] = {}
    failure_statuses: list[int] = []
    successful_responses = 0

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
        if FakeAsyncClient.failure_statuses:
            status = FakeAsyncClient.failure_statuses.pop(0)
            yield httpx.Response(
                status,
                content=json_text({"error": "retry", "echo": headers.get("Authorization")}),
                headers={"Retry-After": "0", "Content-Type": "application/json"},
                request=httpx.Request(method, url),
            )
            return
        prompt = json["messages"][0]["content"]
        answer = FakeAsyncClient.prompt_answers[prompt]
        FakeAsyncClient.successful_responses += 1
        if FakeAsyncClient.successful_responses == 1:
            if "anthropic-version" in headers:
                truncated: dict[str, Any] = {
                    "id": "anthropic-truncated",
                    "stop_reason": "max_tokens",
                    "content": [{"type": "thinking", "thinking": "unfinished"}],
                    "usage": {"input_tokens": 100, "output_tokens": 2048},
                }
            else:
                truncated = {
                    "id": "openai-truncated",
                    "choices": [{"finish_reason": "length", "message": {"content": None}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 2048},
                }
            yield stream_response(truncated, anthropic="anthropic-version" in headers)
            return
        if "anthropic-version" in headers:
            value: dict[str, Any] = {
                "id": "anthropic-mock",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": f"```json\n{answer}\n```"}],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            }
        else:
            value = {
                "id": "openai-mock",
                "echo": headers.get("Authorization"),
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": f"```json\n{answer}\n```"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "completion_tokens_details": {"reasoning_tokens": 5},
                },
            }
        yield stream_response(value, anthropic="anthropic-version" in headers)

    async def aclose(self) -> None:
        pass


class InterruptibleAsyncClient:
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
        InterruptibleAsyncClient.calls += 1
        await asyncio.sleep(0.05)
        prompt = json["messages"][0]["content"]
        yield stream_response(
            {
                "id": "resume-mock",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": InterruptibleAsyncClient.prompt_answers[prompt]},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            }
        )

    async def aclose(self) -> None:
        pass


class FastAsyncClient(InterruptibleAsyncClient):
    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        json: dict[str, Any],
    ) -> AsyncIterator[httpx.Response]:
        prompt = json["messages"][0]["content"]
        yield stream_response(
            {
                "id": "resume-mock",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": InterruptibleAsyncClient.prompt_answers[prompt]},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            }
        )


def test_6_job_matrix_retry_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    test_key = "credential-never-persist-this-value"
    monkeypatch.setenv("BBB_TEST_API_KEY", test_key)
    profiles = ModelsConfig(
        profiles=[
            ModelProfile(
                id="mock-openai",
                protocol="openai_chat",
                base_url=HttpUrl("https://mock.test/v1"),
                model="model-a",
                api_key_env="BBB_TEST_API_KEY",
                auth_mode="bearer",
                params=RequestParams(),
                limits=ProviderLimits(concurrency=2, retries=1),
            ),
            ModelProfile(
                id="mock-anthropic",
                protocol="anthropic_messages",
                base_url=HttpUrl("https://mock.test/v1"),
                model="model-b",
                auth_mode="none",
                params=RequestParams(max_tokens=4096),
                limits=ProviderLimits(concurrency=2, retries=1),
            ),
        ]
    )
    models_path = tmp_path / "models.yaml"
    atomic_write_yaml(models_path, profiles)
    plan_spec = RunPlan(
        id="integration",
        suite=str(ROOT / "suites" / "smoke-v1.yaml"),
        models=str(models_path),
        trials=3,
        concurrency=4,
        database=str(tmp_path / "results.sqlite3"),
        artifacts=str(tmp_path / "artifacts"),
        reports=str(tmp_path / "reports"),
    )
    plan_path = tmp_path / "run.yaml"
    atomic_write_yaml(plan_path, plan_spec)
    plan = resolve_plan(plan_path, PluginRegistry(load_external=False))
    FakeAsyncClient.prompt_answers = {
        item.prompt: json.dumps(item.oracle.witness.model_dump(mode="json"), ensure_ascii=False)
        for item in plan.suite.scenarios
        if item.oracle.witness is not None
    }
    FakeAsyncClient.failure_statuses = [429, 500]
    FakeAsyncClient.successful_responses = 0
    monkeypatch.setattr("backpack_bench.runner.httpx.AsyncClient", FakeAsyncClient)
    result = asyncio.run(execute_plan(plan))
    assert result["jobs_total"] == 6
    assert result["jobs_completed"] == 6
    console_errors = capsys.readouterr().err
    assert "Attempt failed" in console_errors
    assert "HTTP 429" in console_errors
    assert "HTTP 500" in console_errors
    assert test_key not in console_errors
    assert "***REDACTED***" in console_errors

    storage = Storage(plan.database)
    report = build_run_report(storage, result["run_id"])
    assert len(report["profiles"]) == 2
    scores = sorted(profile["overall_score"] for profile in report["profiles"])
    assert scores[0] == pytest.approx(66.6666666667)
    assert scores[1] == 100
    assert all(profile["best_of_3_score"] == 100 for profile in report["profiles"])
    assert any(profile["retry_rate"] > 0 for profile in report["profiles"])
    assert sum(profile["truncation_rate"] for profile in report["profiles"]) == pytest.approx(1 / 3)
    assert (
        sum(profile["error_counts"].get("output_truncated", 0) for profile in report["profiles"])
        == 1
    )
    assert sum(profile["output_tokens"] for profile in report["profiles"]) > 6 * 20
    assert all(
        len(scenario["trial_results"]) == 3
        for profile in report["profiles"]
        for scenario in profile["scenarios"]
    )
    assert all(
        trial["validation"]
        for profile in report["profiles"]
        for scenario in profile["scenarios"]
        for trial in scenario["trial_results"]
    )
    normalized_attempts: list[Path] = []
    for validation_path in plan.artifacts.glob(f"{result['run_id']}/*/attempt_*/validation.json"):
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        if validation.get("normalized_from") == "markdown_json_fence":
            normalized_attempts.append(validation_path.parent)
    assert normalized_attempts
    assert len(normalized_attempts) == 5
    assert all(
        (attempt / "model_output.txt").read_text(encoding="utf-8").startswith("```json\n")
        for attempt in normalized_attempts
    )
    attempts_in_report = [
        attempt
        for profile in report["profiles"]
        for scenario in profile["scenarios"]
        for trial in scenario["trial_results"]
        for attempt in trial["attempts"]
    ]
    failed_attempts = [attempt for attempt in attempts_in_report if attempt["error_type"]]
    assert {attempt["http_status"] for attempt in failed_attempts} >= {429, 500}
    assert any("HTTP 429" in attempt["error_message"] for attempt in failed_attempts)
    assert test_key not in json.dumps(failed_attempts)
    assert group_report_view(report, "difficulty")["entries"]
    attempts = storage.connection.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"]
    assert attempts == 8
    live_rows = storage.connection.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE live_output_tokens IS NOT NULL"
    ).fetchone()["n"]
    assert live_rows == 0
    storage.close()

    resumed = asyncio.run(execute_plan(plan, resume_run_id=result["run_id"]))
    assert resumed["jobs_executed"] == 0
    with sqlite3.connect(plan.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 6
    artifact_text = "".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (tmp_path / "artifacts").rglob("*")
        if path.is_file()
    )
    assert "api_key" not in artifact_text.lower()
    assert test_key not in artifact_text
    assert test_key.encode() not in plan.database.read_bytes()
    assert list((tmp_path / "artifacts").rglob("response.txt"))
    assert (plan.reports / result["run_id"] / "report.json").is_file()
    assert (plan.reports / result["run_id"] / "report.csv").is_file()
    html_path = plan.reports / result["run_id"] / "report.html"
    assert html_path.is_file()
    html_report = html_path.read_text(encoding="utf-8")
    assert "单题与 Trial 明细" in html_report
    assert "Token 入/出/推理/缓存" in html_report
    assert "验证明细" in html_report
    assert "Best-of-3" in html_report
    assert "原始错误（Key 已脱敏）" in html_report
    assert "HTTP 429" in html_report
    assert "packing-3x3" in html_report


def test_interrupted_run_resumes_without_duplicate_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles = ModelsConfig(
        profiles=[
            ModelProfile(
                id="interruptible-a",
                protocol="openai_chat",
                base_url=HttpUrl("https://mock.test/v1"),
                model="model-a",
                auth_mode="none",
                limits=ProviderLimits(concurrency=1, retries=0),
            ),
            ModelProfile(
                id="interruptible-b",
                protocol="openai_chat",
                base_url=HttpUrl("https://mock.test/v1"),
                model="model-b",
                auth_mode="none",
                limits=ProviderLimits(concurrency=1, retries=0),
            ),
        ]
    )
    models_path = tmp_path / "models.yaml"
    atomic_write_yaml(models_path, profiles)
    plan_spec = RunPlan(
        id="resume-integration",
        suite=str(ROOT / "suites" / "smoke-v1.yaml"),
        models=str(models_path),
        trials=3,
        concurrency=1,
        database=str(tmp_path / "resume.sqlite3"),
        artifacts=str(tmp_path / "artifacts"),
        reports=str(tmp_path / "reports"),
    )
    plan_path = tmp_path / "run.yaml"
    atomic_write_yaml(plan_path, plan_spec)
    plan = resolve_plan(plan_path, PluginRegistry(load_external=False))
    InterruptibleAsyncClient.prompt_answers = {
        item.prompt: json.dumps(item.oracle.witness.model_dump(mode="json"), ensure_ascii=False)
        for item in plan.suite.scenarios
        if item.oracle.witness is not None
    }
    InterruptibleAsyncClient.calls = 0
    monkeypatch.setattr("backpack_bench.runner.httpx.AsyncClient", InterruptibleAsyncClient)

    async def interrupt() -> None:
        task = asyncio.create_task(execute_plan(plan))
        while InterruptibleAsyncClient.calls < 3:
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(interrupt())
    with sqlite3.connect(plan.database) as connection:
        run_id, status = connection.execute("SELECT run_id, status FROM runs").fetchone()
        assert status == "interrupted"
        assert (
            connection.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
            == 0
        )
        completed_before_resume = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='completed'"
        ).fetchone()[0]
        assert 0 < completed_before_resume < 6
    assert (plan.reports / run_id / "report.json").is_file()
    assert (plan.reports / run_id / "report.html").is_file()

    monkeypatch.setattr("backpack_bench.runner.httpx.AsyncClient", FastAsyncClient)
    resumed = asyncio.run(execute_plan(plan, resume_run_id=run_id))
    assert resumed["jobs_total"] == 6
    assert resumed["jobs_completed"] == 6
    assert resumed["jobs_executed"] == 6 - completed_before_resume
    with sqlite3.connect(plan.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 6
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 6
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT job_id, attempt_no FROM attempts "
                "GROUP BY job_id, attempt_no HAVING COUNT(*) > 1"
                ")"
            ).fetchone()[0]
            == 0
        )
