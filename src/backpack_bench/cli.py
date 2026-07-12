"""Unified bbbench command-line interface."""

from __future__ import annotations

import asyncio
import json
import threading
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from pydantic import BaseModel
from rich.console import Console

from backpack_bench.canonical import text_hash
from backpack_bench.catalog import load_item_catalog, load_scenario, load_visual_pack
from backpack_bench.evaluation import scenario_hash
from backpack_bench.generator import generate as generate_scenarios
from backpack_bench.io import atomic_write_json, atomic_write_text, load_yaml
from backpack_bench.oracle import solve_exact
from backpack_bench.plugins import PluginRegistry
from backpack_bench.prompt import PROMPT_TEMPLATE_VERSION, render_prompt
from backpack_bench.reporting import (
    build_leaderboard,
    build_run_report,
    console_report,
    group_report_view,
    serialize_report,
)
from backpack_bench.runner import dry_run_summary, execute_plan, resolve_plan
from backpack_bench.schemas import (
    GeneratorSpec,
    ItemCatalogSpec,
    ModelsConfig,
    PlacementAnswer,
    RunPlan,
    ScenarioDocumentSpec,
    SuiteSpec,
    VisualPackSpec,
)
from backpack_bench.static_site import build_static_site, export_results_snapshot
from backpack_bench.storage import Storage
from backpack_bench.suite import load_suite
from backpack_bench.visual import render_card_visual_pack, scaffold_visual_pack

app = typer.Typer(no_args_is_help=True, help="可扩展、可复现的文字/视觉二维背包评测。")
schema_app = typer.Typer(no_args_is_help=True)
scenario_app = typer.Typer(no_args_is_help=True)
oracle_app = typer.Typer(no_args_is_help=True)
suite_app = typer.Typer(no_args_is_help=True)
visual_app = typer.Typer(no_args_is_help=True)
site_app = typer.Typer(no_args_is_help=True)
app.add_typer(schema_app, name="schema")
app.add_typer(scenario_app, name="scenario")
app.add_typer(oracle_app, name="oracle")
app.add_typer(suite_app, name="suite")
app.add_typer(visual_app, name="visual")
app.add_typer(site_app, name="site")
console = Console()


def _print_json(value: object) -> None:
    console.print_json(json.dumps(value, ensure_ascii=False, default=str))


def _fail(error: Exception) -> None:
    console.print(f"[red]error:[/red] {error}")
    raise typer.Exit(1)


@schema_app.command("export")
def export_schema(
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("schemas"),
) -> None:
    """Export the versioned JSON schemas used by public config files."""
    models: dict[str, type[BaseModel]] = {
        "scenario.schema.json": ScenarioDocumentSpec,
        "item-catalog.schema.json": ItemCatalogSpec,
        "visual-pack.schema.json": VisualPackSpec,
        "suite.schema.json": SuiteSpec,
        "models.schema.json": ModelsConfig,
        "run.schema.json": RunPlan,
        "generator.schema.json": GeneratorSpec,
        "answer.schema.json": PlacementAnswer,
    }
    for name, model in models.items():
        atomic_write_json(output / name, model.model_json_schema())
    _print_json({"output": str(output.resolve()), "files": sorted(models)})


@scenario_app.command("validate")
def validate_scenario(
    path: Path,
    show_prompt: Annotated[bool, typer.Option("--show-prompt")] = False,
) -> None:
    """Validate a scenario and render its official prompt."""
    try:
        registry = PluginRegistry()
        loaded = load_scenario(path.resolve())
        scenario = loaded.scenario
        prompt = render_prompt(scenario, registry)
        result = {
            "valid": True,
            "scenario_id": scenario.id,
            "scenario_hash": scenario_hash(scenario, registry),
            "prompt_template_version": PROMPT_TEMPLATE_VERSION,
            "prompt_hash": text_hash(prompt),
            "items": sum(item.count for item in scenario.items),
            "item_catalog_id": loaded.catalog.id,
            "item_catalog_hash": loaded.catalog_hash,
            "board_cells": len(scenario.board.valid_cells()),
        }
        _print_json(result)
        if show_prompt:
            console.print(prompt)
    except Exception as error:
        _fail(error)


