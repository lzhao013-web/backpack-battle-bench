"""Local FastAPI application for visual experiments and benchmark runs."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import Field, HttpUrl, SecretStr
from starlette.responses import Response

from backpack_bench import __version__
from backpack_bench.canonical import content_hash
from backpack_bench.evaluation import validate_placement_answer
from backpack_bench.plugins import PluginRegistry
from backpack_bench.prompt import render_visual_prompt
from backpack_bench.providers.base import profile_hash, resolve_api_key
from backpack_bench.reporting import build_run_report, serialize_report
from backpack_bench.runner import (
    ResolvedPlan,
    create_run_id,
    dry_run_summary,
    execute_plan,
    resolve_plan,
)
from backpack_bench.schemas import (
    ModelProfile,
    PlacementAnswer,
    PlacementInput,
    ProviderLimits,
    ProviderProtocol,
    RequestParams,
    StrictModel,
)
from backpack_bench.storage import Storage
from backpack_bench.suite import ResolvedScenario, ResolvedSuite, load_suite

CATEGORY_LABELS_ZH = {"weapon": "武器", "support": "辅助"}
STAT_LABELS_ZH = {"attack": "攻击"}


def _localize_rule_description(value: str) -> str:
    replacements = {**CATEGORY_LABELS_ZH, **STAT_LABELS_ZH}
    for source, target in replacements.items():
        value = value.replace(source, target)
    return value


class ExperimentRequest(StrictModel):
    suite_id: str
    scenario_id: str
    placements: list[PlacementInput]


class WebApiProfileInput(StrictModel):
    protocol: ProviderProtocol
    base_url: HttpUrl
    endpoint: str | None = None
    model: str
    api_key: SecretStr | None = None
    auth_mode: Literal["bearer", "x-api-key", "both", "none"] | None = None
    display_name: str | None = None
    params: RequestParams = Field(default_factory=RequestParams)
    limits: ProviderLimits = Field(default_factory=ProviderLimits)
    verify_tls: bool = True


class WebRunRequest(StrictModel):
    profile: WebApiProfileInput | None = None


@dataclass
class ManagedRun:
    run_id: str
    config_id: str
    status: str
    error: str | None = None
    result: dict[str, Any] | None = None


class WebRunManager:
    """Own background runs so server shutdown can interrupt them cleanly."""

    def __init__(self) -> None:
        self.states: dict[str, ManagedRun] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.lock = asyncio.Lock()

    async def start(
        self,
        config_id: str,
        plan: ResolvedPlan,
        resume_run_id: str | None = None,
        api_key_overrides: dict[str, str] | None = None,
        rerun_job_id: str | None = None,
    ) -> ManagedRun:
        overrides = api_key_overrides or {}
        for profile in plan.profiles:
            if profile.id not in overrides:
                resolve_api_key(profile)
        async with self.lock:
            run_id = resume_run_id or create_run_id(plan)
            existing = self.tasks.get(run_id)
            if existing is not None and not existing.done():
                raise RuntimeError(f"run {run_id} is already running")
            state = ManagedRun(run_id=run_id, config_id=config_id, status="starting")
            self.states[run_id] = state
            task = asyncio.create_task(
                self._worker(state, plan, resume_run_id, overrides, rerun_job_id)
            )
            self.tasks[run_id] = task
            return state

    async def _worker(
        self,
        state: ManagedRun,
        plan: ResolvedPlan,
        resume_run_id: str | None,
        api_key_overrides: dict[str, str],
        rerun_job_id: str | None,
    ) -> None:
        state.status = "running"
        try:
            state.result = await execute_plan(
                plan,
                resume_run_id=resume_run_id,
                new_run_id=None if resume_run_id else state.run_id,
                api_key_overrides=api_key_overrides,
                rerun_job_id=rerun_job_id,
            )
            state.status = str(state.result["status"])
        except asyncio.CancelledError:
            state.status = "interrupted"
            raise
        except Exception as error:
            state.status = "failed"
            state.error = str(error)
        finally:
            api_key_overrides.clear()

    async def stop(self, config_id: str, run_id: str) -> ManagedRun:
        async with self.lock:
            state = self.states.get(run_id)
            task = self.tasks.get(run_id)
            if state is None or state.config_id != config_id or task is None:
                raise ValueError(f"unknown managed run: {run_id}")
            if task.done() or state.status not in {"starting", "running", "stopping"}:
                raise RuntimeError(f"run {run_id} is not running")
            state.status = "stopping"
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return state

    async def forget(self, config_id: str, run_id: str) -> None:
        """Forget a terminal managed run while rejecting deletion of an active task."""
        async with self.lock:
            state = self.states.get(run_id)
            task = self.tasks.get(run_id)
            if state is not None and state.config_id != config_id:
                raise ValueError(f"run {run_id} does not belong to config {config_id}")
            if task is not None and not task.done():
                raise RuntimeError(f"run {run_id} is still running")
            self.states.pop(run_id, None)
            self.tasks.pop(run_id, None)

    async def shutdown(self) -> None:
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@dataclass(frozen=True)
class WebContext:
    workspace: Path
    registry: PluginRegistry
    suites: dict[str, ResolvedSuite]
    run_configs: dict[str, ResolvedPlan]
    run_config_paths: dict[str, Path]
    manager: WebRunManager

    def suite(self, suite_id: str) -> ResolvedSuite:
        try:
            return self.suites[suite_id]
        except KeyError as error:
            raise HTTPException(status_code=404, detail=f"unknown suite: {suite_id}") from error

    def scenario(self, suite_id: str, scenario_id: str) -> ResolvedScenario:
        suite = self.suite(suite_id)
        for scenario in suite.scenarios:
            if scenario.scenario.id == scenario_id:
                return scenario
        raise HTTPException(status_code=404, detail=f"unknown scenario: {scenario_id}")

    def run_config(self, config_id: str) -> ResolvedPlan:
        try:
            return self.run_configs[config_id]
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail=f"unknown run config: {config_id}"
            ) from error


def _discover_suites(workspace: Path, registry: PluginRegistry) -> dict[str, ResolvedSuite]:
    result: dict[str, ResolvedSuite] = {}
    for path in sorted((workspace / "suites").glob("*.yaml")):
        suite = load_suite(path, registry, verify=True)
        if suite.spec.id in result:
            raise ValueError(f"duplicate suite id: {suite.spec.id}")
        result[suite.spec.id] = suite
    return result


def _discover_run_configs(
    workspace: Path,
    registry: PluginRegistry,
) -> tuple[dict[str, ResolvedPlan], dict[str, Path]]:
    plans: dict[str, ResolvedPlan] = {}
    paths: dict[str, Path] = {}
    config_dir = workspace / "configs"
    if not config_dir.exists():
        return plans, paths
    for path in sorted(config_dir.rglob("*.yaml")):
        try:
            plan = resolve_plan(path, registry)
        except (OSError, ValueError):
            continue
        relative = path.relative_to(workspace).as_posix()
        config_id = content_hash(relative)[:12]
        plans[config_id] = plan
        paths[config_id] = path
    return plans, paths


def _scenario_summary(value: ResolvedScenario) -> dict[str, Any]:
    scenario = value.scenario
    return {
        "id": scenario.id,
        "title": scenario.title,
        "difficulty": scenario.difficulty,
        "tags": scenario.tags,
        "board": {
            "width": scenario.board.width,
            "height": scenario.board.height,
            "cells": len(scenario.board.valid_cells()),
        },
        "instances": sum(item.count for item in scenario.items),
        "oracle_attack": value.oracle.optimal_attack,
        "scenario_hash": value.entry.scenario_hash,
        "prompt_hash": value.prompt_hash,
    }


def _scenario_detail(
    suite_id: str,
    value: ResolvedScenario,
    registry: PluginRegistry,
) -> dict[str, Any]:
    scenario = value.scenario
    instances = [
        {
            "item_id": item_id,
            "type_id": item.id,
            "catalog_id": item.catalog_id,
            "image_url": f"/api/suites/{suite_id}/items/{item.catalog_id}/image",
            "card_url": f"/api/suites/{suite_id}/items/{item.catalog_id}/card",
            "display_name": item.display_name,
            "category": item.category,
            "category_label": CATEGORY_LABELS_ZH.get(item.category, item.category),
            "shape": item.shape,
            "rotations": item.rotations,
            "stats": item.stats,
            "stats_zh": {
                STAT_LABELS_ZH.get(name, name): amount for name, amount in item.stats.items()
            },
            "effects": [effect.model_dump(mode="json") for effect in item.effects],
            "effect_descriptions": [
                _localize_rule_description(registry.effect(effect.type).render_zh(effect))
                for effect in item.effects
            ],
        }
        for item_id, item in scenario.expanded_item_ids().items()
    ]
    return {
        "scenario": scenario.model_dump(mode="json", exclude_none=True),
        "scenario_hash": value.entry.scenario_hash,
        "prompt": value.prompt,
        "prompt_hash": value.prompt_hash,
        "valid_cells": [list(cell) for cell in sorted(scenario.board.valid_cells())],
        "instances": instances,
        "oracle": value.oracle.model_dump(mode="json", exclude_none=True),
        "sheet_url": f"/api/suites/{suite_id}/scenarios/{scenario.id}/sheet",
        "sheet_urls": {
            "visual_shape": (
                f"/api/suites/{suite_id}/scenarios/{scenario.id}/sheet?mode=visual_shape"
            ),
            "visual_full": f"/api/suites/{suite_id}/scenarios/{scenario.id}/sheet",
        },
        "visual_prompts": {
            mode: render_visual_prompt(scenario, registry, mode)
            for mode in ("visual_shape", "visual_full")
        },
    }


def _key_ready(plan: ResolvedPlan) -> bool:
    return all(
        profile.auth_mode == "none"
        or (profile.api_key_env is not None and bool(os.getenv(profile.api_key_env)))
        for profile in plan.profiles
    )


def _runtime_plan(
    base_plan: ResolvedPlan,
    value: WebApiProfileInput | None,
) -> tuple[ResolvedPlan, dict[str, str]]:
    if value is None:
        return base_plan, {}
    model = value.model.strip()
    if not model:
        raise ValueError("模型名不能为空")
    auth_mode = value.auth_mode
    api_key = value.api_key.get_secret_value() if value.api_key is not None else ""
    if auth_mode != "none" and not api_key:
        raise ValueError("当前鉴权方式需要 API Key")
    profile = ModelProfile(
        id="web-runtime",
        display_name=(value.display_name or model).strip() or model,
        protocol=value.protocol,
        base_url=value.base_url,
        endpoint=value.endpoint or None,
        model=model,
        api_key_env="BBBENCH_WEB_RUNTIME_API_KEY" if auth_mode != "none" else None,
        auth_mode=auth_mode,
        params=value.params,
        limits=value.limits,
        verify_tls=value.verify_tls,
    )
    current_profile_hash = profile_hash(profile)
    profile = profile.model_copy(update={"id": f"web-{current_profile_hash[:12]}"})
    plan_hash = content_hash(
        {
            "plan": base_plan.spec,
            "suite_hash": base_plan.suite.suite_hash,
            "profile_hashes": [current_profile_hash],
        }
    )
    plan = replace(base_plan, profiles=(profile,), plan_hash=plan_hash)
    overrides = {profile.id: api_key} if auth_mode != "none" else {}
    return plan, overrides


def _config_summary(context: WebContext, config_id: str, plan: ResolvedPlan) -> dict[str, Any]:
    return {
        "id": config_id,
        "path": context.run_config_paths[config_id].relative_to(context.workspace).as_posix(),
        "plan_id": plan.spec.id,
        "plan_hash": plan.plan_hash,
        "suite_id": plan.suite.spec.id,
        "profiles": [
            {
                "id": profile.id,
                "display_name": profile.display_name or profile.id,
                "protocol": profile.protocol,
                "model": profile.model,
                "thinking_effort": profile.params.thinking_effort,
            }
            for profile in plan.profiles
        ],
        "jobs": len(plan.profiles) * len(plan.suite.scenarios) * plan.spec.trials,
        "trials": plan.spec.trials,
        "concurrency": plan.spec.concurrency,
        "prompt_mode": plan.spec.prompt_mode,
        "key_ready": _key_ready(plan),
    }


def _output_tokens(usage_json: str | None) -> int:
    if not usage_json:
        return 0
    try:
        usage = json.loads(usage_json)
    except (TypeError, json.JSONDecodeError):
        return 0
    if not isinstance(usage, dict):
        return 0
    try:
        return int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
    except (TypeError, ValueError):
        return 0


def _output_tokens_estimated(usage_json: str | None) -> bool:
    if not usage_json:
        return False
    try:
        usage = json.loads(usage_json)
    except (TypeError, json.JSONDecodeError):
        return False
    return bool(usage.get("output_tokens_estimated")) if isinstance(usage, dict) else False


def _job_payload(row: dict[str, Any]) -> dict[str, Any]:
    has_result = row["result_job_id"] is not None
    if has_result:
        output_tokens: int | None = _output_tokens(row["usage_json"])
        output_tokens_estimated: bool | None = _output_tokens_estimated(row["usage_json"])
    elif row["live_output_tokens"] is not None:
        output_tokens = int(row["live_output_tokens"])
        output_tokens_estimated = bool(row["live_tokens_estimated"])
    else:
        output_tokens = None
        output_tokens_estimated = None
    return {
        "job_id": row["job_id"],
        "profile_id": row["profile_id"],
        "display_name": row["display_name"],
        "model": row["model"],
        "scenario_id": row["scenario_id"],
        "title": row["title"],
        "trial": row["trial"],
        "status": row["status"],
        "output_tokens": output_tokens,
        "output_tokens_estimated": output_tokens_estimated,
        "valid": bool(row["valid"]) if has_result else None,
        "error_type": row["error_type"] if has_result else None,
        "actual_attack": int(row["actual_attack"]) if has_result else None,
        "oracle_attack": int(row["oracle_attack"]) if has_result else None,
        "latency_ms": float(row["latency_ms"]) if has_result else None,
        "attempt_count": row["attempt_count"],
    }


def _stage_run_directories(plan: ResolvedPlan, run_id: str) -> list[tuple[Path, Path]]:
    staged: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    try:
        for root in (plan.artifacts.resolve(), plan.reports.resolve()):
            target = (root / run_id).resolve()
            if target.parent != root:
                raise ValueError(f"unsafe run directory: {target}")
            if target in seen or not target.exists():
                continue
            seen.add(target)
            trash = root / f".{run_id}.deleting-{uuid.uuid4().hex}"
            target.rename(trash)
            staged.append((target, trash))
    except Exception:
        for target, trash in reversed(staged):
            if trash.exists() and not target.exists():
                trash.rename(target)
        raise
    return staged


def _restore_run_directories(staged: list[tuple[Path, Path]]) -> None:
    for target, trash in reversed(staged):
        if trash.exists() and not target.exists():
            trash.rename(target)


def _purge_run_directories(staged: list[tuple[Path, Path]]) -> list[str]:
    errors: list[str] = []
    for _, trash in staged:
        try:
            if trash.is_dir():
                shutil.rmtree(trash)
            else:
                trash.unlink()
        except OSError as error:
            errors.append(f"{trash}: {error}")
    return errors


def _run_payload(
    context: WebContext,
    config_id: str,
    run_id: str,
    include_report: bool = True,
    storage: Storage | None = None,
) -> dict[str, Any]:
    plan = context.run_config(config_id)
    managed = context.manager.states.get(run_id)
    if not plan.database.exists() and managed is not None:
        return {
            "run_id": run_id,
            "status": managed.status,
            "error": managed.error,
            "progress": {
                "total": 0,
                "pending": 0,
                "running": 0,
                "completed": 0,
                "attempts": 0,
                "valid": 0,
            },
            "jobs": [],
            "report": None,
        }
    owns_storage = storage is None
    storage = storage or Storage(plan.database)
    try:
        run = storage.get_run(run_id)
        if run is None:
            if managed is None:
                raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
            return {
                "run_id": run_id,
                "status": managed.status,
                "error": managed.error,
                "progress": {
                    "total": 0,
                    "pending": 0,
                    "running": 0,
                    "completed": 0,
                    "attempts": 0,
                    "valid": 0,
                },
                "jobs": [],
                "report": None,
            }
        report = build_run_report(storage, run_id) if include_report else None
        return {
            "run_id": run_id,
            "plan_id": run["plan_id"],
            "plan_hash": run["plan_hash"],
            "status": managed.status if managed is not None else run["status"],
            "error": managed.error if managed else None,
            "started_at": run["started_at"],
            "completed_at": run["completed_at"],
            "progress": storage.run_progress(run_id),
            "profiles": storage.run_profiles(run_id),
            "jobs": [_job_payload(row) for row in storage.run_job_rows(run_id)],
            "report": report,
        }
    finally:
        if owns_storage:
            storage.close()


def create_app(workspace: Path | None = None) -> FastAPI:
    root = (workspace or Path.cwd()).resolve()
    load_dotenv(root / ".env")
    registry = PluginRegistry()
    suites = _discover_suites(root, registry)
    run_configs, run_config_paths = _discover_run_configs(root, registry)
    manager = WebRunManager()
    context = WebContext(
        workspace=root,
        registry=registry,
        suites=suites,
        run_configs=run_configs,
        run_config_paths=run_config_paths,
        manager=manager,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await manager.shutdown()

    app = FastAPI(
        title="Backpack Battle Bench",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )
    static_dir = Path(__file__).with_name("web_static")
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "workspace": str(root),
            "suites": len(context.suites),
            "run_configs": len(context.run_configs),
        }

    @app.get("/api/suites")
    async def list_suites() -> list[dict[str, Any]]:
        return [
            {
                "id": suite.spec.id,
                "title": suite.spec.title,
                "version": suite.spec.version,
                "suite_hash": suite.suite_hash,
                "item_catalog": {
                    "id": suite.catalog.id,
                    "version": suite.catalog.version,
                    "hash": suite.catalog_hash,
                },
                "visual_pack": {
                    "id": suite.visual_pack.spec.id,
                    "version": suite.visual_pack.spec.version,
                    "status": suite.visual_pack.spec.status,
                    "hash": suite.visual_pack.pack_hash,
                },
                "scenarios": [_scenario_summary(item) for item in suite.scenarios],
            }
            for suite in context.suites.values()
        ]

    @app.get("/api/suites/{suite_id}/scenarios/{scenario_id}")
    async def scenario_detail(suite_id: str, scenario_id: str) -> dict[str, Any]:
        return _scenario_detail(
            suite_id,
            context.scenario(suite_id, scenario_id),
            context.registry,
        )

    @app.get("/api/suites/{suite_id}/items/{item_id}/image")
    async def item_image(
        suite_id: str,
        item_id: str,
        rotation: int = Query(0),
    ) -> Response:
        suite = context.suite(suite_id)
        try:
            _, path = suite.visual_pack.asset(item_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        definition = next((item for item in suite.catalog.items if item.id == item_id), None)
        if definition is None:
            raise HTTPException(status_code=404, detail=f"unknown item: {item_id}")
        if rotation not in {0, 90, 180, 270}:
            raise HTTPException(status_code=400, detail=f"invalid rotation: {rotation}")
        if rotation not in definition.rotations:
            raise HTTPException(
                status_code=400,
                detail=f"item {item_id} does not allow rotation {rotation}",
            )
        if rotation == 0:
            return FileResponse(path)
        transpose = {
            90: Image.Transpose.ROTATE_270,
            180: Image.Transpose.ROTATE_180,
            270: Image.Transpose.ROTATE_90,
        }[rotation]
        with Image.open(path) as source:
            rotated = source.convert("RGBA").transpose(transpose)
            buffer = io.BytesIO()
            rotated.save(buffer, format="PNG", optimize=True)
        return Response(
            content=buffer.getvalue(),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/api/suites/{suite_id}/items/{item_id}/card")
    async def item_card(suite_id: str, item_id: str) -> FileResponse:
        suite = context.suite(suite_id)
        try:
            _, path = suite.visual_pack.card(item_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return FileResponse(path)

    @app.get("/api/suites/{suite_id}/scenarios/{scenario_id}/sheet")
    async def scenario_sheet(
        suite_id: str,
        scenario_id: str,
        mode: Literal["visual_shape", "visual_full"] = Query("visual_full"),
    ) -> FileResponse:
        suite = context.suite(suite_id)
        resolved = context.scenario(suite_id, scenario_id)
        try:
            path = suite.visual_pack.scenario_sheet(
                scenario_id,
                resolved.entry.scenario_hash,
                mode,
            )
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return FileResponse(path)

    @app.post("/api/evaluate")
    async def evaluate(request: ExperimentRequest) -> dict[str, Any]:
        resolved = context.scenario(request.suite_id, request.scenario_id)
        answer = PlacementAnswer(placements=request.placements)
        return validate_placement_answer(resolved.scenario, answer, context.registry)

    @app.get("/api/run-configs")
    async def list_run_configs() -> list[dict[str, Any]]:
        return [
            _config_summary(context, config_id, plan)
            for config_id, plan in context.run_configs.items()
        ]

    @app.get("/api/run-configs/{config_id}/preview")
    async def run_preview(config_id: str) -> dict[str, Any]:
        plan = context.run_config(config_id)
        return {**dry_run_summary(plan), "key_ready": _key_ready(plan)}

    @app.get("/api/run-configs/{config_id}/runs")
    async def list_runs(config_id: str) -> list[dict[str, Any]]:
        plan = context.run_config(config_id)
        storage = Storage(plan.database)
        try:
            runs = storage.list_runs(plan_id=plan.spec.id, limit=100)
            result = []
            for run in runs:
                run_id = str(run["run_id"])
                managed = context.manager.states.get(run_id)
                result.append(
                    {
                        "run_id": run["run_id"],
                        "plan_hash": run["plan_hash"],
                        "status": managed.status if managed is not None else run["status"],
                        "started_at": run["started_at"],
                        "completed_at": run["completed_at"],
                        "progress": storage.run_progress(run_id),
                        "profiles": storage.run_profiles(run_id),
                    }
                )
            return result
        finally:
            storage.close()

    @app.post(
        "/api/run-configs/{config_id}/runs",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_run(
        config_id: str,
        request: WebRunRequest | None = None,
    ) -> dict[str, Any]:
        base_plan = context.run_config(config_id)
        try:
            plan, api_key_overrides = _runtime_plan(
                base_plan,
                request.profile if request is not None else None,
            )
            managed = await manager.start(
                config_id,
                plan,
                api_key_overrides=api_key_overrides,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"run_id": managed.run_id, "status": managed.status}

    @app.post(
        "/api/run-configs/{config_id}/runs/{run_id}/resume",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def resume_run(
        config_id: str,
        run_id: str,
        request: WebRunRequest | None = None,
    ) -> dict[str, Any]:
        base_plan = context.run_config(config_id)
        try:
            plan, api_key_overrides = _runtime_plan(
                base_plan,
                request.profile if request is not None else None,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        storage = Storage(plan.database)
        try:
            run = storage.get_run(run_id)
            if run is None:
                raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
            if run["plan_hash"] != plan.plan_hash:
                raise HTTPException(status_code=409, detail="run does not match this config")
        finally:
            storage.close()
        try:
            managed = await manager.start(
                config_id,
                plan,
                resume_run_id=run_id,
                api_key_overrides=api_key_overrides,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"run_id": managed.run_id, "status": managed.status}

    @app.post(
        "/api/run-configs/{config_id}/runs/{run_id}/jobs/{job_id}/rerun",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def rerun_zero_score_job(
        config_id: str,
        run_id: str,
        job_id: str,
        request: WebRunRequest | None = None,
    ) -> dict[str, Any]:
        base_plan = context.run_config(config_id)
        try:
            plan, api_key_overrides = _runtime_plan(
                base_plan,
                request.profile if request is not None else None,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        storage = Storage(plan.database)
        try:
            run = storage.get_run(run_id)
            if run is None:
                raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
            if run["plan_hash"] != plan.plan_hash:
                raise HTTPException(status_code=409, detail="run does not match this config")
            if run["status"] != "completed":
                raise HTTPException(status_code=409, detail="只有已完成的 Run 可以重跑 Job")
            job = next(
                (row for row in storage.run_job_rows(run_id) if row["job_id"] == job_id),
                None,
            )
            if job is None:
                raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
            if job["result_job_id"] is None or int(job["actual_attack"]) != 0:
                raise HTTPException(status_code=409, detail="只有得分为 0 的 Job 可以重跑")
        finally:
            storage.close()
        try:
            managed = await manager.start(
                config_id,
                plan,
                resume_run_id=run_id,
                api_key_overrides=api_key_overrides,
                rerun_job_id=job_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "run_id": managed.run_id,
            "status": managed.status,
            "job_id": job_id,
        }

    @app.get("/api/run-configs/{config_id}/runs/{run_id}")
    async def run_status(config_id: str, run_id: str) -> dict[str, Any]:
        return _run_payload(context, config_id, run_id)

    @app.get("/api/run-configs/{config_id}/runs/{run_id}/events")
    async def run_events(config_id: str, run_id: str, request: Request) -> StreamingResponse:
        initial = _run_payload(context, config_id, run_id, include_report=False)
        plan = context.run_config(config_id)

        async def event_stream() -> AsyncIterator[str]:
            payload = initial
            stream_storage: Storage | None = None
            try:
                while True:
                    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    yield f"data: {encoded}\n\n"
                    if payload["status"] not in {"starting", "running", "stopping"}:
                        return
                    await asyncio.sleep(0.25)
                    if await request.is_disconnected():
                        return
                    if stream_storage is None and plan.database.exists():
                        stream_storage = Storage(plan.database)
                    payload = _run_payload(
                        context,
                        config_id,
                        run_id,
                        include_report=False,
                        storage=stream_storage,
                    )
            finally:
                if stream_storage is not None:
                    stream_storage.close()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.delete("/api/run-configs/{config_id}/runs/{run_id}")
    async def delete_run(config_id: str, run_id: str) -> dict[str, Any]:
        plan = context.run_config(config_id)
        storage = Storage(plan.database)
        try:
            run = storage.get_run(run_id)
            if run is None or run["plan_id"] != plan.spec.id:
                raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
            if run["status"] in {"starting", "running", "stopping"}:
                raise HTTPException(status_code=409, detail="运行中的 Run 不能删除，请先中断")
        finally:
            storage.close()
        try:
            await manager.forget(config_id, run_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

        try:
            staged = _stage_run_directories(plan, run_id)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=500, detail=f"无法暂存 Run 产物：{error}") from error
        delete_storage: Storage | None = None
        try:
            delete_storage = Storage(plan.database)
            delete_storage.delete_run(run_id)
        except Exception as error:
            _restore_run_directories(staged)
            raise HTTPException(status_code=500, detail=f"无法删除 Run 记录：{error}") from error
        finally:
            if delete_storage is not None:
                delete_storage.close()
        cleanup_errors = _purge_run_directories(staged)
        return {
            "run_id": run_id,
            "deleted": True,
            "removed_directories": len(staged),
            "cleanup_errors": cleanup_errors,
        }

    @app.post(
        "/api/run-configs/{config_id}/runs/{run_id}/stop",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def stop_run(config_id: str, run_id: str) -> dict[str, Any]:
        context.run_config(config_id)
        try:
            managed = await manager.stop(config_id, run_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"run_id": managed.run_id, "status": managed.status}

    @app.get("/api/run-configs/{config_id}/runs/{run_id}/report")
    async def run_report(
        config_id: str,
        run_id: str,
        output_format: Literal["json", "csv", "html"] = Query("json", alias="format"),
    ) -> Response:
        plan = context.run_config(config_id)
        storage = Storage(plan.database)
        try:
            report = build_run_report(storage, run_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        finally:
            storage.close()
        if output_format == "json":
            return JSONResponse(report)
        text = serialize_report(report, output_format)
        if output_format == "html":
            return HTMLResponse(text)
        return PlainTextResponse(
            text,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{run_id}.csv"'},
        )

    return app
