"""Provider-aware reasoning semantics.

`requested` is the user-facing intelligence intent. `effective` is the exact
wire/CLI value. Keeping both prevents an unsupported literal from being sent
while still making provider ceilings visible.
"""

from __future__ import annotations

from typing import Any, Mapping


ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def normalize_reasoning(provider: Mapping[str, Any], model: str, requested: str) -> dict[str, str]:
    transport = str(provider.get("transport", ""))
    requested = str(requested).lower()
    if requested not in ORDER:
        return {
            "requested": requested,
            "effective": "high",
            "normalization": "invalid_requested_effort",
            "detail": f"Unknown reasoning effort {requested!r}; fail-closed validation should reject this configuration.",
        }

    if transport in {"xai_responses", "grok_cli_oauth"} and model in {"grok-4.5", "x-ai/grok-4.5"}:
        effective = "high" if requested in {"xhigh", "max", "ultra"} else requested
        return {
            "requested": requested,
            "effective": effective,
            "normalization": "provider_ceiling" if effective != requested else "identity",
            "detail": "Grok 4.5 exposes low, medium, and high; xhigh intent is sent as high.",
        }

    if transport == "claude_cli_oauth":
        effective = "max" if requested in {"xhigh", "max", "ultra"} else requested
        return {
            "requested": requested,
            "effective": effective,
            "normalization": "provider_equivalent" if effective != requested else "identity",
            "detail": "Claude Code exposes max rather than xhigh for its highest CLI effort.",
        }

    if transport == "codex_cli_oauth":
        # Verified against codex-cli 0.144.5: the API rejects "minimal" for
        # gpt-5.6-sol and names its supported set none/low/medium/high/xhigh/max.
        effective = {"minimal": "low", "ultra": "max"}.get(requested, requested)
        return {
            "requested": requested,
            "effective": effective,
            "normalization": "provider_ceiling" if effective != requested else "identity",
            "detail": "Codex accepts none, low, medium, high, xhigh, and max; minimal is rejected upstream.",
        }

    if transport == "codex_host":
        effective = requested
        return {
            "requested": requested,
            "effective": effective,
            "normalization": "identity",
            "detail": "The Codex host owns native model and reasoning selection.",
        }

    if transport in {"openrouter_chat", "openrouter_fusion"}:
        effective = requested if requested in {"none", "minimal", "low", "medium", "high", "xhigh", "max"} else "max"
        return {
            "requested": requested,
            "effective": effective,
            "normalization": "router_mapping" if effective != requested else "identity",
            "detail": "OpenRouter receives the explicit effort and may map it to the nearest model-supported level.",
        }

    effective = requested if requested not in {"ultra"} else "max"
    return {
        "requested": requested,
        "effective": effective,
        "normalization": "generic_ceiling" if effective != requested else "identity",
        "detail": "No provider-specific override is configured.",
    }


def seat_reasoning_report(config: Mapping[str, Any]) -> list[dict[str, str]]:
    providers = config.get("providers", {})
    rows: list[dict[str, str]] = []
    for seat_name, seat in sorted(config.get("seats", {}).items()):
        provider_name = str(seat.get("provider", ""))
        provider = providers.get(provider_name, {})
        normalized = normalize_reasoning(provider, str(seat.get("model", "")), str(seat.get("reasoning", "xhigh")))
        rows.append(
            {
                "seat": str(seat_name),
                "role": str(seat.get("role", "")),
                "provider": provider_name,
                "model": str(seat.get("model", "")),
                **normalized,
            }
        )
    return rows