@app.command("generate")
def generate_command(config: Path) -> None:
    """Generate exact-oracle scenarios from a stable seed config."""
    try:
        path = config.resolve()
        spec = load_yaml(path, GeneratorSpec)
        result = generate_scenarios(spec, path.parent, PluginRegistry())
        _print_json(result.model_dump(mode="json"))
    except Exception as error:
        _fail(error)


@oracle_app.command("solve")
def solve_oracle(
    scenario_path: Path,
    timeout: Annotated[float, typer.Option("--timeout")] = 60.0,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Prove the exact optimum for one scenario."""
    try:
        scenario = load_scenario(scenario_path.resolve()).scenario
        oracle = solve_exact(scenario, PluginRegistry(), timeout)
        if output is not None:
            atomic_write_json(output, oracle)
        _print_json(oracle.model_dump(mode="json", exclude_none=True))
        if not oracle.exact:
            raise typer.Exit(2)
    except typer.Exit:
        raise
    except Exception as error:
        _fail(error)


@suite_app.command("validate")
def validate_suite(path: Path) -> None:
    """Verify scenario/oracle hashes and every exact witness."""
    try:
        resolved = load_suite(path.resolve(), PluginRegistry(), verify=True)
        _print_json(
            {
                "valid": True,
                "suite_id": resolved.spec.id,
                "suite_hash": resolved.suite_hash,
                "scenarios": len(resolved.scenarios),
                "total_weight": sum(item.entry.weight for item in resolved.scenarios),
            }
        )
    except Exception as error:
        _fail(error)


@visual_app.command("scaffold")
def scaffold_visuals(
    catalog_path: Path,
    output: Annotated[Path, typer.Option("--output", "-o")],
    pack_id: Annotated[str, typer.Option("--id")] = "placeholder-v1",
    version: Annotated[str, typer.Option("--version")] = "1.0.0",
    cell_size: Annotated[int, typer.Option("--cell-size", min=32, max=1024)] = 128,
) -> None:
    """Generate deterministic shape placeholders and a frozen visual-pack manifest."""
    try:
        catalog = load_item_catalog(catalog_path.resolve())
        manifest = scaffold_visual_pack(catalog, output, pack_id, version, cell_size)
        resolved = load_visual_pack(manifest, catalog)
        _print_json(
            {
                "manifest": str(manifest),
                "visual_pack_hash": resolved.pack_hash,
                "assets": len(resolved.spec.assets),
            }
        )
    except Exception as error:
        _fail(error)


@visual_app.command("render-suite")
def render_suite_visuals(
    suites: list[Path],
    output: Annotated[Path, typer.Option("--output", "-o")],
    pack_id: Annotated[str, typer.Option("--id")] = "visual-card-v1",
    version: Annotated[str, typer.Option("--version")] = "1.0.0",
    cell_size: Annotated[int, typer.Option("--cell-size", min=64, max=256)] = 128,
    art_sources: Annotated[
        Path | None,
        typer.Option("--art-sources", help="Generated PNG source directory"),
    ] = None,
) -> None:
    """Render item cards and complete PNG scenario sheets for one shared catalog."""
    try:
        registry = PluginRegistry()
        resolved_suites = [load_suite(path.resolve(), registry) for path in suites]
        if not resolved_suites:
            raise ValueError("at least one suite is required")
        catalog_hashes = {suite.catalog_hash for suite in resolved_suites}
        if len(catalog_hashes) != 1:
            raise ValueError("all suites must use the same item catalog")
        scenarios = {
            scenario.scenario.id: (scenario.scenario, scenario.entry.scenario_hash)
            for suite in resolved_suites
            for scenario in suite.scenarios
        }
        catalog = resolved_suites[0].catalog
        manifest = render_card_visual_pack(
            catalog,
            list(scenarios.values()),
            registry,
            output,
            pack_id,
            version,
            cell_size,
            art_sources,
        )
        resolved = load_visual_pack(manifest, catalog)
        _print_json(
            {
                "manifest": str(manifest),
                "visual_pack_hash": resolved.pack_hash,
                "assets": len(resolved.spec.assets),
                "cards": len(resolved.spec.cards),
                "scenario_sheets": len(resolved.spec.scenario_sheets),
            }
        )
    except Exception as error:
        _fail(error)


@app.command("run")
def run_command(
    config: Path,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    resume: Annotated[str | None, typer.Option("--resume")] = None,
) -> None:
    """Execute or resume a model × scenario × trial matrix."""
    try:
        plan = resolve_plan(config.resolve(), PluginRegistry())
        if dry_run:
            _print_json(dry_run_summary(plan))
            return
        load_dotenv(Path.cwd() / ".env")
        result = asyncio.run(execute_plan(plan, resume_run_id=resume))
        _print_json(result)
    except KeyboardInterrupt:
        console.print(
            "[yellow]run interrupted; completed jobs were preserved for --resume[/yellow]"
        )
        raise typer.Exit(130) from None
    except Exception as error:
        _fail(error)


def _write_or_print_report(
    value: dict[str, object],
    output_format: str,
    output: Path | None,
) -> None:
    if output_format == "console":
        if output is not None:
            raise ValueError("console report cannot be written with --output")
        console_report(value, console)
        return
    text = serialize_report(value, output_format)
    if output is None:
        print(text, end="")
    else:
        atomic_write_text(output, text)
        _print_json({"output": str(output.resolve()), "format": output_format})


@app.command("report")
def report_command(
    run_id: str,
    output_format: Annotated[str, typer.Option("--format", "-f")] = "console",
    database: Annotated[Path, typer.Option("--database", "-d")] = Path(".bbbench/results.sqlite3"),
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    group_by: Annotated[str | None, typer.Option("--group-by")] = None,
) -> None:
    """Render one run as console, JSON, CSV or static HTML."""
    try:
        if output_format not in {"console", "json", "csv", "html"}:
            raise ValueError("format must be console, json, csv or html")
        storage = Storage(database)
        try:
            value = group_report_view(build_run_report(storage, run_id), group_by)
        finally:
            storage.close()
        _write_or_print_report(value, output_format, output)
    except Exception as error:
        _fail(error)


@app.command("leaderboard")
def leaderboard_command(
    suite_id: str,
    output_format: Annotated[str, typer.Option("--format", "-f")] = "console",
    database: Annotated[Path, typer.Option("--database", "-d")] = Path(".bbbench/results.sqlite3"),
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Rank the latest run of every complete model configuration."""
    try:
        if output_format not in {"console", "json", "csv", "html"}:
            raise ValueError("format must be console, json, csv or html")
        storage = Storage(database)
        try:
            value = build_leaderboard(storage, suite_id)
        finally:
            storage.close()
        _write_or_print_report(value, output_format, output)
    except Exception as error:
        _fail(error)


@site_app.command("snapshot")
def site_snapshot_command(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("."),
    database: Annotated[Path, typer.Option("--database", "-d")] = Path(".bbbench/results.sqlite3"),
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("leaderboard/results.json"),
) -> None:
    """Export aggregate-only public leaderboard results from a local database."""
    try:
        path = export_results_snapshot(
            workspace.resolve(),
            database.resolve(),
            output.resolve(),
        )
        _print_json({"snapshot": str(path)})
    except Exception as error:
        _fail(error)


@site_app.command("build")
def site_build_command(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("."),
    output: Annotated[Path, typer.Option("--output", "-o")] = Path(".bbbench/pages"),
    snapshot: Annotated[Path | None, typer.Option("--snapshot")] = None,
) -> None:
    """Build the standalone backend-free leaderboard for GitHub Pages."""
    try:
        path = build_static_site(
            workspace.resolve(),
            output.resolve(),
            snapshot.resolve() if snapshot is not None else None,
        )
        _print_json({"site": str(path), "index": str(path / "index.html")})
    except Exception as error:
        _fail(error)


@app.command("web")
def web_command(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8000,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("."),
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
) -> None:
    """Launch the local graphical scenario lab and run console."""
    try:
        import uvicorn

        from backpack_bench.web import create_app

        application = create_app(workspace.resolve())
        browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        url = f"http://{browser_host}:{port}/"
        console.print(f"Backpack Battle Bench Web: [link={url}]{url}[/link]")
        if open_browser:
            threading.Timer(0.6, webbrowser.open, args=(url,)).start()
        uvicorn.run(application, host=host, port=port, log_level="info")
    except Exception as error:
        _fail(error)


if __name__ == "__main__":
    app()
