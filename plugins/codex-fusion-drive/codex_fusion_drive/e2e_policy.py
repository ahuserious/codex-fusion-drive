"""Hash-bound e2e_policy overlay. No secrets. Unattended e2e overrides interview."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from typing import Any, Mapping

STABLY_DEPTHS = ("ci_shots_logs", "jig_video_logs", "full_sim_ui")
LOOP_ON_CI = ("n", "y", "heavy")
VALIDATION_GATING = ("on", "off")
AUTHOR_STYLE = "verification-author-first"
TEST_FLOW_AUTHOR_STYLES = (AUTHOR_STYLE,)
LOOP_ON_CI_VALUES = LOOP_ON_CI
VALIDATION_GATING_VALUES = VALIDATION_GATING

DEFAULT_E2E_POLICY: dict[str, Any] = {
    "unattended": False,
    "require_screenshots_per_feature": True,
    "require_video_evidence": True,
    "cursor_cloud_video": True,
    "stably_authoring": True,
    "stably_depth": "full_sim_ui",
    "loop_on_ci": "n",
    "auto_review_and_merge": False,
    "models": "maximum-intelligence",
    "fusion_preset": "maximum-intelligence",
    "engine": "in_harness",
    "validation_gating": "on",
    "test_flow_author_style": AUTHOR_STYLE,
    "codex_oauth_required_for_sol_subscription": True,
    "spawn_isolation": "none",
    "spawn_surface": "side_terminal_teammates",
    "api_jig_required_before_live": True,
    "abc_contract": True,
    "side_terminal_teammates": True,
    "evidence_isolation": "none",
}
E2E_POLICY_DEFAULTS = DEFAULT_E2E_POLICY
ALLOWED_KEYS = set(DEFAULT_E2E_POLICY)

YN_KEYS = {
    "unattended",
    "require_screenshots_per_feature",
    "require_video_evidence",
    "cursor_cloud_video",
    "stably_authoring",
    "auto_review_and_merge",
    "codex_oauth_required_for_sol_subscription",
    "api_jig_required_before_live",
    "abc_contract",
    "side_terminal_teammates",
}
BOOL_KEYS = YN_KEYS
STABLY_CLOUD_COMMAND = ("npx", "stably", "--browser", "cloud", "test")


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def unattended_class_active() -> bool:
    token = os.environ.get("FUSION_UNATTENDED") or os.environ.get("FUSION_E2E_UNATTENDED") or ""
    return token.strip().lower() in {"1", "true", "yes", "y"}


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"y", "yes", "true", "on", "1"}:
        return True
    if text in {"n", "no", "false", "off", "0"}:
        return False
    return default


def _policy_from(raw: Any) -> Mapping[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        return raw  # type: ignore[return-value]
    if "schema_version" in raw and "e2e_policy" in raw:
        nested = raw.get("e2e_policy")
        return nested if isinstance(nested, Mapping) or nested is None else raw
    return raw


def normalize_e2e_policy(raw: Mapping[str, Any] | None = None, *, unattended_class: bool = False) -> dict[str, Any]:
    policy = dict(DEFAULT_E2E_POLICY)
    source = _policy_from(raw) if isinstance(raw, Mapping) else None
    if isinstance(source, Mapping):
        for key, value in source.items():
            if key in DEFAULT_E2E_POLICY:
                policy[key] = value
    for key in YN_KEYS:
        policy[key] = _coerce_bool(policy.get(key), bool(DEFAULT_E2E_POLICY[key]))
    if policy.get("stably_depth") not in STABLY_DEPTHS:
        policy["stably_depth"] = "full_sim_ui" if policy.get("unattended") or unattended_class else "ci_shots_logs"
    if policy.get("loop_on_ci") not in LOOP_ON_CI:
        policy["loop_on_ci"] = "n"
    gating = policy.get("validation_gating")
    if gating is True:
        gating = "on"
    if gating is False:
        gating = "off"
    policy["validation_gating"] = gating if gating in VALIDATION_GATING else "on"
    policy["test_flow_author_style"] = AUTHOR_STYLE
    if policy.get("spawn_isolation") != "none":
        policy["spawn_isolation"] = "none"
    if policy.get("spawn_surface") not in {"side_terminal_teammates", "worktree"}:
        policy["spawn_surface"] = "side_terminal_teammates"
    if unattended_class:
        policy["unattended"] = True
        policy["stably_depth"] = "full_sim_ui"
        policy["stably_authoring"] = True
        policy["require_screenshots_per_feature"] = True
        policy["require_video_evidence"] = True
        policy["cursor_cloud_video"] = True
        policy["validation_gating"] = "on"
        policy["auto_review_and_merge"] = False
        policy["api_jig_required_before_live"] = True
    return policy


def merge_e2e_policy(existing: Mapping[str, Any] | None = None, **updates: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if isinstance(existing, Mapping):
        payload.update({key: existing[key] for key in existing if key in DEFAULT_E2E_POLICY})
    for key, value in updates.items():
        dest = str(key).replace("-", "_")
        if dest not in DEFAULT_E2E_POLICY or value is None:
            continue
        payload[dest] = value
    return normalize_e2e_policy(payload, unattended_class=_coerce_bool(payload.get("unattended"), False))


def effective_e2e_policy(config: Mapping[str, Any] | None = None, *, unattended_class: bool = False) -> dict[str, Any]:
    raw = config.get("e2e_policy") if isinstance(config, Mapping) else None
    return normalize_e2e_policy(raw if isinstance(raw, Mapping) else None, unattended_class=unattended_class)


def load_e2e_policy(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return effective_e2e_policy(config, unattended_class=unattended_class_active())


def validate_e2e_policy(raw: Any) -> list[str]:
    if raw is None:
        return []
    policy = _policy_from(raw)
    if policy is None:
        return []
    if not isinstance(policy, Mapping):
        return ["e2e_policy must be an object"]
    errors: list[str] = []
    for key in YN_KEYS:
        if key in policy and not isinstance(policy[key], bool) and not isinstance(policy[key], str):
            errors.append(f"e2e_policy.{key} must be boolean or y/n")
    depth = policy.get("stably_depth", DEFAULT_E2E_POLICY["stably_depth"])
    if depth not in STABLY_DEPTHS:
        errors.append("e2e_policy.stably_depth must be ci_shots_logs|jig_video_logs|full_sim_ui")
    loop = policy.get("loop_on_ci", DEFAULT_E2E_POLICY["loop_on_ci"])
    if loop not in LOOP_ON_CI:
        errors.append("e2e_policy.loop_on_ci must be n|y|heavy")
    gating = policy.get("validation_gating", DEFAULT_E2E_POLICY["validation_gating"])
    if gating not in VALIDATION_GATING and gating not in {True, False}:
        errors.append("e2e_policy.validation_gating must be on|off")
    style = policy.get("test_flow_author_style", AUTHOR_STYLE)
    if style not in {AUTHOR_STYLE, None}:
        errors.append("e2e_policy.test_flow_author_style must be verification-author-first")
    isolation = policy.get("spawn_isolation", "none")
    if isolation not in {None, "none"}:
        errors.append("e2e_policy.spawn_isolation must be none")
    surface = policy.get("spawn_surface", "side_terminal_teammates")
    if surface not in {None, "side_terminal_teammates", "worktree"}:
        errors.append("e2e_policy.spawn_surface must be side_terminal_teammates or worktree")
    for key in ("fusion_preset", "engine", "models"):
        if key in policy and (not isinstance(policy[key], str) or not policy[key].strip()):
            errors.append(f"e2e_policy.{key} must be a nonempty string")
    return errors


def policy_hash(policy: Mapping[str, Any]) -> str:
    return _canonical_hash(normalize_e2e_policy(policy))


e2e_policy_hash = policy_hash


def format_e2e_policy(policy: Mapping[str, Any]) -> str:
    normalized = normalize_e2e_policy(policy)
    lines = ["e2e_policy (no secrets)"]
    for key in sorted(normalized):
        value = normalized[key]
        if key in YN_KEYS:
            value = "y" if value else "n"
        lines.append(f"  {key}: {value}")
    lines.append(f"policy_sha256 {policy_hash(normalized)}")
    return "\n".join(lines)


def stably_command_argv() -> tuple[str, ...]:
    return STABLY_CLOUD_COMMAND


def parse_boolish(value: Any) -> bool:
    return _coerce_bool(value, False)


def codex_oauth_named_blocker(
    config: Mapping[str, Any] | None = None,
    *,
    required_seats: list[str] | None = None,
) -> dict[str, Any]:
    config = config or {}
    providers = config.get("providers") if isinstance(config.get("providers"), Mapping) else {}
    seats = config.get("seats") if isinstance(config.get("seats"), Mapping) else {}
    sol_subscription = [
        name
        for name, seat in seats.items()
        if isinstance(seat, Mapping) and str(seat.get("provider")) == "codex_oauth"
    ]
    required = list(required_seats or [])
    required_uses_codex = any(
        isinstance(seats.get(name), Mapping) and str(seats[name].get("provider")) == "codex_oauth"
        for name in required
    )
    provider = providers.get("codex_oauth") if isinstance(providers, Mapping) else None
    present = isinstance(provider, Mapping)
    enabled = bool(present and provider.get("enabled"))
    command = str(provider.get("command") or "codex") if present else "codex"
    binary = shutil.which(command)
    missing = (not present) or (not enabled) or (binary is None)
    message = (
        "named blocker: missing_codex_oauth. Sol-via-subscription seats require provider "
        "codex_oauth. Do not silently fall back to OpenRouter openai/gpt-5.6-sol (1.05M)."
        if missing
        else "codex_oauth present for Sol-via-subscription; tokens are not printed"
    )
    return {
        "name": "codex_oauth",
        "required_for": sol_subscription or ["sol-via-subscription seats"],
        "sol_subscription_seats": sol_subscription,
        "present": present,
        "enabled": enabled,
        "command": command,
        "binary_available": binary is not None,
        "binary_present": binary is not None,
        "blocker": missing,
        "named_blocker": missing,
        "profile_requires_codex_oauth": required_uses_codex,
        "silent_fallback": False,
        "auth_value_accessed": False,
        "status": "BLOCKER" if missing else "ok",
        "issue": message if missing else None,
        "message": message,
    }


def named_codex_oauth_blocker(
    config: Mapping[str, Any] | None = None,
    *,
    required_seats: list[str] | None = None,
) -> str | None:
    info = codex_oauth_named_blocker(config, required_seats=required_seats)
    if info["named_blocker"] and (required_seats is None or info["profile_requires_codex_oauth"]):
        if required_seats is not None and not info["profile_requires_codex_oauth"]:
            return None
        return str(info["message"])
    return None


def doctor_named_blockers(config: Mapping[str, Any], required_seats: list[str] | None = None) -> dict[str, Any]:
    info = codex_oauth_named_blocker(config, required_seats=required_seats)
    blockers = [info] if info.get("named_blocker") else []
    return {"blockers": blockers, "codex_oauth": info}


def codex_oauth_status_line(config: Mapping[str, Any] | None = None) -> str:
    info = codex_oauth_named_blocker(config)
    state = "BLOCKER" if info["named_blocker"] else "ok"
    binary = "binary=yes" if info["binary_available"] else "binary=no"
    return f"{state} {binary} silent_fallback=false — {info['message']}"
