"""Human-facing workflow and configuration reports."""

from __future__ import annotations

from typing import Any, Mapping

from .config import effective_config_report, load_config, reporting_flags
from .errors import ConfigurationError
from .presets import list_presets
from .util import json_copy


def workflow_mermaid(config: Mapping[str, Any] | None = None, *, profile_name: str | None = None) -> str:
    config = dict(config or load_config())
    profile_name = profile_name or str(config.get("active_profile"))
    if profile_name not in config.get("profiles", {}):
        raise ConfigurationError(f"Unknown Fusion Drive profile: {profile_name}")
    profile = config["profiles"][profile_name]
    host_goal_creation_tool = str(
        config["lifecycle"]["host_goal_creation_tool"]
    )
    engine_name = profile["engine"]
    engine = config["engines"][engine_name]
    panel = list(engine.get("panel", []))
    lines = [
        "flowchart TD",
        '  U["User task"] --> P["Codex planning host"]',
        f'  P --> E["Fusion engine: {engine_name}"]',
    ]
    for index, seat_name in enumerate(panel):
        seat = config["seats"][seat_name]
        node = f"M{index + 1}"
        lines.append(
            f'  E --> {node}["{seat_name}<br/>{seat["model"]}<br/>requested {seat["reasoning"]} / effective {seat["effective_reasoning"]}"]'
        )
    if engine_name == "openrouter_fusion":
        lines.append('  E --> OR["OpenRouter server-side panel and judge<br/>separately configured"]')
        lines.append('  OR --> F["OpenRouter Fusion result"]')
    else:
        judge = config["seats"][engine["judge"]]
        fuser = config["seats"][engine["fuser"]]
        lines.append(f'  E --> J["Judge: {judge["model"]}<br/>{judge["reasoning"]}"]')
        for index in range(len(panel)):
            lines.append(f"  M{index + 1} --> J")
        lines.append(f'  J --> F["Fuser: {fuser["model"]}<br/>{fuser["reasoning"]}"]')
    lines.extend(
        [
            '  F --> G0["Synthesis gate"]',
            '  G0 --> GP["Plan approval gate<br/>Grok 4.5 xhigh intent / high wire"]',
            '  GP --> C{"User confirms exact plan?"}',
            '  C -- "No" --> P',
            (
                '  C -- "Yes, execute" --> '
                f'CG["Codex host {host_goal_creation_tool}"]'
            ),
            '  CG --> GE["Pre-execution gate"]',
            '  GE --> X["Host-owned execution"]',
            '  X --> GS["Subagent and post-execution gates"]',
            '  GS --> GF["Final gate"]',
            '  GF --> SU["Summary gate"]',
            '  SU --> AE["Deterministic auto-eval HTML/SVG"]',
            '  P -. "optional preset" .-> D["gpt-5.6-sol ultra driver"]',
            '  D --> AG["2 Grok panels + Grok judge + Grok fuser"]',
            '  AG --> GS',
        ]
    )
    return "\n".join(lines)


def gate_inventory(
    config: Mapping[str, Any] | None = None,
    *,
    profile_name: str | None = None,
) -> list[dict[str, Any]]:
    config = dict(config or load_config())
    profile_name = profile_name or str(config["active_profile"])
    if profile_name not in config.get("profiles", {}):
        raise ConfigurationError(f"Unknown Fusion Drive profile: {profile_name}")
    profile = config["profiles"][profile_name]
    gate_set = config["gate_sets"][profile["gate_set"]]
    rows = []
    for name, stage in gate_set["stages"].items():
        rows.append(
            {
                "stage": name,
                "owner": stage["owner"],
                "automatic": stage["automatic"],
                "reviewers": json_copy(gate_set["reviewers"]),
                "requested_reasoning": gate_set["requested_reasoning"],
                "effective_reasoning": gate_set["effective_reasoning"],
                "required_evidence": json_copy(stage["required_evidence"]),
            }
        )
    return rows


def workflow_report(config: Mapping[str, Any] | None = None, *, profile_name: str | None = None) -> dict[str, Any]:
    config = dict(config or load_config())
    profile_name = profile_name or str(config["active_profile"])
    flags = reporting_flags(config)
    effective = effective_config_report(config)
    # The redacted config is by far the largest thing this report carries and it
    # duplicates config_show verbatim; config_hash and validation stay either way
    # because the exact-hash approval flow depends on them.
    if not flags["return_full_redacted_config_after_planning"]:
        effective.pop("config", None)
    if not flags["return_reasoning_normalization"]:
        effective.pop("reasoning", None)
    report = {
        "profile": profile_name,
        "gates": gate_inventory(config, profile_name=profile_name),
        "subagent_presets": list_presets(config),
        "batching": json_copy(config["batching"]),
        "integrations": json_copy(config["integrations"]),
        "lifecycle": json_copy(config["lifecycle"]),
        **effective,
    }
    if flags["return_mermaid_after_planning"]:
        report["mermaid"] = workflow_mermaid(config, profile_name=profile_name)
    return report


