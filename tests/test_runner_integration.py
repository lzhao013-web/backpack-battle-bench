import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import HttpUrl

from backpack_bench.io import atomic_write_yaml
from backpack_bench.plugins import PluginRegistry
from backpack_bench.reporting import build_run_report, group_report_view
from backpack_bench.runner import execute_plan, resolve_plan
from backpack_bench.schemas import (
    ModelProfile,
    ModelsConfig,
    ProviderLimits,
    RequestParams,
    RunPlan,
)
from backpack_bench.storage import Storage

ROOT = Path(__file__).resolve().parents[1]


class FakeAsyncClient:
    prompt_answers: dict[str, str] = {}
    failure_statuses: list[int] = []
    successful_responses = 0

    def __init__(self, **_: object) -> None:
        pass

    async def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> httpx.Response:
        if FakeAsyncClient.failure_statuses:
            status = FakeAsyncClient.failure_statuses.pop(0)
            return httpx.Response(
                status,
                json={"error": "retry", "echo": headers.get("Authorization")},
                headers={"Retry-After": "0"},
            )
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
            return httpx.Response(200, json=truncated)
        if "anthropic-version" in headers:
            value: dict[str, Any] = {
                "id": "anthropic-mock",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": answer}],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            }
        else:
            value = {
                "id": "openai-mock",
                "echo": headers.get("Authorization"),
                "choices": [{"finish_reason": "stop", "message": {"content": answer}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "completion_tokens_details": {"reasoning_tokens": 5},
                },
            }
        return httpx.Response(200, json=value)

    async def aclose(self) -> None:
        pass


class InterruptibleAsyncClient:
    prompt_answers: dict[str, str] = {}
    calls = 0

    def __init__(self, **_: object) -> None:
        pass

    async def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> httpx.Response:
        InterruptibleAsyncClient.calls += 1
        await asyncio.sleep(0.05)
        prompt = json["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "id": "resume-mock",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": InterruptibleAsyncClient.prompt_answers[prompt]},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            },
        )

    async def aclose(self) -> None:
        pass


class FastAsyncClient(InterruptibleAsyncClient):
    async def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> httpx.Response:
        prompt = json["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "id": "resume-mock",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": InterruptibleAsyncClient.prompt_answers[prompt]},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            },
        )


def test_24_job_matrix_retry_and_resume(
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
    assert result["jobs_total"] == 24
    assert result["jobs_completed"] == 24
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
    assert scores[0] == pytest.approx(91.6666666667)
    assert scores[1] == 100
    assert any(profile["retry_rate"] > 0 for profile in report["profiles"])
    assert sum(profile["truncation_rate"] for profile in report["profiles"]) == pytest.approx(
        1 / 12
    )
    assert (
        sum(profile["error_counts"].get("output_truncated", 0) for profile in report["profiles"])
        == 1
    )
    assert sum(profile["output_tokens"] for profile in report["profiles"]) > 24 * 20
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
    assert attempts == 26
    storage.close()

    resumed = asyncio.run(execute_plan(plan, resume_run_id=result["run_id"]))
    assert resumed["jobs_executed"] == 0
    with sqlite3.connect(plan.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 24
    artifact_text = "".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (tmp_path / "artifacts").rglob("*")
        if path.is_file()
    )
    assert "api_key" not in artifact_text.lower()
    assert test_key not in artifact_text
    assert test_key.encode() not in plan.database.read_bytes()
    assert (plan.reports / result["run_id"] / "report.json").is_file()
    assert (plan.reports / result["run_id"] / "report.csv").is_file()
    html_path = plan.reports / result["run_id"] / "report.html"
    assert html_path.is_file()
    html_report = html_path.read_text(encoding="utf-8")
    assert "单题与 Trial 明细" in html_report
    assert "Token 入/出/推理/缓存" in html_report
    assert "验证明细" in html_report
    assert "原始错误（Key 已脱敏）" in html_report
    assert "HTTP 429" in html_report
    assert "mixed-3x3" in html_report


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
        assert 0 < completed_before_resume < 24
    assert (plan.reports / run_id / "report.json").is_file()
    assert (plan.reports / run_id / "report.html").is_file()

    monkeypatch.setattr("backpack_bench.runner.httpx.AsyncClient", FastAsyncClient)
    resumed = asyncio.run(execute_plan(plan, resume_run_id=run_id))
    assert resumed["jobs_total"] == 24
    assert resumed["jobs_completed"] == 24
    assert resumed["jobs_executed"] == 24 - completed_before_resume
    with sqlite3.connect(plan.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 24
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 24
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT job_id, attempt_no FROM attempts "
                "GROUP BY job_id, attempt_no HAVING COUNT(*) > 1"
                ")"
            ).fetchone()[0]
            == 0
        )
