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


def _validation(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("validation_json")
    if not value:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _attempt_report(row: dict[str, Any]) -> dict[str, Any]:
    input_tokens, output_tokens, reasoning_tokens, cached_tokens = _token_counts(_usage(row))
    return {
        "attempt": int(row["attempt_no"]),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "http_status": row.get("http_status"),
        "error_type": row.get("error_type"),
        "error_message": row.get("error_message"),
        "latency_ms": float(row["latency_ms"]),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cached_tokens": cached_tokens,
    }


def _trial_report(row: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any]:
    input_tokens, output_tokens, reasoning_tokens, cached_tokens = _token_counts(_usage(row))
    has_result = row.get("result_job_id") is not None
    return {
        "trial": int(row["trial"]),
        "job_id": str(row["job_id"]),
        "status": str(row.get("status") or "pending"),
        "valid": bool(row.get("valid")) if has_result else False,
        "actual_attack": int(row.get("actual_attack") or 0),
        "oracle_attack": _oracle_attack(row),
        "ratio": float(row.get("ratio") or 0.0),
        "error_type": _error_name(row),
        "finish_reason": row.get("finish_reason"),
        "attempt_count": int(row.get("attempt_count") or 0),
        "attempts": attempts,
        "latency_ms": (float(row["latency_ms"]) if row.get("latency_ms") is not None else None),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cached_tokens": cached_tokens,
        "estimated_cost": (
            float(row["estimated_cost"]) if row.get("estimated_cost") is not None else None
        ),
        "validation": _validation(row),
    }


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
    attempts_by_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in storage.report_attempt_rows(run_id):
        attempts_by_job[str(attempt["job_id"])].append(_attempt_report(attempt))
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
                    "trial_results": [
                        _trial_report(row, attempts_by_job[str(row["job_id"])])
                        for row in scenario_rows
                    ],
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


def _html_text(value: Any) -> str:
    return html.escape(str(value))


def _html_percent(value: Any, digits: int = 1) -> str:
    return "—" if value is None else f"{float(value):.{digits}%}"


def _html_number(value: Any, digits: int = 1) -> str:
    return "—" if value is None else f"{float(value):,.{digits}f}"


def _html_cost(value: Any) -> str:
    return "—" if value is None else f"${float(value):,.6f}"


def _html_errors(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return '<span class="muted">—</span>'
    return "".join(
        f'<span class="error">{_html_text(name)} × {int(count)}</span>'
        for name, count in sorted(value.items())
    )


def _html_attempt_history(attempts: Any) -> str:
    if not isinstance(attempts, list) or not attempts:
        return '<span class="muted">尚未发起</span>'
    failures = sum(bool(attempt.get("error_type")) for attempt in attempts)
    rows = []
    for attempt in attempts:
        error_message = attempt.get("error_message")
        message_html = (
            f'<pre class="attempt-error">{html.escape(str(error_message))}</pre>'
            if error_message
            else '<span class="muted">—</span>'
        )
        rows.append(
            "<tr>"
            f"<td>{int(attempt.get('attempt') or 0)}</td>"
            f"<td>{_html_text(attempt.get('http_status') or '—')}</td>"
            f"<td>{_html_text(attempt.get('error_type') or 'success')}</td>"
            f"<td>{_html_number(attempt.get('latency_ms'))}</td>"
            f"<td>{message_html}</td>"
            "</tr>"
        )
    return (
        '<details class="attempt-history"><summary>'
        f"{len(attempts)} 次 / {failures} 次失败</summary>"
        '<table class="attempt-table"><thead><tr><th>#</th><th>HTTP</th>'
        "<th>类型</th><th>延迟 ms</th><th>原始错误（Key 已脱敏）</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></details>"
    )


def _html_trial_rows(trials: Any) -> str:
    if not isinstance(trials, list) or not trials:
        return '<tr><td colspan="12" class="muted">没有 trial 明细</td></tr>'
    rows = []
    for trial in trials:
        validation = trial.get("validation")
        validation_html = '<span class="muted">—</span>'
        if isinstance(validation, dict) and validation:
            payload = html.escape(json.dumps(validation, ensure_ascii=False, indent=2))
            validation_html = (
                f'<details class="validation"><summary>查看</summary><pre>{payload}</pre></details>'
            )
        valid = bool(trial.get("valid"))
        token_text = " / ".join(
            f"{int(trial.get(name) or 0):,}"
            for name in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens")
        )
        rows.append(
            "<tr>"
            f"<td>{int(trial.get('trial') or 0)}</td>"
            f'<td><span class="state {"ok" if valid else "bad"}">'
            f"{'合法' if valid else '失败'}</span></td>"
            f"<td>{int(trial.get('actual_attack') or 0)} / "
            f"{int(trial.get('oracle_attack') or 0)}</td>"
            f"<td>{_html_percent(trial.get('ratio'))}</td>"
            f"<td>{_html_text(trial.get('error_type') or '—')}</td>"
            f"<td>{_html_attempt_history(trial.get('attempts'))}</td>"
            f"<td>{_html_number(trial.get('latency_ms'))}</td>"
            f'<td class="tokens">{token_text}</td>'
            f"<td>{_html_text(trial.get('finish_reason') or '—')}</td>"
            f"<td>{_html_cost(trial.get('estimated_cost'))}</td>"
            f"<td><code>{_html_text(trial.get('job_id') or '—')}</code></td>"
            f"<td>{validation_html}</td>"
            "</tr>"
        )
    return "".join(rows)


def _html_scenarios(scenarios: Any) -> str:
    if not isinstance(scenarios, list) or not scenarios:
        return '<p class="muted">这个视图没有单题明细。</p>'
    blocks = []
    for scenario in scenarios:
        tags = "".join(
            f'<span class="tag">{_html_text(tag)}</span>' for tag in scenario.get("tags", [])
        )
        blocks.append(
            '<details class="scenario" open><summary>'
            f"<span><strong>{_html_text(scenario.get('title'))}</strong>"
            f"<small>{_html_text(scenario.get('scenario_id'))}</small></span>"
            f"<span>得分 {_html_percent(scenario.get('ratio_mean'))}</span>"
            f"<span>平均攻击 {_html_number(scenario.get('attack_mean'))} / "
            f"{int(scenario.get('oracle_attack') or 0)}</span>"
            f"<span>合法 {_html_percent(scenario.get('valid_rate'))}</span>"
            "</summary>"
            '<div class="scenario-stats">'
            f"<span>难度 <b>{_html_text(scenario.get('difficulty'))}</b></span>"
            f"<span>Trials <b>{int(scenario.get('trials') or 0)}</b></span>"
            f"<span>最好 / 最差 <b>{int(scenario.get('attack_best') or 0)} / "
            f"{int(scenario.get('attack_worst') or 0)}</b></span>"
            f"<span>标准差 <b>{_html_number(scenario.get('attack_stddev'))}</b></span>"
            f"<span>最优率 <b>{_html_percent(scenario.get('optimal_hit_rate'))}</b></span>"
            f"<span>权重 <b>{_html_number(scenario.get('weight'), 2)}</b></span>"
            f"<span>错误 {_html_errors(scenario.get('error_counts'))}</span>"
            "</div>"
            f'<div class="tags">{tags}</div>'
            '<div class="table-wrap"><table class="trial-table"><thead><tr>'
            "<th>Trial</th><th>结果</th><th>攻击 / Oracle</th><th>比例</th><th>错误</th>"
            "<th>尝试明细</th><th>延迟 ms</th><th>Token 入/出/推理/缓存</th><th>结束原因</th>"
            "<th>成本</th><th>Job ID</th><th>验证明细</th>"
            f"</tr></thead><tbody>{_html_trial_rows(scenario.get('trial_results'))}"
            "</tbody></table></div></details>"
        )
    return "".join(blocks)


def html_report(value: dict[str, Any]) -> str:
    profiles = value.get("profiles", value.get("entries", []))
    profiles = profiles if isinstance(profiles, list) else []
    title = _html_text(value.get("run_id", value.get("suite_id", "report")))
    metadata = [
        ("Run", value.get("run_id")),
        ("Plan", value.get("plan_id")),
        ("Suite", value.get("suite_id")),
        ("状态", value.get("status")),
        ("开始", value.get("started_at")),
        ("完成", value.get("completed_at")),
        ("分组", value.get("group_by")),
    ]
    metadata_html = "".join(
        f"<div><span>{_html_text(label)}</span><strong>{_html_text(item)}</strong></div>"
        for label, item in metadata
        if item is not None
    )
    overview_rows = []
    profile_sections = []
    for index, profile in enumerate(profiles, 1):
        display = profile.get("display_name") or profile.get("profile_id") or f"profile-{index}"
        overview_rows.append(
            "<tr>"
            f"<td>{profile.get('rank', index)}</td>"
            f'<td><a href="#profile-{index}">{_html_text(display)}</a>'
            f"<small>{_html_text(profile.get('profile_id', ''))}</small></td>"
            f"<td>{_html_text(profile.get('model', '—'))}</td>"
            f"<td>{_html_number(profile.get('overall_score'), 3)}</td>"
            f"<td>{_html_percent(profile.get('valid_rate'))}</td>"
            f"<td>{_html_percent(profile.get('optimal_hit_rate'))}</td>"
            f"<td>{_html_percent(profile.get('retry_rate'))}</td>"
            f"<td>{_html_percent(profile.get('truncation_rate'))}</td>"
            f"<td>{_html_number(profile.get('latency_p50_ms'))}</td>"
            f"<td>{_html_number(profile.get('latency_p95_ms'))}</td>"
            f"<td>{_html_cost(profile.get('estimated_cost'))}</td>"
            "</tr>"
        )
        total_tokens = int(profile.get("input_tokens") or 0) + int(
            profile.get("output_tokens") or 0
        )
        metrics = (
            ("总分", _html_number(profile.get("overall_score"), 3)),
            ("合法率", _html_percent(profile.get("valid_rate"))),
            ("最优命中率", _html_percent(profile.get("optimal_hit_rate"))),
            ("Jobs", f"{int(profile.get('jobs') or 0):,}"),
            ("平均攻击", _html_number(profile.get("attack_mean"))),
            (
                "最好 / 最差",
                f"{int(profile.get('attack_best') or 0)} / {int(profile.get('attack_worst') or 0)}",
            ),
            ("攻击标准差", _html_number(profile.get("attack_stddev"))),
            ("延迟 P50", f"{_html_number(profile.get('latency_p50_ms'))} ms"),
            ("延迟 P95", f"{_html_number(profile.get('latency_p95_ms'))} ms"),
            ("总 Token", f"{total_tokens:,}"),
            ("重试率", _html_percent(profile.get("retry_rate"))),
            ("截断率", _html_percent(profile.get("truncation_rate"))),
            ("估算成本", _html_cost(profile.get("estimated_cost"))),
        )
        metrics_html = "".join(
            f"<div><span>{_html_text(label)}</span><strong>{result}</strong></div>"
            for label, result in metrics
        )
        identity = " · ".join(
            _html_text(item)
            for item in (
                profile.get("profile_id"),
                profile.get("model"),
                profile.get("protocol"),
                f"thinking={profile.get('thinking_effort')}"
                if profile.get("thinking_effort") is not None
                else None,
            )
            if item is not None
        )
        token_line = (
            f"输入 {int(profile.get('input_tokens') or 0):,} · "
            f"输出 {int(profile.get('output_tokens') or 0):,} · "
            f"推理 {int(profile.get('reasoning_tokens') or 0):,} · "
            f"缓存 {int(profile.get('cached_tokens') or 0):,}"
        )
        profile_sections.append(
            f'<section class="profile" id="profile-{index}"><h2>{_html_text(display)}</h2>'
            f'<p class="identity">{identity}</p><div class="metrics">{metrics_html}</div>'
            f'<p class="token-line">Token：{token_line}</p>'
            f"<p><b>错误分布：</b>{_html_errors(profile.get('error_counts'))}</p>"
            f"<h3>单题与 Trial 明细</h3>{_html_scenarios(profile.get('scenarios'))}</section>"
        )

    style = """
:root { --bg:#f4f6fa; --card:#fff; --ink:#172033; --muted:#697386; --line:#dce2eb;
  --brand:#3157d5; --good:#177245; --bad:#b42318; --soft:#eef2ff }
* { box-sizing:border-box } html { scroll-behavior:smooth }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.55 system-ui,sans-serif }
header { padding:2rem max(1.5rem,calc((100vw - 1500px)/2)); color:white;
  background:linear-gradient(120deg,#17213a,#3157d5) }
header h1,header p { margin:.15rem 0 } main { max-width:1500px; margin:auto; padding:1.4rem }
a { color:var(--brand); text-decoration:none } a:hover { text-decoration:underline }
.metadata,.metrics,.scenario-stats { display:grid;
  grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:.7rem }
.metadata div,.metrics div { padding:.7rem .85rem; background:var(--card);
  border:1px solid var(--line); border-radius:9px }
.metadata span,.metrics span { display:block; color:var(--muted); font-size:.78rem }
.metadata strong { overflow-wrap:anywhere }.metrics strong { font-size:1.08rem }
.profile { margin:1.2rem 0; padding:1.1rem; background:var(--card);
  border:1px solid var(--line); border-radius:13px }
.profile h2 { margin:.1rem 0 }.identity,.token-line,.muted,small { color:var(--muted) }
.table-wrap { width:100%; overflow:auto; border:1px solid var(--line); border-radius:8px }
table { border-collapse:collapse; width:100%; background:white }
th,td { padding:.52rem .62rem; border-bottom:1px solid var(--line); text-align:right;
  white-space:nowrap; vertical-align:top }
th { background:#222d42; color:white; font-size:.78rem }
td:first-child,th:first-child { text-align:left }
td small,summary small { display:block }.overview { margin-top:1rem }
.scenario { margin:.7rem 0; border:1px solid var(--line); border-radius:9px; overflow:hidden }
.scenario>summary { cursor:pointer; padding:.75rem .9rem; display:flex; flex-wrap:wrap;
  justify-content:space-between; gap:.7rem; background:#f8faff }
.scenario-stats { padding:.7rem .9rem }.scenario-stats>span { color:var(--muted) }
.scenario-stats b { color:var(--ink) }.tags { padding:0 .8rem .6rem }
.tag,.error { display:inline-block; margin:.1rem; padding:.15rem .48rem;
  border-radius:999px; background:var(--soft) }
.error { color:var(--bad); background:#fff0ee }
.state { padding:.12rem .42rem; border-radius:999px; font-weight:600 }
.state.ok { color:var(--good); background:#eaf7ef }
.state.bad { color:var(--bad); background:#fff0ee }
.tokens { font-variant-numeric:tabular-nums }
.validation summary { cursor:pointer; color:var(--brand) }
.attempt-history>summary { cursor:pointer; color:var(--brand) }
.attempt-table { margin-top:.45rem; min-width:680px }
.attempt-table th,.attempt-table td { white-space:normal }
pre { max-width:620px; max-height:420px; overflow:auto; padding:.7rem; background:#111827;
  color:#dbe5f4; border-radius:7px; text-align:left; white-space:pre }
.attempt-error { min-width:360px; max-height:180px; margin:0 }
code { font-size:.78rem }
footer { max-width:1500px; margin:0 auto 2rem; padding:0 1.4rem; color:var(--muted) }
@media(max-width:700px) { main { padding:.7rem }.profile { padding:.7rem }
  .metadata,.metrics { grid-template-columns:1fr 1fr } }
"""
    overview = (
        '<section class="profile overview"><h2>模型总览</h2><div class="table-wrap"><table>'
        "<thead><tr><th>Rank</th><th>Profile</th><th>Model</th><th>总分</th><th>合法率</th>"
        "<th>最优率</th><th>重试率</th><th>截断率</th><th>P50 ms</th><th>P95 ms</th>"
        f"<th>成本</th></tr></thead><tbody>{''.join(overview_rows)}</tbody></table></div></section>"
        if overview_rows
        else '<section class="profile muted">没有可展示的模型结果。</section>'
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title} · Backpack Battle Bench</title><style>{style}</style></head><body>"
        f"<header><h1>Backpack Battle Bench</h1><p>{title}</p></header><main>"
        f'<section class="metadata">{metadata_html}</section>{overview}{"".join(profile_sections)}'
        "</main><footer>静态报告由 bbbench 生成；验证明细来自 SQLite 中的脱敏结果。</footer>"
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