def gate_set_list(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """List every configured gate set with reviewers, quorum, and stages."""

    config = dict(config or load_config())
    seats = config.get("seats", {})
    gate_sets = []
    for name, gate_set in config.get("gate_sets", {}).items():
        reviewers = []
        for reviewer_name in gate_set.get("reviewers", []):
            seat = seats.get(reviewer_name, {})
            reviewers.append(
                {
                    "seat": reviewer_name,
                    "provider": seat.get("provider"),
                    "model": seat.get("model"),
                    "requested_reasoning": seat.get("reasoning"),
                    "effective_reasoning": seat.get("effective_reasoning"),
                }
            )
        gate_sets.append(
            {
                "name": name,
                "enabled": bool(gate_set.get("enabled")),
                "fail_closed": bool(gate_set.get("fail_closed")),
                "required_passes": gate_set.get("required_passes"),
                "max_concurrency": gate_set.get("max_concurrency"),
                "requested_reasoning": gate_set.get("requested_reasoning"),
                "effective_reasoning": gate_set.get("effective_reasoning"),
                "stages": sorted(gate_set.get("stages", {})),
                "reviewers": reviewers,
            }
        )
    return {"count": len(gate_sets), "gate_sets": gate_sets}


def provider_list(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """List configured provider routes with auth modes, never credential values."""

    config = dict(config or load_config())
    providers = []
    for name, provider in config.get("providers", {}).items():
        auth = provider.get("auth", {}) if isinstance(provider.get("auth"), Mapping) else {}
        providers.append(
            {
                "name": name,
                "enabled": bool(provider.get("enabled")),
                "transport": provider.get("transport"),
                "billing": provider.get("billing"),
                "auth_mode": auth.get("mode"),
                "api_key_env": auth.get("api_key_env"),
                "max_concurrency": provider.get("max_concurrency"),
                "async_batch_supported": bool(
                    (provider.get("async_batch") or {}).get("supported")
                ),
            }
        )
    return {"count": len(providers), "providers": providers}


def _legacy_pricing_by_model(config_dir: Any) -> dict[str, dict[str, Any]]:
    import json
    from pathlib import Path as _Path

    legacy_path = _Path(config_dir) / "default.json"
    if not legacy_path.is_file():
        return {}
    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    pricing_by_model: dict[str, dict[str, Any]] = {}
    for seat in legacy.get("seats", {}).values():
        if isinstance(seat, Mapping) and isinstance(seat.get("pricing"), Mapping):
            pricing_by_model.setdefault(str(seat.get("model")), dict(seat["pricing"]))
    return pricing_by_model


def cost_estimate(
    config: Mapping[str, Any] | None = None,
    *,
    profile_name: str | None = None,
    assumed_input_tokens_per_call: int = 20000,
    calls_per_seat: int = 1,
) -> dict[str, Any]:
    """Bounded per-seat cost estimate; unknown costs stay explicitly unknown."""

    from .config import DEFAULT_CONFIG_PATH

    config = dict(config or load_config())
    profile_name = profile_name or str(config["active_profile"])
    if profile_name not in config.get("profiles", {}):
        raise ConfigurationError(f"Unknown Fusion Drive profile: {profile_name}")
    profile = config["profiles"][profile_name]
    engine = config["engines"][profile["engine"]]
    gate_set = config["gate_sets"][profile["gate_set"]]
    seat_names = list(engine.get("panel", []))
    for role_key in ("judge", "fuser", "seat"):
        role_seat = engine.get(role_key)
        if isinstance(role_seat, str):
            seat_names.append(role_seat)
    seat_names.extend(gate_set.get("reviewers", []))
    pricing_by_model = _legacy_pricing_by_model(DEFAULT_CONFIG_PATH.parent)
    providers = config.get("providers", {})
    seats = config.get("seats", {})
    rows = []
    known_total = 0.0
    unknown_models = set()
    for seat_name in dict.fromkeys(seat_names):
        seat = seats.get(seat_name, {})
        provider = providers.get(seat.get("provider"), {})
        billing = provider.get("billing")
        model = str(seat.get("model"))
        pricing = pricing_by_model.get(model)
        row = {
            "seat": seat_name,
            "model": model,
            "provider": seat.get("provider"),
            "billing": billing,
            "calls": calls_per_seat,
            "max_output_tokens": seat.get("max_output_tokens"),
        }
        if billing == "metered_api" and pricing:
            input_rate = float(pricing["input_per_million_usd"])
            output_rate = float(pricing["output_per_million_usd"])
            per_call = (
                assumed_input_tokens_per_call / 1_000_000 * input_rate
                + float(seat.get("max_output_tokens") or 0) / 1_000_000 * output_rate
            )
            row["known_pricing"] = True
            row["max_cost_usd"] = round(per_call * calls_per_seat, 4)
            known_total += per_call * calls_per_seat
        else:
            row["known_pricing"] = False
            row["max_cost_usd"] = None
            unknown_models.add(model)
        rows.append(row)
    budgets = profile.get("budgets", {})
    return {
        "profile": profile_name,
        "engine": profile["engine"],
        "gate_set": profile["gate_set"],
        "assumptions": {
            "assumed_input_tokens_per_call": assumed_input_tokens_per_call,
            "calls_per_seat": calls_per_seat,
            "output_bounded_by": "seat max_output_tokens",
            "unknown_cost_policy": budgets.get("unknown_cost_policy"),
        },
        "seats": rows,
        "known_metered_max_cost_usd": round(known_total, 4),
        "unknown_cost_models": sorted(unknown_models),
        "budget_max_cost_usd": budgets.get("max_cost_usd"),
    }
