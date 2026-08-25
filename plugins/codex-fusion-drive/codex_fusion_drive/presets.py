"""Resolve immutable per-subagent fusion presets."""

from __future__ import annotations

from typing import Any, Mapping

from .config import load_config
from .errors import ConfigurationError
from .fallback import resolve_model
from .util import canonical_hash, json_copy


def resolve_preset(name: str, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    config = dict(config or load_config())
    preset = config.get("subagent_presets", {}).get(name)
    if not isinstance(preset, Mapping):
        raise ConfigurationError(f"Unknown subagent preset: {name}")
    engine_name = preset.get("worker_engine")
    engine = config.get("engines", {}).get(engine_name)
    if not isinstance(engine, Mapping):
        raise ConfigurationError(f"Preset {name} references unknown engine {engine_name!r}")
    gate_name = preset.get("gate_set")
    gate_set = config.get("gate_sets", {}).get(gate_name)
    if not isinstance(gate_set, Mapping):
        raise ConfigurationError(f"Preset {name} references unknown gate set {gate_name!r}")
    driver = json_copy(preset["driver"])
    configured_driver_model = str(driver.get("model", ""))
    driver_model = resolve_model(configured_driver_model, config)
    if driver_model != configured_driver_model:
        # The host reads this to pick its driver, so the substitution has to
        # travel with the preset rather than being applied silently downstream.
        driver["model"] = driver_model
        driver["model_fallback"] = {"from": configured_driver_model, "to": driver_model}
    resolved = {
        "name": name,
        "driver": driver,
        "worker_engine_name": engine_name,
        "worker_engine": json_copy(engine),
        "worker_reasoning": preset.get("worker_reasoning"),
        "gate_set_name": gate_name,
        "gate_set": json_copy(gate_set),
        "batch_mode": preset.get("batch_mode"),
        "max_parallel_subagents": preset.get("max_parallel_subagents"),
        "max_fusion_depth": preset.get("max_fusion_depth"),
        "host_owned_driver": preset.get("driver", {}).get("owner") == "codex_host",
        "host_action": (
            "The Codex host must select the driver model/reasoning and spawn native subagents; "
            "the MCP server does not recursively launch Claude."
        ),
    }
    resolved["preset_sha256"] = canonical_hash(resolved)
    return resolved


def list_presets(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    config = dict(config or load_config())
    return {name: resolve_preset(name, config) for name in sorted(config.get("subagent_presets", {}))}

