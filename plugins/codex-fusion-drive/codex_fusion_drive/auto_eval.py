"""Deterministic, dependency-free HTML/SVG workflow evaluation."""

from __future__ import annotations

import html
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import load_config, runtime_dir
from .report import gate_inventory
from .util import (
    atomic_write_text,
    canonical_hash,
    canonical_json,
    json_copy,
    read_json,
    validate_identifier,
)


VERDICT_SCORES = {"PASS": 100.0, "NEEDS_WORK": 50.0, "FAIL": 0.0, "NOT_RUN": 0.0}


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _grade(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def _normalized_gate_rows(
    evidence: Mapping[str, Any],
    config: Mapping[str, Any],
    profile_name: str,
) -> list[dict[str, Any]]:
    supplied = evidence.get("gates", {})
    supplied = supplied if isinstance(supplied, Mapping) else {}
    rows = []
    for gate in gate_inventory(config, profile_name=profile_name):
        raw = supplied.get(gate["stage"], {})
        raw = raw if isinstance(raw, Mapping) else {}
        verdict = str(raw.get("verdict", "NOT_RUN")).upper()
        if verdict not in VERDICT_SCORES:
            verdict = "FAIL"
        score = _number(raw.get("score"), VERDICT_SCORES[verdict])
        score = min(100.0, max(0.0, score))
        rows.append(
            {
                "stage": gate["stage"],
                "owner": gate["owner"],
                "verdict": verdict,
                "score": round(score, 2),
                "grade": _grade(score),
                "evidence": sorted(set(str(item) for item in raw.get("evidence", []))),
                "reviewers": list(raw.get("reviewers", gate["reviewers"])),
            }
        )
    return rows


def _model_rows(
    evidence: Mapping[str, Any],
    config: Mapping[str, Any],
    profile_name: str,
) -> list[dict[str, Any]]:
    supplied = evidence.get("models", [])
    supplied = supplied if isinstance(supplied, list) else []
    by_seat = {
        str(item.get("seat")): item
        for item in supplied
        if isinstance(item, Mapping) and item.get("seat")
    }
    selected_profile = config["profiles"][profile_name]
    engine = config["engines"][selected_profile["engine"]]
    seat_names = list(engine.get("panel", []))
    if engine.get("judge"):
        seat_names.append(engine["judge"])
    if engine.get("fuser"):
        seat_names.append(engine["fuser"])
    seat_names.extend(config["gate_sets"][selected_profile["gate_set"]]["reviewers"])
    seat_names.extend(by_seat)
    rows = []
    for seat_name in sorted(set(seat_names)):
        seat = config["seats"][seat_name]
        raw = by_seat.get(seat_name, {})
        billed_cost = raw.get("billed_cost_usd")
        if not isinstance(billed_cost, (int, float)):
            billed_cost = None
        rows.append(
            {
                "seat": seat_name,
                "model": str(raw.get("model", seat["model"])),
                "provider": seat["provider"],
                "role": seat["role"],
                "requested_reasoning": seat["reasoning"],
                "effective_reasoning": seat["effective_reasoning"],
                "input_tokens": _integer(raw.get("input_tokens")),
                "output_tokens": _integer(raw.get("output_tokens")),
                "reasoning_tokens": _integer(raw.get("reasoning_tokens")),
                "latency_seconds": round(_number(raw.get("latency_seconds")), 3),
                "billed_cost_usd": round(float(billed_cost), 6) if billed_cost is not None else None,
                "subscription_usage_units": raw.get("subscription_usage_units"),
                "honesty_observations": sorted(set(str(item) for item in raw.get("honesty_observations", []))),
            }
        )
    return rows


def _contribution_rows(evidence: Mapping[str, Any], models: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ablations = evidence.get("ablations", [])
    ablations = ablations if isinstance(ablations, list) else []
    measured: dict[str, dict[str, Any]] = {}
    for item in ablations:
        if not isinstance(item, Mapping):
            continue
        model = str(item.get("model", ""))
        pinned = bool(item.get("pinned"))
        hashes_match = bool(item.get("same_task_hash")) and bool(item.get("same_config_except_model"))
        baseline = item.get("baseline_score")
        without = item.get("without_model_score")
        if (
            model
            and pinned
            and hashes_match
            and isinstance(baseline, (int, float))
            and isinstance(without, (int, float))
        ):
            measured[model] = {
                "status": "measured",
                "delta_score": round(float(baseline) - float(without), 3),
                "baseline_score": round(float(baseline), 3),
                "without_model_score": round(float(without), 3),
                "method": "pinned_single_model_ablation",
            }
    rows = []
    for model in sorted({str(item["model"]) for item in models}):
        row = {"model": model}
        row.update(
            measured.get(
                model,
                {
                    "status": "unknown",
                    "delta_score": None,
                    "baseline_score": None,
                    "without_model_score": None,
                    "method": "not_scored_without_pinned_ablation",
                },
            )
        )
        rows.append(row)
    return rows


def evaluate(evidence: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    config = dict(config or load_config())
    profile_name = str(evidence.get("profile") or config["active_profile"])
    if profile_name not in config["profiles"]:
        raise ValueError(f"Unknown evaluation profile: {profile_name}")
    selected_profile = config["profiles"][profile_name]
    gates = _normalized_gate_rows(evidence, config, profile_name)
    models = _model_rows(evidence, config, profile_name)
    tool_calls = evidence.get("tool_calls", [])
    tool_calls = [dict(item) for item in tool_calls if isinstance(item, Mapping)] if isinstance(tool_calls, list) else []
    claims = evidence.get("claims", [])
    claims = [dict(item) for item in claims if isinstance(item, Mapping)] if isinstance(claims, list) else []
    failures = evidence.get("failures", [])
    failures = [dict(item) for item in failures if isinstance(item, Mapping)] if isinstance(failures, list) else []
    config_changes = evidence.get("config_changes", [])
    config_changes = [dict(item) for item in config_changes if isinstance(item, Mapping)] if isinstance(config_changes, list) else []

    tool_errors = [
        {
            "tool": str(item.get("tool", "unknown")),
            "error": str(item.get("error", "")),
            "category": str(item.get("category", "tool_error")),
            "recoverable": bool(item.get("recoverable", False)),
        }
        for item in tool_calls
        if item.get("error") or str(item.get("status", "")).lower() in {"error", "failed"}
    ]
    unsupported_claims = [
        {
            "claim": str(item.get("claim", "")),
            "model": str(item.get("model", "unknown")),
            "evidence": str(item.get("evidence", "")),
        }
        for item in claims
        if item.get("verified") is False
    ]
    verified_claims = sum(1 for item in claims if item.get("verified") is True)
    unknown_claims = sum(1 for item in claims if item.get("verified") is None)
    gate_average = sum(item["score"] for item in gates) / len(gates) if gates else 0.0
    known_cost = round(
        sum(float(item["billed_cost_usd"]) for item in models if item["billed_cost_usd"] is not None),
        6,
    )
    unknown_cost_models = sorted(item["seat"] for item in models if item["billed_cost_usd"] is None)
    total_tokens = sum(
        item["input_tokens"] + item["output_tokens"] + item["reasoning_tokens"] for item in models
    )
    reasoning_tokens = sum(item["reasoning_tokens"] for item in models)
    reasoning_share = reasoning_tokens / total_tokens if total_tokens else 0.0
    failed_gates = sum(1 for item in gates if item["verdict"] in {"FAIL", "NOT_RUN"})
    over_reasoning = reasoning_share > 0.7 and failed_gates == 0 and not unsupported_claims
    under_reasoning = reasoning_share < 0.05 and (failed_gates > 0 or bool(unsupported_claims))
    honesty_observations = [
        observation
        for item in models
        for observation in item["honesty_observations"]
    ]
    honesty_score = 100.0
    honesty_score -= 20.0 * len(unsupported_claims)
    honesty_score -= 5.0 * sum(1 for item in claims if item.get("confidence_without_evidence"))
    honesty_score += min(10.0, 2.0 * sum(1 for item in claims if item.get("uncertainty_disclosed")))
    honesty_score = min(100.0, max(0.0, honesty_score))
    contribution = _contribution_rows(evidence, models)
    measured_contributions = [item for item in contribution if item["status"] == "measured"]
    efficiency = {
        "gate_average": round(gate_average, 2),
        "grade": _grade(gate_average),
        "known_billed_cost_usd": known_cost,
        "unknown_cost_models": unknown_cost_models,
        "total_tokens": total_tokens,
        "reasoning_tokens": reasoning_tokens,
        "reasoning_share": round(reasoning_share, 4),
        "latency_seconds_sum": round(sum(item["latency_seconds"] for item in models), 3),
        "pass_score_per_known_dollar": (
            round(gate_average / known_cost, 3) if known_cost > 0 and not unknown_cost_models else None
        ),
        "interpretation": (
            "Efficiency per dollar is unknown while any participating model has unknown billed cost."
            if unknown_cost_models
            else "Efficiency uses gate score divided by known billed API cost."
        ),
    }
    evaluation = {
        "schema_version": 1,
        "run_id": str(evidence.get("run_id", "unassigned")),
        "report_timestamp": evidence.get("report_timestamp"),
        "profile": profile_name,
        "engine": str(evidence.get("engine", selected_profile["engine"])),
        "input_evidence_sha256": canonical_hash(evidence),
        "config_sha256": canonical_hash(config),
        "gates": gates,
        "models": models,
        "spend": {
            "known_billed_cost_usd": known_cost,
            "unknown_cost_models": unknown_cost_models,
            "subscription_usage": [
                {
                    "seat": item["seat"],
                    "provider": item["provider"],
                    "usage_units": item["subscription_usage_units"],
                    "billed_cost_usd": item["billed_cost_usd"],
                }
                for item in models
                if config["providers"][item["provider"]]["billing"] == "subscription"
            ],
            "warning": "Unknown subscription usage or cost is never rendered as zero.",
        },
        "efficiency": efficiency,
        "config_changes": sorted(config_changes, key=lambda item: canonical_json(item)),
        "failures": sorted(failures, key=lambda item: canonical_json(item)),
        "tool_errors": sorted(tool_errors, key=lambda item: canonical_json(item)),
        "claims": {
            "verified": verified_claims,
            "unknown": unknown_claims,
            "unsupported": sorted(unsupported_claims, key=lambda item: canonical_json(item)),
        },
        "honesty": {
            "score": round(honesty_score, 2),
            "grade": _grade(honesty_score),
            "observations": sorted(set(honesty_observations)),
            "basis": "Only supplied claim verification and explicit observations are graded.",
        },
        "reasoning": {
            "over_reasoning_flag": over_reasoning,
            "under_reasoning_flag": under_reasoning,
            "reasoning_share": round(reasoning_share, 4),
            "policy": "Heuristic signal only; unbounded reasoning does not remove evidence requirements.",
        },
        "intelligence_contribution": contribution,
        "measured_contribution_count": len(measured_contributions),
        "settings": json_copy(
            {
                "configured_active_profile": config["active_profile"],
                "selected_profile": profile_name,
                "engines": config["engines"],
                "gate_sets": config["gate_sets"],
                "batching": config["batching"],
                "rescue": config["rescue"],
                "human_sim_users": config["human_sim_users"],
                "auto_eval": config["auto_eval"],
            }
        ),
    }
    evaluation["evaluation_sha256"] = canonical_hash(evaluation)
    return evaluation


def _workflow_svg(evaluation: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    profile_name = str(evaluation.get("profile") or config["active_profile"])
    active = config["profiles"][profile_name]
    engine_name = str(evaluation["engine"])
    engine = config["engines"].get(engine_name, config["engines"][active["engine"]])
    panel = list(engine.get("panel", []))
    nodes: list[tuple[str, int, int, int, int, str]] = [
        ("Task", 30, 42, 110, 46, "task"),
        (engine_name.replace("_", " "), 185, 42, 175, 46, "engine"),
    ]
    for index, seat_name in enumerate(panel):
        nodes.append((seat_name, 410, 10 + index * 62, 190, 44, "model"))
    judge_name = str(engine.get("judge", "server judge"))
    fuser_name = str(engine.get("fuser", engine.get("seat", "server fuser")))
    middle_y = 10 + max(0, len(panel) - 1) * 31
    nodes.extend(
        [
            (judge_name, 645, middle_y, 180, 44, "judge"),
            (fuser_name, 870, middle_y, 180, 44, "fuser"),
            ("approval gates", 1095, middle_y, 170, 44, "gate"),
            ("Claude session + execute", 1310, middle_y, 190, 44, "execute"),
        ]
    )
    edges = [
        (140, 65, 185, 65),
    ]
    for index in range(len(panel)):
        y = 32 + index * 62
        edges.extend([(360, 65, 410, y), (600, y, 645, middle_y + 22)])
    if not panel:
        edges.append((360, 65, 645, middle_y + 22))
    edges.extend(
        [
            (825, middle_y + 22, 870, middle_y + 22),
            (1050, middle_y + 22, 1095, middle_y + 22),
            (1265, middle_y + 22, 1310, middle_y + 22),
        ]
    )
    height = max(150, 34 + max(1, len(panel)) * 62)
    palette = {
        "task": "#253238",
        "engine": "#D6532B",
        "model": "#F1B24A",
        "judge": "#277C78",
        "fuser": "#135E6A",
        "gate": "#A13D2D",
        "execute": "#253238",
    }
    svg = [
        f'<svg class="workflow-svg" viewBox="0 0 1530 {height}" role="img" aria-label="Fusion workflow graph">',
        '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#8A8177"/></marker></defs>',
    ]
    for x1, y1, x2, y2 in edges:
        svg.append(f'<path d="M{x1} {y1} L{x2} {y2}" stroke="#8A8177" stroke-width="2" fill="none" marker-end="url(#arrow)"/>')
    for label, x, y, width, height_node, kind in nodes:
        svg.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height_node}" rx="8" fill="{palette[kind]}"/>')
        svg.append(
            f'<text x="{x + width / 2:.1f}" y="{y + height_node / 2 + 4:.1f}" text-anchor="middle" '
            f'fill="#FFF9EE" font-size="13">{_escape(label)}</text>'
        )
    svg.append("</svg>")
    return "".join(svg)


def _bar_svg(
    rows: Sequence[Mapping[str, Any]],
    *,
    value_key: str,
    label_key: str,
    unknown_label: str,
) -> str:
    width = 720
    row_height = 34
    height = max(64, 24 + row_height * len(rows))
    known_values = [_number(item.get(value_key)) for item in rows if item.get(value_key) is not None]
    maximum = max(known_values, default=1.0)
    svg = [f'<svg class="bar-svg" viewBox="0 0 {width} {height}" role="img">']
    for index, item in enumerate(rows):
        y = 16 + index * row_height
        label = str(item.get(label_key, "unknown"))
        raw = item.get(value_key)
        if raw is None:
            bar_width = 0
            display = unknown_label
        else:
            value = _number(raw)
            bar_width = 410 * value / maximum if maximum else 0
            display = f"{value:.3f}"
        svg.append(f'<text x="0" y="{y + 15}" fill="#253238" font-size="12">{_escape(label)}</text>')
        svg.append(f'<rect x="230" y="{y}" width="420" height="20" rx="4" fill="#EFE5D3"/>')
        svg.append(f'<rect x="230" y="{y}" width="{bar_width:.2f}" height="20" rx="4" fill="#D6532B"/>')
        svg.append(f'<text x="660" y="{y + 15}" fill="#5D554D" font-size="11">{_escape(display)}</text>')
    svg.append("</svg>")
    return "".join(svg)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    head = "".join(f"<th>{_escape(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_escape(item)}</td>" for item in row) + "</tr>"
        for row in rows
    )
    return f"<div class=\"table-wrap\"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def render_html(evaluation: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    gates = evaluation["gates"]
    models = evaluation["models"]
    contributions = evaluation["intelligence_contribution"]
    gate_table = _table(
        ["Gate", "Owner", "Verdict", "Score", "Grade", "Evidence"],
        [
            [
                item["stage"],
                item["owner"],
                item["verdict"],
                f'{item["score"]:.2f}',
                item["grade"],
                ", ".join(item["evidence"]) or "not supplied",
            ]
            for item in gates
        ],
    )
    model_table = _table(
        ["Seat", "Model", "Role", "Reasoning", "Tokens", "Latency", "Billed USD"],
        [
            [
                item["seat"],
                item["model"],
                item["role"],
                f'{item["requested_reasoning"]} -> {item["effective_reasoning"]}',
                item["input_tokens"] + item["output_tokens"] + item["reasoning_tokens"],
                f'{item["latency_seconds"]:.3f}s',
                "unknown" if item["billed_cost_usd"] is None else f'${item["billed_cost_usd"]:.6f}',
            ]
            for item in models
        ],
    )
    contribution_table = _table(
        ["Model", "Status", "Delta", "Method"],
        [
            [
                item["model"],
                item["status"],
                "unknown" if item["delta_score"] is None else f'{item["delta_score"]:+.3f}',
                item["method"],
            ]
            for item in contributions
        ],
    )
    cost_svg = _bar_svg(models, value_key="billed_cost_usd", label_key="seat", unknown_label="unknown")
    gate_svg = _bar_svg(gates, value_key="score", label_key="stage", unknown_label="not run")
    workflow_svg = _workflow_svg(evaluation, config)
    failures = evaluation["failures"]
    tool_errors = evaluation["tool_errors"]
    unsupported = evaluation["claims"]["unsupported"]
    config_changes = evaluation["config_changes"]

    def cards(items: Sequence[Mapping[str, Any]], empty: str) -> str:
        if not items:
            return f'<p class="empty">{_escape(empty)}</p>'
        return "".join(
            f'<article class="issue"><code>{_escape(item.get("category") or item.get("severity") or "record")}</code>'
            f'<p>{_escape(item.get("error") or item.get("message") or item.get("claim") or canonical_json(item))}</p></article>'
            for item in items
        )

    timestamp = evaluation.get("report_timestamp") or "not supplied"
    settings_json = json.dumps(evaluation["settings"], indent=2, sort_keys=True, ensure_ascii=False)
    css = """
    :root{--ink:#253238;--muted:#6B6259;--paper:#FFF9EE;--panel:#FFFDF7;--line:#D8CCB9;--ember:#D6532B;--gold:#F1B24A;--teal:#277C78;--deep:#135E6A;--danger:#A13D2D}
    *{box-sizing:border-box}body{margin:0;color:var(--ink);background:radial-gradient(circle at 12% 8%,#F7D9A8 0,transparent 28%),linear-gradient(135deg,#FFF9EE,#F4EBDD);font-family:"Iowan Old Style","Palatino Linotype",Palatino,serif}
    main{max-width:1220px;margin:0 auto;padding:48px 28px 80px}header{border-top:8px solid var(--ember);padding:28px 0 20px;display:grid;grid-template-columns:1.4fr .6fr;gap:28px}
    h1,h2,h3{font-family:"Avenir Next Condensed","Franklin Gothic Condensed","Trebuchet MS",sans-serif;letter-spacing:.01em;margin:0}h1{font-size:clamp(42px,7vw,86px);line-height:.88;text-transform:uppercase}h2{font-size:28px;margin-bottom:18px}h3{font-size:18px}
    .kicker,.mono,code,th{font-family:"SFMono-Regular","Cascadia Mono","Liberation Mono",monospace}.kicker{color:var(--ember);font-weight:800;text-transform:uppercase;letter-spacing:.14em}.summary{align-self:end;border-left:2px solid var(--ink);padding-left:20px}
    .grid{display:grid;grid-template-columns:repeat(12,1fr);gap:18px}.card{grid-column:span 4;background:rgba(255,253,247,.92);border:1px solid var(--line);box-shadow:5px 5px 0 rgba(37,50,56,.12);padding:22px;border-radius:4px}.wide{grid-column:1/-1}.half{grid-column:span 6}
    .metric{font-family:"Avenir Next Condensed","Franklin Gothic Condensed",sans-serif;font-size:38px;font-weight:800;color:var(--deep)}.label{color:var(--muted);font-size:13px;text-transform:uppercase;letter-spacing:.08em}
    .workflow-svg,.bar-svg{width:100%;height:auto;display:block}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}th,td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top}
    .issue{border-left:4px solid var(--danger);background:#FFF1E9;padding:12px 14px;margin:10px 0}.issue p{margin:6px 0 0}.empty{color:var(--teal);font-style:italic}pre{white-space:pre-wrap;word-break:break-word;background:#253238;color:#FFF9EE;padding:18px;border-radius:4px;max-height:560px;overflow:auto;font-size:11px}
    .stamp{display:inline-block;border:2px solid var(--teal);color:var(--teal);padding:5px 8px;font-family:"SFMono-Regular",monospace;font-size:12px;transform:rotate(-1deg)}footer{margin-top:28px;border-top:1px solid var(--line);padding-top:18px;color:var(--muted);font-size:12px}
    @media(max-width:820px){header{grid-template-columns:1fr}.card,.half{grid-column:1/-1}main{padding:28px 16px 60px}h1{font-size:48px}}
    """
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Codex Fusion Drive Auto-Eval</title><style>{css}</style></head>
<body><main>
<header><div><p class="kicker">Deterministic workflow tearsheet</p><h1>Fusion<br>Drive</h1></div><div class="summary"><span class="stamp">{_escape(evaluation["evaluation_sha256"][:16])}</span><p>Run <span class="mono">{_escape(evaluation["run_id"])}</span></p><p>Timestamp: {_escape(timestamp)}</p><p>Engine: {_escape(evaluation["engine"])}</p></div></header>
<section class="grid">
<article class="card"><p class="label">Gate grade</p><p class="metric">{_escape(evaluation["efficiency"]["grade"])}</p><p>{evaluation["efficiency"]["gate_average"]:.2f}/100 average</p></article>
<article class="card"><p class="label">Known API spend</p><p class="metric">${evaluation["spend"]["known_billed_cost_usd"]:.4f}</p><p>{len(evaluation["spend"]["unknown_cost_models"])} seats remain unknown</p></article>
<article class="card"><p class="label">Honesty grade</p><p class="metric">{_escape(evaluation["honesty"]["grade"])}</p><p>{evaluation["honesty"]["score"]:.2f}/100 evidence-based</p></article>
<article class="card wide"><h2>Proposed workflow</h2>{workflow_svg}</article>
<article class="card half"><h2>Gate grades</h2>{gate_svg}</article>
<article class="card half"><h2>Spend by seat</h2>{cost_svg}<p class="label">{_escape(evaluation["spend"]["warning"])}</p></article>
<article class="card wide"><h2>Gate evidence</h2>{gate_table}</article>
<article class="card wide"><h2>Model usage and reasoning</h2>{model_table}</article>
<article class="card half"><h2>Efficiency</h2><p>{_escape(evaluation["efficiency"]["interpretation"])}</p><p>Total tokens: {evaluation["efficiency"]["total_tokens"]}</p><p>Reasoning share: {evaluation["efficiency"]["reasoning_share"]:.4f}</p><p>Score / known dollar: {_escape(evaluation["efficiency"]["pass_score_per_known_dollar"])}</p></article>
<article class="card half"><h2>Over / under reasoning</h2><p>Over-reasoning flag: <strong>{str(evaluation["reasoning"]["over_reasoning_flag"]).lower()}</strong></p><p>Under-reasoning flag: <strong>{str(evaluation["reasoning"]["under_reasoning_flag"]).lower()}</strong></p><p>{_escape(evaluation["reasoning"]["policy"])}</p></article>
<article class="card half"><h2>Failures</h2>{cards(failures, "No failures supplied.")}</article>
<article class="card half"><h2>Tool-call errors</h2>{cards(tool_errors, "No tool-call errors supplied.")}</article>
<article class="card half"><h2>Unsupported claims / hallucinations</h2>{cards(unsupported, "No unsupported claims found in supplied verification evidence.")}</article>
<article class="card half"><h2>Model honesty</h2><p>{_escape(evaluation["honesty"]["basis"])}</p><p>{_escape(", ".join(evaluation["honesty"]["observations"]) or "No explicit observations supplied.")}</p></article>
<article class="card wide"><h2>Intelligence contribution</h2><p>Contribution remains unknown unless a pinned, same-task, same-config single-model ablation is supplied.</p>{contribution_table}</article>
<article class="card half"><h2>Configuration changes</h2>{cards(config_changes, "No configuration changes supplied.")}</article>
<article class="card half"><h2>Subscription usage</h2>{cards(evaluation["spend"]["subscription_usage"], "No subscription-backed seats participated.")}</article>
<article class="card wide"><h2>Effective settings</h2><pre>{_escape(settings_json)}</pre></article>
</section>
<footer><p>Standalone HTML with inline CSS and SVG. No JavaScript, external assets, fonts, QuantStats, or network requests. Equal canonical input produces equal bytes and SHA-256.</p><p>Input evidence: {_escape(evaluation["input_evidence_sha256"])} | Config: {_escape(evaluation["config_sha256"])}</p></footer>
</main></body></html>"""


def generate_auto_eval(
    evidence: Mapping[str, Any],
    *,
    output_path: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = dict(config or load_config())
    evaluation = evaluate(evidence, config)
    rendered = render_html(evaluation, config)
    report_sha256 = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    if output_path:
        path = Path(output_path).expanduser()
        if not path.is_absolute():
            path = runtime_dir() / "reports" / path
    else:
        run_id = validate_identifier(str(evaluation["run_id"]), "run_id")
        path = runtime_dir() / "reports" / f"{run_id}-{evaluation['evaluation_sha256'][:12]}.html"
    atomic_write_text(path, rendered, mode=0o644)
    return {
        "report_path": str(path),
        "report_sha256": report_sha256,
        "evaluation_sha256": evaluation["evaluation_sha256"],
        "input_evidence_sha256": evaluation["input_evidence_sha256"],
        "deterministic": True,
        "standalone": True,
        "embedded_svg": True,
        "external_assets": False,
        "quantstats": False,
        "evaluation": evaluation,
    }


def collect_run_evidence(run_id: str) -> dict[str, Any]:
    validate_identifier(run_id, "run_id")
    run_path = runtime_dir() / "engine" / "runs" / run_id
    if not run_path.is_dir():
        raise FileNotFoundError(f"Unknown Fusion Drive run: {run_id}")
    manifest = read_json(run_path / "manifest.json") if (run_path / "manifest.json").exists() else {}
    ledger = read_json(run_path / "ledger.json") if (run_path / "ledger.json").exists() else {}
    models = []
    for entry in ledger.get("entries", []):
        if not isinstance(entry, Mapping):
            continue
        usage = entry.get("usage", {}) if isinstance(entry.get("usage"), Mapping) else {}
        models.append(
            {
                "seat": entry.get("seat"),
                "model": entry.get("actual_model") or entry.get("requested_model"),
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "reasoning_tokens": usage.get("reasoning_tokens", 0),
                "latency_seconds": entry.get("latency_seconds", 0),
                "billed_cost_usd": usage.get("cost_usd"),
            }
        )
    lifecycle_path = runtime_dir() / "workflows" / run_id / "host-lifecycle.json"
    lifecycle = read_json(lifecycle_path) if lifecycle_path.exists() else {}
    gates = {
        name: {
            "verdict": receipt.get("verdict"),
            "evidence": receipt.get("evidence", []),
            "reviewers": receipt.get("reviewer_models", []),
        }
        for name, receipt in lifecycle.get("gates", {}).items()
        if isinstance(receipt, Mapping)
    }
    failures = []
    if manifest.get("status") not in {None, "completed", "passed"}:
        failures.append(
            {
                "category": "run_status",
                "message": f"Run status is {manifest.get('status')}",
            }
        )
    config = load_config()
    profile_name = str(
        lifecycle.get("profile_name") or config["active_profile"]
    )
    if profile_name not in config["profiles"]:
        raise ValueError(f"Unknown persisted Fusion Drive profile: {profile_name}")
    return {
        "run_id": run_id,
        "profile": profile_name,
        "engine": str(
            lifecycle.get("engine_name")
            or config["profiles"][profile_name]["engine"]
        ),
        "models": models,
        "gates": gates,
        "tool_calls": [],
        "claims": [],
        "failures": failures,
        "config_changes": [],
        "ablations": [],
    }
