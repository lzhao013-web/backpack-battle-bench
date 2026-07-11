"""Core bbbench command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel
from rich.console import Console

from backpack_bench.canonical import text_hash
from backpack_bench.evaluation import scenario_hash
from backpack_bench.io import atomic_write_json, load_yaml
from backpack_bench.plugins import PluginRegistry
from backpack_bench.prompt import PROMPT_TEMPLATE_VERSION, render_prompt
from backpack_bench.schemas import PlacementAnswer, ScenarioSpec

app = typer.Typer(no_args_is_help=True, help="纯文字二维背包评测。")
schema_app = typer.Typer(no_args_is_help=True)
scenario_app = typer.Typer(no_args_is_help=True)
app.add_typer(schema_app, name="schema")
app.add_typer(scenario_app, name="scenario")
console = Console()


def _print_json(value: object) -> None:
    console.print_json(json.dumps(value, ensure_ascii=False, default=str))


@schema_app.command("export")
def export_schema(
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("schemas"),
) -> None:
    """Export the core public JSON schemas."""
    models: dict[str, type[BaseModel]] = {
        "scenario.schema.json": ScenarioSpec,
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
    """Validate a scenario and render its generated prompt."""
    registry = PluginRegistry()
    scenario = load_yaml(path.resolve(), ScenarioSpec)
    prompt = render_prompt(scenario, registry)
    _print_json(
        {
            "valid": True,
            "scenario_id": scenario.id,
            "scenario_hash": scenario_hash(scenario, registry),
            "prompt_template_version": PROMPT_TEMPLATE_VERSION,
            "prompt_hash": text_hash(prompt),
        }
    )
    if show_prompt:
        console.print(prompt)
