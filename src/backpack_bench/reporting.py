"""Run reports, weighted scores and static leaderboard formats."""

from __future__ import annotations

import csv
import html
import io
import json
import statistics
from collections import Counter, defaultdict
from typing import Any, cast

from rich.console import Console
from rich.table import Table

from backpack_bench.storage import Storage


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _usage(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("usage_json")
    if not value:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _token_counts(usage: dict[str, Any]) -> tuple[int, int, int, int]:
    input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
    details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    reasoning = int(details.get("reasoning_tokens", details.get("thinking_tokens", 0)) or 0)
    prompt_details = usage.get("prompt_tokens_details") or {}
    cached = int(prompt_details.get("cached_tokens", usage.get("cache_read_input_tokens", 0)) or 0)
    return input_tokens, output_tokens, reasoning, cached


def _oracle_attack(row: dict[str, Any]) -> int:
    return int(row.get("oracle_attack") or row.get("exact_oracle_attack") or 0)


def _error_name(row: dict[str, Any]) -> str:
    if row.get("result_job_id") is None:
        return f"job_{row.get('status', 'pending')}"
    return str(row.get("error_type") or "success")


def _aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute metrics while giving each scenario its manifest weight exactly once."""
    if not rows:
        raise ValueError("cannot aggregate an empty row group")
    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_scenario[str(row["scenario_hash"])].append(row)
    weighted_ratio = 0.0
    total_weight = 0.0
    for scenario_rows in by_scenario.values():
        weight = float(scenario_rows[0]["weight"])
        ratio_mean = statistics.fmean(float(row.get("ratio") or 0.0) for row in scenario_rows)
        weighted_ratio += weight * ratio_mean
        total_weight += weight

    attacks = [int(row.get("actual_attack") or 0) for row in rows]
    latencies = [float(row["latency_ms"]) for row in rows if row.get("latency_ms") is not None]
    tokens = [0, 0, 0, 0]
    for row in rows:
        counts = _token_counts(_usage(row))
        tokens = [current + added for current, added in zip(tokens, counts, strict=True)]
    costs = [float(row["estimated_cost"]) for row in rows if row.get("estimated_cost") is not None]
    return {
        "scenarios": len(by_scenario),
        "jobs": len(rows),
        "overall_score": 100 * weighted_ratio / total_weight if total_weight else 0.0,
        "valid_rate": sum(bool(row.get("valid")) for row in rows) / len(rows),
        "optimal_hit_rate": sum(
            bool(row.get("valid")) and int(row.get("actual_attack") or 0) == _oracle_attack(row)
            for row in rows
        )
        / len(rows),
        "attack_mean": statistics.fmean(attacks),
        "attack_best": max(attacks),
        "attack_worst": min(attacks),
        "attack_stddev": statistics.pstdev(attacks),
        "latency_p50_ms": _percentile(latencies, 0.5),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "input_tokens": tokens[0],
        "output_tokens": tokens[1],
        "reasoning_tokens": tokens[2],
        "cached_tokens": tokens[3],
        "estimated_cost": sum(costs) if costs else None,
        "retry_rate": sum(int(row.get("attempt_count") or 0) > 1 for row in rows) / len(rows),
        "truncation_rate": sum(_error_name(row) == "output_truncated" for row in rows) / len(rows),
        "error_counts": dict(sorted(Counter(_error_name(row) for row in rows).items())),
    }


def _profile_config(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("config_json")
    if isinstance(value, str):
        value = json.loads(value)
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _thinking_effort(row: dict[str, Any]) -> str:
    params = _profile_config(row).get("params")
    if isinstance(params, dict):
        effort = params.get("thinking_effort")
        if effort is not None:
            return str(effort)
    return "default"


def _named_groups(values: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [
        {"group_value": name, **_aggregate_rows(group)} for name, group in sorted(values.items())
    ]


def build_run_report(storage: Storage, run_id: str) -> dict[str, Any]:
    run = storage.get_run(run_id)
    if run is None:
        raise ValueError(f"run {run_id} was not found")
    rows = storage.report_rows(run_id)
    by_profile: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_profile[str(row["profile_hash"])].append(row)
    profiles: list[dict[str, Any]] = []
    for current_profile_hash, profile_rows in by_profile.items():
        by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in profile_rows:
            by_scenario[str(row["scenario_hash"])].append(row)
        scenario_reports: list[dict[str, Any]] = []
        for scenario_rows in by_scenario.values():
            ratios = [float(row["ratio"] or 0.0) for row in scenario_rows]
            attacks = [int(row["actual_attack"] or 0) for row in scenario_rows]
            valid_count = sum(bool(row["valid"]) for row in scenario_rows)
            oracle = _oracle_attack(scenario_rows[0])
            weight = float(scenario_rows[0]["weight"])
            ratio_mean = statistics.fmean(ratios)
            scenario_reports.append(
                {
                    "scenario_id": scenario_rows[0]["scenario_id"],
                    "title": scenario_rows[0]["title"],
                    "difficulty": scenario_rows[0]["difficulty"],
                    "tags": json.loads(scenario_rows[0]["tags_json"]),
                    "weight": weight,
                    "oracle_attack": oracle,
                    "trials": len(scenario_rows),
                    "valid_rate": valid_count / len(scenario_rows),
                    "attack_mean": statistics.fmean(attacks),
                    "attack_best": max(attacks),
                    "attack_worst": min(attacks),
                    "attack_stddev": statistics.pstdev(attacks),
                    "ratio_mean": ratio_mean,
                    "optimal_hit_rate": sum(attack == oracle for attack in attacks) / len(attacks),
                    "error_counts": dict(
                        sorted(Counter(_error_name(row) for row in scenario_rows).items())
                    ),
                }
            )
        aggregate = _aggregate_rows(profile_rows)
        difficulty_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        tag_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in profile_rows:
            difficulty_rows[str(row["difficulty"])].append(row)
            for tag in json.loads(row["tags_json"]):
                tag_rows[str(tag)].append(row)
        profiles.append(
            {
                **aggregate,
                "profile_hash": current_profile_hash,
                "profile_id": profile_rows[0]["profile_id"],
                "display_name": profile_rows[0]["display_name"],
                "protocol": profile_rows[0]["protocol"],
                "model": profile_rows[0]["model"],
                "thinking_effort": _thinking_effort(profile_rows[0]),
                "groups": {
                    "difficulty": _named_groups(difficulty_rows),
                    "tag": _named_groups(tag_rows),
                },
                "scenarios": sorted(scenario_reports, key=lambda item: item["scenario_id"]),
            }
        )
    profiles.sort(
        key=lambda item: (-item["overall_score"], -item["valid_rate"], item["profile_id"])
    )
    model_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    thinking_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        model_rows[str(row["model"])].append(row)
        thinking_rows[_thinking_effort(row)].append(row)
    return {
        "run_id": run_id,
        "plan_id": run["plan_id"],
        "suite_id": run["suite_id"],
        "suite_hash": run["suite_hash"],
        "status": run["status"],
        "started_at": run["started_at"],
        "completed_at": run["completed_at"],
        "profiles": profiles,
        "groups": {
            "model": _named_groups(model_rows),
            "thinking_effort": _named_groups(thinking_rows),
        },
    }


def build_leaderboard(storage: Storage, suite_id: str) -> dict[str, Any]:
    runs = storage.latest_completed_runs(suite_id)
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    suite_hash: str | None = runs[0]["suite_hash"] if runs else None
    for run in runs:
        if suite_hash is not None and run["suite_hash"] != suite_hash:
            continue
        report = build_run_report(storage, str(run["run_id"]))
        for profile in report["profiles"]:
            key = (str(profile["profile_hash"]), str(run["suite_hash"]))
            if key not in selected:
                selected[key] = {**profile, "run_id": run["run_id"]}
    entries = sorted(
        selected.values(),
        key=lambda item: (-item["overall_score"], -item["valid_rate"], item["profile_id"]),
    )
    for rank, entry in enumerate(entries, 1):
        entry["rank"] = rank
    return {"suite_id": suite_id, "suite_hash": suite_hash, "entries": entries}


def group_report_view(value: dict[str, Any], group_by: str | None) -> dict[str, Any]:
    """Flatten a supported grouping for console/CSV/HTML and focused JSON output."""
    if group_by is None:
        return value
    if group_by not in {"difficulty", "tag", "model", "thinking_effort"}:
        raise ValueError("group-by must be difficulty, tag, model or thinking_effort")
    entries: list[dict[str, Any]] = []
    if group_by in {"difficulty", "tag"}:
        for profile in value.get("profiles", []):
            for group in profile.get("groups", {}).get(group_by, []):
                entries.append(
                    {
                        **group,
                        "profile_id": profile["profile_id"],
                        "profile_hash": profile["profile_hash"],
                        "model": f"{profile['model']} [{group['group_value']}]",
                    }
                )
    else:
        for group in value.get("groups", {}).get(group_by, []):
            label = str(group["group_value"])
            entries.append(
                {
                    **group,
                    "profile_id": label,
                    "model": label if group_by == "model" else "all models",
                }
            )
    entries.sort(key=lambda item: (-item["overall_score"], str(item["profile_id"])))
    return {
        "run_id": value.get("run_id"),
        "suite_id": value.get("suite_id"),
        "suite_hash": value.get("suite_hash"),
        "group_by": group_by,
        "entries": entries,
    }


def console_report(value: dict[str, Any], console: Console | None = None) -> None:
    console = console or Console()
    profiles = value.get("profiles", value.get("entries", []))
    table = Table(title=f"Backpack Battle Bench — {value.get('run_id', value.get('suite_id'))}")
    for column in (
        "Rank",
        "Profile",
        "Model",
        "Score",
        "Valid",
        "Optimal",
        "Retry",
        "Trunc",
        "P50 ms",
        "Tokens",
    ):
        table.add_column(column, justify="right" if column not in {"Profile", "Model"} else "left")
    for index, profile in enumerate(profiles, 1):
        table.add_row(
            str(profile.get("rank", index)),
            str(profile["profile_id"]),
            str(profile["model"]),
            f"{profile['overall_score']:.3f}",
            f"{profile['valid_rate']:.1%}",
            f"{profile['optimal_hit_rate']:.1%}",
            f"{profile['retry_rate']:.1%}",
            f"{profile['truncation_rate']:.1%}",
            f"{profile['latency_p50_ms']:.1f}" if profile["latency_p50_ms"] is not None else "—",
            str(profile["input_tokens"] + profile["output_tokens"]),
        )
    console.print(table)


def csv_report(value: dict[str, Any]) -> str:
    profiles = value.get("profiles", value.get("entries", []))
    output = io.StringIO()
    fields = [
        "rank",
        "profile_id",
        "model",
        "group_value",
        "overall_score",
        "valid_rate",
        "optimal_hit_rate",
        "retry_rate",
        "truncation_rate",
        "attack_mean",
        "attack_best",
        "attack_worst",
        "attack_stddev",
        "latency_p50_ms",
        "latency_p95_ms",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "estimated_cost",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for index, profile in enumerate(profiles, 1):
        writer.writerow({"rank": profile.get("rank", index), **profile})
    return output.getvalue()


def html_report(value: dict[str, Any]) -> str:
    profiles = value.get("profiles", value.get("entries", []))
    rows = []
    for index, profile in enumerate(profiles, 1):
        latency = (
            f"{profile['latency_p50_ms']:.1f}" if profile["latency_p50_ms"] is not None else "—"
        )
        rows.append(
            "<tr>"
            f"<td>{profile.get('rank', index)}</td>"
            f"<td>{html.escape(str(profile['profile_id']))}</td>"
            f"<td>{html.escape(str(profile['model']))}</td>"
            f"<td>{profile['overall_score']:.3f}</td>"
            f"<td>{profile['valid_rate']:.1%}</td>"
            f"<td>{profile['optimal_hit_rate']:.1%}</td>"
            f"<td>{profile['retry_rate']:.1%}</td>"
            f"<td>{profile['truncation_rate']:.1%}</td>"
            f"<td>{latency}</td>"
            "</tr>"
        )
    title = html.escape(str(value.get("run_id", value.get("suite_id", "report"))))
    style = """
body { font-family: system-ui; margin: 2rem; background: #f7f7f8; color: #222 }
table { border-collapse: collapse; width: 100%; background: white }
th, td { border: 1px solid #ddd; padding: .55rem; text-align: right }
th:nth-child(2), td:nth-child(2), th:nth-child(3), td:nth-child(3) { text-align: left }
th { background: #222; color: white }
"""
    header = (
        "<tr><th>Rank</th><th>Profile</th><th>Model</th><th>Score</th>"
        "<th>Valid</th><th>Optimal</th><th>Retry</th><th>Trunc</th><th>P50 ms</th></tr>"
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"<title>{title}</title><style>{style}</style></head><body><h1>{title}</h1>"
        f"<table><thead>{header}</thead><tbody>{''.join(rows)}</tbody></table>"
        "</body></html>"
    )


def serialize_report(value: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if output_format == "csv":
        return csv_report(value)
    if output_format == "html":
        return html_report(value)
    raise ValueError(f"unsupported serialized report format: {output_format}")
