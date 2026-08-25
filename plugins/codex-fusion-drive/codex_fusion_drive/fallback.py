"""Substitute an unavailable model with a configured stand-in.

Fable 5 bills the Claude subscription. When that allowance runs out every place
it is named fails at once — panel seats, the host execution model, and every
subagent driver — and each one has to be edited separately to recover. A single
`model_fallbacks` map redirects the model at each of those read points instead,
and the statusline reports the substitution so the swap is never silent.

Provider-prefixed slugs (`anthropic/claude-fable-5`) keep their prefix, so an
OpenRouter seat and a subscription seat can share one map entry.
"""

from __future__ import annotations

from typing import Any, Mapping


def model_fallbacks(config: Mapping[str, Any]) -> dict[str, str]:
    raw = config.get("model_fallbacks")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(source): str(target)
        for source, target in raw.items()
        if isinstance(target, str) and target
    }


def resolve_model(model: str, config: Mapping[str, Any]) -> str:
    """Return the model that should actually be dispatched for `model`."""

    fallbacks = model_fallbacks(config)
    if not fallbacks:
        return model
    if model in fallbacks:
        return fallbacks[model]
    prefix, separator, bare = model.rpartition("/")
    if separator and bare in fallbacks:
        return f"{prefix}{separator}{fallbacks[bare]}"
    return model


def is_substituted(model: str, config: Mapping[str, Any]) -> bool:
    return resolve_model(model, config) != model


def active_substitutions(config: Mapping[str, Any]) -> list[dict[str, str]]:
    """Every configured model the live config would actually redirect.

    Used by the statusline: reporting the whole map would advertise fallbacks
    for models this configuration never references.
    """

    fallbacks = model_fallbacks(config)
    if not fallbacks:
        return []

    referenced: set[str] = set()
    for seat in config.get("seats", {}).values():
        if isinstance(seat, Mapping) and seat.get("model"):
            referenced.add(str(seat["model"]))
    for profile in config.get("profiles", {}).values():
        if not isinstance(profile, Mapping):
            continue
        execution = profile.get("execution")
        if isinstance(execution, Mapping) and execution.get("model"):
            referenced.add(str(execution["model"]))
    for preset in config.get("subagent_presets", {}).values():
        if not isinstance(preset, Mapping):
            continue
        driver = preset.get("driver")
        if isinstance(driver, Mapping) and driver.get("model"):
            referenced.add(str(driver["model"]))

    seen: dict[str, str] = {}
    for model in sorted(referenced):
        target = resolve_model(model, config)
        if target != model:
            seen[model] = target
    return [{"from": source, "to": target} for source, target in sorted(seen.items())]
