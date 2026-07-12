"""Resumable local model-matrix execution engine."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console

from backpack_bench import __version__
from backpack_bench.canonical import content_hash, text_hash
from backpack_bench.evaluation import parse_and_score_output
from backpack_bench.io import atomic_write_bytes, atomic_write_json, atomic_write_text, load_yaml
from backpack_bench.plugins import PluginRegistry
from backpack_bench.prompt import (
    PROMPT_TEMPLATE_VERSION,
    VISUAL_PROMPT_TEMPLATE_VERSION,
    render_visual_prompt,
)
from backpack_bench.providers import adapter_for
from backpack_bench.providers.base import (
    ParsedCompletion,
    PromptImage,
    profile_hash,
    redact_headers,
    redact_secret_values,
    resolve_api_key,
)
from backpack_bench.schemas import ModelProfile, ModelsConfig, RunPlan
from backpack_bench.storage import DbWriter, Storage
from backpack_bench.suite import ResolvedScenario, ResolvedSuite, load_suite

runner_console = Console(stderr=True, highlight=False)


def benchmark_suite_id(plan: ResolvedPlan) -> str:
    if plan.spec.prompt_mode == "text":
        return plan.suite.spec.id
    return f"{plan.suite.spec.id}@{plan.spec.prompt_mode}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def create_run_id(plan: ResolvedPlan) -> str:
    return (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + plan.plan_hash[:10]
        + "_"
        + uuid.uuid4().hex[:6]
    )


@dataclass(frozen=True)
class ResolvedPlan:
    path: Path
    spec: RunPlan
    suite: ResolvedSuite
    profiles: tuple[ModelProfile, ...]
    database: Path
    artifacts: Path
    reports: Path
    plan_hash: str
    registry: PluginRegistry


@dataclass(frozen=True)
class JobContext:
    job_id: str
    run_id: str
    profile: ModelProfile
    profile_hash: str
    scenario: ResolvedScenario
    prompt: str
    prompt_image: PromptImage | None
    trial: int


@dataclass(frozen=True)
class AttemptOutcome:
    retryable: bool
    error_type: str | None
    error_message: str | None
    http_status: int | None
    response_json: Any | None
    response_text: str | None
    completion: ParsedCompletion | None
    validation: dict[str, Any] | None
    latency_ms: float
    retry_after: float | None


def _attempt_failure(outcome: AttemptOutcome) -> tuple[str | None, str | None]:
    error_type = outcome.error_type
    error_message = outcome.error_message
    validation = outcome.validation
    if error_type is None and validation is not None and not validation.get("valid", False):
        error_type = str(validation.get("error_type") or "invalid_output")
        errors = validation.get("errors")
        error_message = (
            json.dumps(errors, ensure_ascii=False, indent=2)
            if errors
            else f"validation failed: {error_type}"
        )
    return error_type, error_message


def _print_attempt_failure(
    job: JobContext,
    attempt_no: int,
    outcome: AttemptOutcome,
    api_key: str | None,
) -> None:
    error_type, error_message = _attempt_failure(outcome)
    if error_type is None:
        return
    runner_console.print(
        f"[red]Attempt failed[/red] job={job.job_id} trial={job.trial} "
        f"attempt={attempt_no} type={error_type} http={outcome.http_status or '—'}"
    )
    if error_message:
        safe_message = redact_secret_values(error_message, api_key)
        runner_console.print(str(safe_message), markup=False)


class RateLimiter:
    def __init__(self, qps: float | None) -> None:
        self.interval = 0.0 if qps is None else 1.0 / qps
        self.next_allowed = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.interval == 0:
            return
        async with self.lock:
            now = time.monotonic()
            delay = self.next_allowed - now
            if delay > 0:
                await asyncio.sleep(delay)
            self.next_allowed = max(now, self.next_allowed) + self.interval


TokenProgressCallback = Callable[[int, bool, bool], Awaitable[None]]
_TOKEN_PART = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u3400-\u9fff]")


def _estimate_output_tokens(value: str) -> int:
    """Tokenizer-independent count candidate compared with provider usage."""
    total = 0
    for match in _TOKEN_PART.finditer(value):
        part = match.group(0)
        is_word = part[0].isascii() and (part[0].isalnum() or part[0] == "_")
        total += math.ceil(len(part) / 4) if is_word else 1
    return total


def _merge_usage(target: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge_usage(current, value)
        else:
            target[key] = value


def _reported_output_tokens(usage: dict[str, Any]) -> int | None:
    value = usage.get("completion_tokens", usage.get("output_tokens"))
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _store_recorded_output_tokens(
    usage: dict[str, Any],
    count: int,
    *,
    estimated: bool,
    api_reported: int | None,
    stream_peak: int,
) -> None:
    token_keys = [key for key in ("completion_tokens", "output_tokens") if key in usage]
    if not token_keys:
        token_keys = ["output_tokens"]
    for key in token_keys:
        usage[key] = count
    usage["api_output_tokens"] = api_reported
    usage["stream_output_tokens_peak"] = stream_peak
    usage["output_tokens_estimated"] = estimated


class LiveTokenReporter:
    """Throttle streamed token snapshots before writing them to SQLite."""

    def __init__(self, storage: Storage, writer: DbWriter, job_id: str) -> None:
        self.storage = storage
        self.writer = writer
        self.job_id = job_id
        self.last_write = 0.0
        self.last_value: tuple[int, bool] | None = None

    async def report(self, output_tokens: int, estimated: bool, force: bool = False) -> None:
        value = (max(0, output_tokens), estimated)
        now = time.monotonic()
        if not force:
            if value == self.last_value:
                return
            if (
                self.last_value is not None
                and self.last_value[0] > 0
                and now - self.last_write < 0.15
            ):
                return
        await self.writer.submit(
            self.storage.set_job_live_tokens,
            self.job_id,
            value[0],
            value[1],
        )
        self.last_value = value
        self.last_write = now


def resolve_plan(path: Path, registry: PluginRegistry) -> ResolvedPlan:
    path = path.resolve()
    spec = load_yaml(path, RunPlan)
    base = path.parent
    suite = load_suite((base / spec.suite).resolve(), registry)
    models = load_yaml((base / spec.models).resolve(), ModelsConfig)
    profiles = models.profiles
    if spec.model_ids is not None:
        wanted = set(spec.model_ids)
        profiles = [profile for profile in profiles if profile.id in wanted]
        missing = sorted(wanted - {profile.id for profile in profiles})
        if missing:
            raise ValueError(f"run plan references unknown model profiles: {missing}")
    if not profiles:
        raise ValueError("run plan selected no model profiles")
    hashes = [profile_hash(profile) for profile in profiles]
    plan_hash = content_hash(
        {
            "plan": spec,
            "suite_hash": suite.suite_hash,
            "profile_hashes": hashes,
        }
    )
    return ResolvedPlan(
        path=path,
        spec=spec,
        suite=suite,
        profiles=tuple(profiles),
        database=(base / spec.database).resolve(),
        artifacts=(base / spec.artifacts).resolve(),
        reports=(base / spec.reports).resolve(),
        plan_hash=plan_hash,
        registry=registry,
    )


def build_jobs(plan: ResolvedPlan, run_id: str) -> list[JobContext]:
    jobs: list[JobContext] = []
    for profile in plan.profiles:
        current_profile_hash = profile_hash(profile)
        for resolved_scenario in plan.suite.scenarios:
            if plan.spec.prompt_mode == "text":
                prompt = resolved_scenario.prompt
                prompt_image = None
            else:
                prompt = render_visual_prompt(
                    resolved_scenario.scenario,
                    plan.registry,
                    plan.spec.prompt_mode,
                )
                sheet_path = plan.suite.visual_pack.scenario_sheet(
                    resolved_scenario.scenario.id,
                    resolved_scenario.entry.scenario_hash,
                    plan.spec.prompt_mode,
                )
                prompt_image = PromptImage(str(sheet_path))
            for trial in range(1, plan.spec.trials + 1):
                job_id = content_hash(
                    {
                        "run_id": run_id,
                        "profile_hash": current_profile_hash,
                        "scenario_hash": resolved_scenario.entry.scenario_hash,
                        "trial": trial,
                    }
                )[:24]
                jobs.append(
                    JobContext(
                        job_id=job_id,
                        run_id=run_id,
                        profile=profile,
                        profile_hash=current_profile_hash,
                        scenario=resolved_scenario,
                        prompt=prompt,
                        prompt_image=prompt_image,
                        trial=trial,
                    )
                )
    return jobs


def dry_run_summary(plan: ResolvedPlan) -> dict[str, Any]:
    return {
        "plan_id": plan.spec.id,
        "plan_hash": plan.plan_hash,
        "suite_id": plan.suite.spec.id,
        "leaderboard_suite_id": benchmark_suite_id(plan),
        "suite_hash": plan.suite.suite_hash,
        "profiles": [
            {
                "id": profile.id,
                "model": profile.model,
                "protocol": profile.protocol,
                "profile_hash": profile_hash(profile),
            }
            for profile in plan.profiles
        ],
        "scenarios": len(plan.suite.scenarios),
        "trials": plan.spec.trials,
        "jobs": len(plan.profiles) * len(plan.suite.scenarios) * plan.spec.trials,
        "concurrency": plan.spec.concurrency,
        "prompt_mode": plan.spec.prompt_mode,
        "database": str(plan.database),
        "artifacts": str(plan.artifacts),
        "reports": str(plan.reports),
    }


def _retry_after(headers: httpx.Headers) -> float | None:
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            return max(0.0, date.timestamp() - datetime.now(UTC).timestamp())
        except (TypeError, ValueError, OverflowError):
            return None


def _validate_completion(
    job: JobContext,
    completion: ParsedCompletion,
    registry: PluginRegistry,
) -> dict[str, Any]:
    return parse_and_score_output(
        job.scenario.scenario,
        completion.content,
        registry,
        finish_reason=completion.finish_reason,
        allow_markdown_json_fence=job.profile.params.json_mode,
    )


async def _call_once(
    job: JobContext,
    client: httpx.AsyncClient,
    api_key: str | None,
    registry: PluginRegistry,
    token_progress: TokenProgressCallback,
) -> AttemptOutcome:
    adapter = adapter_for(job.profile)
    endpoint = adapter.endpoint(job.profile)
    headers = adapter.headers(job.profile, api_key)
    body = adapter.body(job.profile, job.prompt, job.prompt_image)
    await token_progress(0, True, True)
    started = time.perf_counter()
    http_status: int | None = None
    retry_after: float | None = None
    raw_lines: list[str] = []
    try:
        async with client.stream("POST", endpoint, headers=headers, json=body) as response:
            http_status = response.status_code
            retry_after = _retry_after(response.headers)
            if not 200 <= response.status_code < 300:
                await response.aread()
                response_text = response.text
                try:
                    response_json: Any | None = response.json()
                except json.JSONDecodeError:
                    response_json = None
                return AttemptOutcome(
                    retryable=response.status_code in {408, 429} or response.status_code >= 500,
                    error_type="api_http",
                    error_message=f"HTTP {response.status_code}: {response_text[:2000]}",
                    http_status=response.status_code,
                    response_json=response_json,
                    response_text=response_text,
                    completion=None,
                    validation=None,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    retry_after=retry_after,
                )

            content_type = response.headers.get("content-type", "").lower()
            if "application/json" in content_type or "+json" in content_type:
                await response.aread()
                response_text = response.text
                try:
                    response_json = response.json()
                except json.JSONDecodeError:
                    return AttemptOutcome(
                        retryable=False,
                        error_type="api_schema",
                        error_message="successful streaming response was not SSE or JSON",
                        http_status=response.status_code,
                        response_json=None,
                        response_text=response_text,
                        completion=None,
                        validation=None,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        retry_after=None,
                    )
                try:
                    completion = adapter.parse(response_json)
                except ValueError as error:
                    return AttemptOutcome(
                        retryable=False,
                        error_type="api_schema",
                        error_message=str(error),
                        http_status=response.status_code,
                        response_json=response_json,
                        response_text=response_text,
                        completion=None,
                        validation=None,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        retry_after=None,
                    )
                reported = _reported_output_tokens(completion.usage)
                estimated = _estimate_output_tokens(
                    (completion.reasoning or "") + completion.content
                )
                token_count_is_estimated = reported is None
                if reported is None:
                    completion.usage["output_tokens"] = estimated
                    completion.usage["output_tokens_estimated"] = True
                    reported = estimated
                await token_progress(reported, token_count_is_estimated, True)
                validation = _validate_completion(job, completion, registry)
                return AttemptOutcome(
                    retryable=False,
                    error_type=None,
                    error_message=None,
                    http_status=response.status_code,
                    response_json=response_json,
                    response_text=response_text,
                    completion=completion,
                    validation=validation,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    retry_after=None,
                )

            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            usage: dict[str, Any] = {}
            finish_reason: str | None = None
            response_id: str | None = None
            direct_completion: ParsedCompletion | None = None
            parsed_events = 0
            stream_peak = 0
            stream_peak_estimated = True

            def observe_stream_count(count: int, estimated: bool) -> None:
                nonlocal stream_peak, stream_peak_estimated
                if count > stream_peak or (count == stream_peak and not estimated):
                    stream_peak = count
                    stream_peak_estimated = estimated

            async for line in response.aiter_lines():
                raw_lines.append(line)
                data = line.strip()
                if not data or data.startswith((":", "event:", "id:", "retry:")):
                    continue
                if data.startswith("data:"):
                    data = data[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError as error:
                    return AttemptOutcome(
                        retryable=False,
                        error_type="api_schema",
                        error_message=f"invalid JSON in stream event: {error}",
                        http_status=response.status_code,
                        response_json=None,
                        response_text="\n".join(raw_lines),
                        completion=None,
                        validation=None,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        retry_after=None,
                    )
                parsed_events += 1
                try:
                    direct_completion = adapter.parse(event)
                except ValueError:
                    try:
                        parsed = adapter.parse_stream_event(event)
                    except ValueError as error:
                        return AttemptOutcome(
                            retryable=False,
                            error_type="api_schema",
                            error_message=str(error),
                            http_status=response.status_code,
                            response_json=None,
                            response_text="\n".join(raw_lines),
                            completion=None,
                            validation=None,
                            latency_ms=(time.perf_counter() - started) * 1000,
                            retry_after=None,
                        )
                    content_parts.append(parsed.content_delta)
                    reasoning_parts.append(parsed.reasoning_delta)
                    if parsed.finish_reason is not None:
                        finish_reason = parsed.finish_reason
                    if parsed.response_id is not None:
                        response_id = parsed.response_id
                    _merge_usage(usage, parsed.usage)
                    estimated_count = _estimate_output_tokens(
                        "".join(reasoning_parts) + "".join(content_parts)
                    )
                    event_reported = _reported_output_tokens(parsed.usage)
                    event_count = max(event_reported or 0, estimated_count)
                    event_estimated = event_reported is None or estimated_count > event_reported
                    observe_stream_count(event_count, event_estimated)
                    await token_progress(
                        event_count,
                        event_estimated,
                        False,
                    )
                else:
                    reported = _reported_output_tokens(direct_completion.usage)
                    estimated_count = _estimate_output_tokens(
                        (direct_completion.reasoning or "") + direct_completion.content
                    )
                    event_count = max(reported or 0, estimated_count)
                    event_estimated = reported is None or estimated_count > reported
                    observe_stream_count(event_count, event_estimated)
                    await token_progress(
                        event_count,
                        event_estimated,
                        False,
                    )

            if direct_completion is not None:
                completion = direct_completion
                _merge_usage(completion.usage, usage)
            else:
                content = "".join(content_parts)
                reasoning = "".join(reasoning_parts)
                if parsed_events == 0:
                    return AttemptOutcome(
                        retryable=False,
                        error_type="api_schema",
                        error_message="successful response contained no stream events",
                        http_status=response.status_code,
                        response_json=None,
                        response_text="\n".join(raw_lines),
                        completion=None,
                        validation=None,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        retry_after=None,
                    )
                if not content and finish_reason != "length":
                    return AttemptOutcome(
                        retryable=False,
                        error_type="api_schema",
                        error_message="stream produced no final text content",
                        http_status=response.status_code,
                        response_json=None,
                        response_text="\n".join(raw_lines),
                        completion=None,
                        validation=None,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        retry_after=None,
                    )
                estimated_count = _estimate_output_tokens(reasoning + content)
                observe_stream_count(estimated_count, True)
                completion = ParsedCompletion(
                    content=content,
                    reasoning=reasoning or None,
                    finish_reason=finish_reason,
                    usage=usage,
                    response_id=response_id,
                )
            api_reported = _reported_output_tokens(completion.usage)
            final_text_estimate = _estimate_output_tokens(
                (completion.reasoning or "") + completion.content
            )
            observe_stream_count(final_text_estimate, True)
            final_count = max(api_reported or 0, stream_peak)
            final_estimated = api_reported is None or stream_peak > api_reported
            if final_count == 0:
                final_count = final_text_estimate
                final_estimated = True
            if api_reported is not None and api_reported == final_count:
                final_estimated = False
            elif stream_peak == final_count:
                final_estimated = stream_peak_estimated
            _store_recorded_output_tokens(
                completion.usage,
                final_count,
                estimated=final_estimated,
                api_reported=api_reported,
                stream_peak=stream_peak,
            )
            await token_progress(final_count, final_estimated, True)
            validation = _validate_completion(job, completion, registry)
            return AttemptOutcome(
                retryable=False,
                error_type=None,
                error_message=None,
                http_status=response.status_code,
                response_json=None,
                response_text="\n".join(raw_lines),
                completion=completion,
                validation=validation,
                latency_ms=(time.perf_counter() - started) * 1000,
                retry_after=None,
            )
    except httpx.HTTPError as error:
        return AttemptOutcome(
            retryable=True,
            error_type="api_transport",
            error_message=str(error),
            http_status=http_status,
            response_json=None,
            response_text="\n".join(raw_lines) or None,
            completion=None,
            validation=None,
            latency_ms=(time.perf_counter() - started) * 1000,
            retry_after=retry_after,
        )


async def _call_once_with_total_timeout(
    job: JobContext,
    client: httpx.AsyncClient,
    api_key: str | None,
    registry: PluginRegistry,
    token_progress: TokenProgressCallback,
) -> AttemptOutcome:
    """Enforce a wall-clock request deadline in addition to HTTP inactivity timeouts."""
    timeout_seconds = job.profile.limits.timeout_seconds
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_seconds):
            return await _call_once(
                job,
                client,
                api_key,
                registry,
                token_progress,
            )
    except TimeoutError:
        return AttemptOutcome(
            retryable=True,
            error_type="api_timeout",
            error_message=(f"request exceeded total timeout of {timeout_seconds:g} seconds"),
            http_status=None,
            response_json=None,
            response_text=None,
            completion=None,
            validation=None,
            latency_ms=(time.perf_counter() - started) * 1000,
            retry_after=None,
        )


def _usage_tokens(usage: dict[str, Any]) -> tuple[int, int, int, int]:
    input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
    completion_details = usage.get("completion_tokens_details")
    output_details = usage.get("output_tokens_details")
    details = completion_details if isinstance(completion_details, dict) else output_details
    reasoning_tokens = (
        int(details.get("reasoning_tokens", details.get("thinking_tokens", 0)) or 0)
        if isinstance(details, dict)
        else 0
    )
    prompt_details = usage.get("prompt_tokens_details")
    cached_tokens = (
        int(prompt_details.get("cached_tokens", 0) or 0)
        if isinstance(prompt_details, dict)
        else int(usage.get("cache_read_input_tokens", 0) or 0)
    )
    return input_tokens, output_tokens, reasoning_tokens, cached_tokens


def _estimated_cost(profile: ModelProfile, usage: dict[str, Any]) -> float | None:
    if profile.pricing is None:
        return None
    input_tokens, output_tokens, reasoning_tokens, _ = _usage_tokens(usage)
    pricing = profile.pricing
    if pricing.input_per_million is None or pricing.output_per_million is None:
        return None
    non_reasoning_output = max(0, output_tokens - reasoning_tokens)
    reasoning_price = pricing.reasoning_per_million or pricing.output_per_million
    return (
        input_tokens * pricing.input_per_million
        + non_reasoning_output * pricing.output_per_million
        + reasoning_tokens * reasoning_price
    ) / 1_000_000


def _attempt_artifact(
    plan: ResolvedPlan,
    job: JobContext,
    attempt_no: int,
    outcome: AttemptOutcome,
    api_key: str | None,
) -> Path:
    adapter = adapter_for(job.profile)
    error_type, error_message = _attempt_failure(outcome)
    path = plan.artifacts / job.run_id / job.job_id / f"attempt_{attempt_no:03d}"
    path.mkdir(parents=True, exist_ok=False)
    request_body = adapter.body(job.profile, job.prompt, job.prompt_image)
    messages = request_body.get("messages")
    if job.prompt_image is not None and isinstance(messages, list) and messages:
        content = messages[0].get("content") if isinstance(messages[0], dict) else None
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                image_url = block.get("image_url")
                if isinstance(image_url, dict) and "url" in image_url:
                    image_url["url"] = "<omitted:image/png>"
                source = block.get("source")
                if isinstance(source, dict) and "data" in source:
                    source["data"] = "<omitted:image/png>"
    atomic_write_json(
        path / "request.json",
        redact_secret_values(
            {
                "url": adapter.endpoint(job.profile),
                "headers": redact_headers(adapter.headers(job.profile, api_key)),
                "body": request_body,
            },
            api_key,
        ),
    )
    if outcome.response_json is not None:
        atomic_write_json(
            path / "response.json", redact_secret_values(outcome.response_json, api_key)
        )
    elif outcome.response_text is not None:
        atomic_write_text(
            path / "response.txt", redact_secret_values(outcome.response_text, api_key)
        )
    if outcome.completion is not None:
        atomic_write_text(
            path / "model_output.txt",
            redact_secret_values(outcome.completion.content, api_key),
        )
        if outcome.completion.reasoning is not None:
            atomic_write_text(
                path / "reasoning.txt",
                redact_secret_values(outcome.completion.reasoning, api_key),
            )
    if outcome.validation is not None:
        atomic_write_json(
            path / "validation.json", redact_secret_values(outcome.validation, api_key)
        )
    atomic_write_json(
        path / "summary.json",
        redact_secret_values(
            {
                "job_id": job.job_id,
                "attempt": attempt_no,
                "http_status": outcome.http_status,
                "error_type": error_type,
                "error_message": error_message,
                "latency_ms": round(outcome.latency_ms, 3),
                "finish_reason": outcome.completion.finish_reason if outcome.completion else None,
                "usage": outcome.completion.usage if outcome.completion else {},
            },
            api_key,
        ),
    )
    return path.resolve()


def _next_attempt_no(storage: Storage, plan: ResolvedPlan, job: JobContext) -> int:
    database_next = storage.next_attempt_no(job.job_id)
    job_dir = plan.artifacts / job.run_id / job.job_id
    artifact_numbers: list[int] = []
    if job_dir.exists():
        for path in job_dir.glob("attempt_*"):
            try:
                artifact_numbers.append(int(path.name.removeprefix("attempt_")))
            except ValueError:
                continue
    artifact_next = max(artifact_numbers, default=0) + 1
    return max(database_next, artifact_next)


async def _execute_job(
    plan: ResolvedPlan,
    job: JobContext,
    storage: Storage,
    writer: DbWriter,
    client: httpx.AsyncClient,
    api_key: str | None,
    limiter: RateLimiter,
    global_semaphore: asyncio.Semaphore,
    profile_semaphore: asyncio.Semaphore,
) -> None:
    async with global_semaphore, profile_semaphore:
        await writer.submit(storage.set_job_status, job.job_id, "running")
        token_reporter = LiveTokenReporter(storage, writer, job.job_id)
        first_attempt = _next_attempt_no(storage, plan, job)
        final_outcome: AttemptOutcome | None = None
        final_artifact_dir: Path | None = None
        for offset in range(job.profile.limits.retries + 1):
            attempt_no = first_attempt + offset
            await limiter.wait()
            started_at = utc_now()
            outcome = await _call_once_with_total_timeout(
                job,
                client,
                api_key,
                plan.registry,
                token_reporter.report,
            )
            completed_at = utc_now()
            error_type, error_message = _attempt_failure(outcome)
            _print_attempt_failure(job, attempt_no, outcome, api_key)
            artifact_dir = _attempt_artifact(plan, job, attempt_no, outcome, api_key)
            final_artifact_dir = artifact_dir
            usage = outcome.completion.usage if outcome.completion else {}
            safe_usage = redact_secret_values(usage, api_key)
            await writer.submit(
                storage.insert_attempt,
                {
                    "job_id": job.job_id,
                    "attempt_no": attempt_no,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "http_status": outcome.http_status,
                    "error_type": error_type,
                    "error_message": redact_secret_values(error_message, api_key),
                    "latency_ms": outcome.latency_ms,
                    "usage": safe_usage,
                    "artifact_dir": str(artifact_dir),
                },
            )
            final_outcome = outcome
            if not outcome.retryable or offset >= job.profile.limits.retries:
                break
            delay = outcome.retry_after if outcome.retry_after is not None else min(30.0, 2**offset)
            await asyncio.sleep(delay)
        if final_outcome is None or final_artifact_dir is None:
            raise AssertionError("job produced no attempt")
        oracle_attack = int(job.scenario.oracle.optimal_attack or 0)
        completion = final_outcome.completion
        validation = final_outcome.validation or {
            "valid": False,
            "error_type": final_outcome.error_type or "api_transport",
            "actual_attack": 0,
            "errors": [{"message": final_outcome.error_message or "unknown error"}],
        }
        valid = bool(validation.get("valid"))
        actual_attack = int(validation.get("actual_attack", 0)) if valid else 0
        if actual_attack > oracle_attack:
            message = f"actual attack {actual_attack} exceeds exact oracle {oracle_attack}"
            validation = {
                **validation,
                "valid": False,
                "error_type": "engine_oracle_inconsistency",
                "errors": [{"message": message}],
            }
            valid = False
            actual_attack = 0
        usage = completion.usage if completion else {}
        safe_validation = redact_secret_values(validation, api_key)
        safe_usage = redact_secret_values(usage, api_key)
        atomic_write_json(
            final_artifact_dir / "score.json",
            {
                "valid": valid,
                "error_type": validation.get("error_type"),
                "actual_attack": actual_attack,
                "oracle_attack": oracle_attack,
                "ratio": actual_attack / oracle_attack if oracle_attack else 0.0,
                "validation": safe_validation,
                "estimated_cost": _estimated_cost(job.profile, usage),
            },
        )
        await writer.submit(
            storage.save_result,
            {
                "job_id": job.job_id,
                "valid": valid,
                "error_type": validation.get("error_type"),
                "actual_attack": actual_attack,
                "oracle_attack": oracle_attack,
                "ratio": actual_attack / oracle_attack if oracle_attack else 0.0,
                "finish_reason": completion.finish_reason if completion else None,
                "validation": safe_validation,
                "usage": safe_usage,
                "latency_ms": final_outcome.latency_ms,
                "estimated_cost": _estimated_cost(job.profile, usage),
            },
        )
        if validation.get("error_type") == "engine_oracle_inconsistency":
            raise RuntimeError(validation["errors"][0]["message"])


async def execute_plan(
    plan: ResolvedPlan,
    resume_run_id: str | None = None,
    new_run_id: str | None = None,
    api_key_overrides: dict[str, str] | None = None,
    rerun_job_id: str | None = None,
) -> dict[str, Any]:
    if resume_run_id is not None and new_run_id is not None:
        raise ValueError("resume_run_id and new_run_id are mutually exclusive")
    if rerun_job_id is not None and resume_run_id is None:
        raise ValueError("rerun_job_id requires resume_run_id")
    overrides = api_key_overrides or {}
    api_keys = {
        profile.id: overrides[profile.id] if profile.id in overrides else resolve_api_key(profile)
        for profile in plan.profiles
    }
    storage = Storage(plan.database)
    writer = DbWriter(storage)
    await writer.start()
    run_id = resume_run_id or new_run_id or create_run_id(plan)
    jobs = build_jobs(plan, run_id)
    pending: list[JobContext] = []
    clients: dict[str, httpx.AsyncClient] = {}
    tasks: list[asyncio.Task[None]] = []
    run_registered = False
    status = "running"
    caught_error: BaseException | None = None
    try:
        if resume_run_id:
            run = storage.get_run(run_id)
            if run is None:
                raise ValueError(f"run {run_id} does not exist in {plan.database}")
            if run["plan_hash"] != plan.plan_hash or run["suite_hash"] != plan.suite.suite_hash:
                raise ValueError("resume run does not match the supplied plan/suite hashes")
            if storage.has_engine_oracle_inconsistency(run_id):
                raise ValueError("a run with an engine/Oracle inconsistency cannot be resumed")
            if rerun_job_id is not None:
                if not storage.reset_zero_score_job(run_id, rerun_job_id):
                    raise ValueError(f"job {rerun_job_id} is not a completed zero-score job")
            else:
                storage.reset_interrupted(run_id)
            run_registered = True
        else:
            storage.create_run(
                {
                    "run_id": run_id,
                    "plan_id": plan.spec.id,
                    "plan_hash": plan.plan_hash,
                    "suite_id": benchmark_suite_id(plan),
                    "suite_hash": plan.suite.suite_hash,
                    "status": "running",
                    "started_at": utc_now(),
                    "config": plan.spec.model_dump(mode="json", exclude_none=True),
                }
            )
            run_registered = True
        for profile in plan.profiles:
            storage.register_profile(
                {
                    "profile_hash": profile_hash(profile),
                    "profile_id": profile.id,
                    "display_name": profile.display_name or profile.id,
                    "protocol": profile.protocol,
                    "model": profile.model,
                    "config": profile.model_dump(
                        mode="json", exclude={"api_key_env"}, exclude_none=True
                    ),
                }
            )
        scenario_inputs = {job.scenario.entry.scenario_hash: job for job in jobs}
        for scenario in plan.suite.scenarios:
            storage.register_scenario(
                {
                    "scenario_hash": scenario.entry.scenario_hash,
                    "scenario_id": scenario.scenario.id,
                    "title": scenario.scenario.title,
                    "difficulty": scenario.scenario.difficulty,
                    "tags": scenario.scenario.tags,
                    "oracle_attack": int(scenario.oracle.optimal_attack or 0),
                }
            )
            prompt_path = (
                plan.artifacts / run_id / "prompts" / f"{scenario.entry.scenario_hash}.txt"
            )
            if not prompt_path.exists():
                current_input = scenario_inputs[scenario.entry.scenario_hash]
                atomic_write_text(prompt_path, current_input.prompt)
                if current_input.prompt_image is not None:
                    image_path = (
                        plan.artifacts / run_id / "inputs" / f"{scenario.entry.scenario_hash}.png"
                    )
                    atomic_write_bytes(
                        image_path, Path(current_input.prompt_image.path).read_bytes()
                    )
        for job in jobs:
            storage.create_job(
                {
                    "job_id": job.job_id,
                    "run_id": run_id,
                    "profile_hash": job.profile_hash,
                    "scenario_hash": job.scenario.entry.scenario_hash,
                    "trial": job.trial,
                    "weight": job.scenario.entry.weight,
                }
            )
        run_artifact = plan.artifacts / run_id / "run.json"
        if not run_artifact.exists():
            atomic_write_json(
                run_artifact,
                {
                    "run_id": run_id,
                    "plan_hash": plan.plan_hash,
                    "suite_hash": plan.suite.suite_hash,
                    "item_catalog_hash": plan.suite.catalog_hash,
                    "visual_pack_hash": plan.suite.visual_pack.pack_hash,
                    "engine_version": __version__,
                    "prompt_mode": plan.spec.prompt_mode,
                    "prompt_template_version": (
                        PROMPT_TEMPLATE_VERSION
                        if plan.spec.prompt_mode == "text"
                        else VISUAL_PROMPT_TEMPLATE_VERSION
                    ),
                    "prompt_hashes": {
                        scenario_hash: text_hash(job.prompt)
                        for scenario_hash, job in scenario_inputs.items()
                    },
                    "profiles": [profile_hash(profile) for profile in plan.profiles],
                    "jobs": len(jobs),
                },
            )
        completed = storage.completed_job_ids(run_id)
        pending = [job for job in jobs if job.job_id not in completed]
        global_semaphore = asyncio.Semaphore(plan.spec.concurrency)
        profile_semaphores = {
            profile.id: asyncio.Semaphore(profile.limits.concurrency) for profile in plan.profiles
        }
        limiters = {profile.id: RateLimiter(profile.limits.qps) for profile in plan.profiles}
        clients = {
            profile.id: httpx.AsyncClient(
                timeout=profile.limits.timeout_seconds,
                verify=profile.verify_tls,
                follow_redirects=True,
            )
            for profile in plan.profiles
        }
        tasks = [
            asyncio.create_task(
                _execute_job(
                    plan,
                    job,
                    storage,
                    writer,
                    clients[job.profile.id],
                    api_keys[job.profile.id],
                    limiters[job.profile.id],
                    global_semaphore,
                    profile_semaphores[job.profile.id],
                )
            )
            for job in pending
        ]
        if tasks:
            await asyncio.gather(*tasks)
        status = "completed"
    except asyncio.CancelledError as error:
        status = "interrupted"
        caught_error = error
    except Exception as error:
        status = "failed"
        caught_error = error
    finally:
        if caught_error is not None:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        if clients:
            await asyncio.gather(*(client.aclose() for client in clients.values()))
        if run_registered:
            if status in {"failed", "interrupted"}:
                await writer.submit(storage.reset_running_jobs, run_id)
            await writer.submit(storage.complete_run, run_id, status, utc_now())
            try:
                from backpack_bench.reporting import build_run_report, serialize_report

                report = build_run_report(storage, run_id)
                report_dir = plan.reports / run_id
                for output_format in ("json", "csv", "html"):
                    atomic_write_text(
                        report_dir / f"report.{output_format}",
                        serialize_report(report, output_format),
                    )
            except Exception as report_error:
                runner_console.print(
                    f"[red]Report generation failed[/red] run={run_id}: {report_error}"
                )
                if caught_error is None:
                    status = "failed"
                    caught_error = report_error
                    await writer.submit(storage.complete_run, run_id, status, utc_now())
        rows = storage.report_rows(run_id) if run_registered else []
        await writer.close()
        storage.close()
    if caught_error is not None:
        if isinstance(caught_error, asyncio.CancelledError):
            raise caught_error
        raise RuntimeError(str(caught_error)) from caught_error
    return {
        "run_id": run_id,
        "status": status,
        "jobs_total": len(jobs),
        "jobs_executed": len(pending),
        "jobs_completed": sum(row["status"] == "completed" for row in rows),
        "database": str(plan.database),
        "artifacts": str(plan.artifacts / run_id),
        "reports": str(plan.reports / run_id),
    }
