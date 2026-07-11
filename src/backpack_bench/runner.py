"""Resumable local model-matrix execution engine."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from backpack_bench import __version__
from backpack_bench.canonical import content_hash
from backpack_bench.evaluation import parse_and_score_output
from backpack_bench.io import atomic_write_json, atomic_write_text, load_yaml
from backpack_bench.plugins import PluginRegistry
from backpack_bench.prompt import PROMPT_TEMPLATE_VERSION
from backpack_bench.providers import adapter_for
from backpack_bench.providers.base import (
    ParsedCompletion,
    profile_hash,
    redact_headers,
    redact_secret_values,
    resolve_api_key,
)
from backpack_bench.schemas import ModelProfile, ModelsConfig, RunPlan
from backpack_bench.storage import DbWriter, Storage
from backpack_bench.suite import ResolvedScenario, ResolvedSuite, load_suite


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
                        trial=trial,
                    )
                )
    return jobs


def dry_run_summary(plan: ResolvedPlan) -> dict[str, Any]:
    return {
        "plan_id": plan.spec.id,
        "plan_hash": plan.plan_hash,
        "suite_id": plan.suite.spec.id,
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


async def _call_once(
    job: JobContext,
    client: httpx.AsyncClient,
    api_key: str | None,
    limiter: RateLimiter,
    registry: PluginRegistry,
) -> AttemptOutcome:
    adapter = adapter_for(job.profile)
    endpoint = adapter.endpoint(job.profile)
    headers = adapter.headers(job.profile, api_key)
    body = adapter.body(job.profile, job.scenario.prompt)
    await limiter.wait()
    started = time.perf_counter()
    try:
        response = await client.post(endpoint, headers=headers, json=body)
    except httpx.HTTPError as error:
        return AttemptOutcome(
            retryable=True,
            error_type="api_transport",
            error_message=str(error),
            http_status=None,
            response_json=None,
            response_text=None,
            completion=None,
            validation=None,
            latency_ms=(time.perf_counter() - started) * 1000,
            retry_after=None,
        )
    latency_ms = (time.perf_counter() - started) * 1000
    response_text = response.text
    try:
        response_json = response.json()
    except json.JSONDecodeError:
        response_json = None
    if not 200 <= response.status_code < 300:
        return AttemptOutcome(
            retryable=response.status_code in {408, 429} or response.status_code >= 500,
            error_type="api_http",
            error_message=f"HTTP {response.status_code}: {response_text[:2000]}",
            http_status=response.status_code,
            response_json=response_json,
            response_text=response_text,
            completion=None,
            validation=None,
            latency_ms=latency_ms,
            retry_after=_retry_after(response.headers),
        )
    if response_json is None:
        return AttemptOutcome(
            retryable=False,
            error_type="api_schema",
            error_message="successful response was not JSON",
            http_status=response.status_code,
            response_json=None,
            response_text=response_text,
            completion=None,
            validation=None,
            latency_ms=latency_ms,
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
            latency_ms=latency_ms,
            retry_after=None,
        )
    validation = parse_and_score_output(
        job.scenario.scenario,
        completion.content,
        registry,
        finish_reason=completion.finish_reason,
    )
    return AttemptOutcome(
        retryable=False,
        error_type=None,
        error_message=None,
        http_status=response.status_code,
        response_json=response_json,
        response_text=response_text,
        completion=completion,
        validation=validation,
        latency_ms=latency_ms,
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
    path = plan.artifacts / job.run_id / job.job_id / f"attempt_{attempt_no:03d}"
    path.mkdir(parents=True, exist_ok=False)
    atomic_write_json(
        path / "request.json",
        redact_secret_values(
            {
                "url": adapter.endpoint(job.profile),
                "headers": redact_headers(adapter.headers(job.profile, api_key)),
                "body": adapter.body(job.profile, job.scenario.prompt),
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
                "error_type": outcome.error_type,
                "error_message": outcome.error_message,
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
        first_attempt = _next_attempt_no(storage, plan, job)
        final_outcome: AttemptOutcome | None = None
        final_artifact_dir: Path | None = None
        for offset in range(job.profile.limits.retries + 1):
            attempt_no = first_attempt + offset
            started_at = utc_now()
            outcome = await _call_once(job, client, api_key, limiter, plan.registry)
            completed_at = utc_now()
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
                    "error_type": outcome.error_type,
                    "error_message": redact_secret_values(outcome.error_message, api_key),
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
) -> dict[str, Any]:
    if resume_run_id is not None and new_run_id is not None:
        raise ValueError("resume_run_id and new_run_id are mutually exclusive")
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
            storage.reset_interrupted(run_id)
            run_registered = True
        else:
            storage.create_run(
                {
                    "run_id": run_id,
                    "plan_id": plan.spec.id,
                    "plan_hash": plan.plan_hash,
                    "suite_id": plan.suite.spec.id,
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
                atomic_write_text(prompt_path, scenario.prompt)
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
                    "engine_version": __version__,
                    "prompt_template_version": PROMPT_TEMPLATE_VERSION,
                    "prompt_hashes": {
                        scenario.entry.scenario_hash: scenario.prompt_hash
                        for scenario in plan.suite.scenarios
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
            await writer.submit(storage.complete_run, run_id, status, utc_now())
            if caught_error is None:
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
