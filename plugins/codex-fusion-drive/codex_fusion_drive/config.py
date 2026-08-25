"""Schema-v2 configuration, proposal, and approval workflow."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from .e2e_policy import validate_e2e_policy
from .errors import ConfigurationError
from .fallback import resolve_model
from .reasoning import normalize_reasoning, seat_reasoning_report
from .util import (
    atomic_write_json,
    canonical_hash,
    deep_get,
    deep_merge,
    deep_set,
    exclusive_lock,
    json_copy,
    now_utc,
    read_json,
    redact,
    reject_plaintext_secrets,
    validate_identifier,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PLUGIN_ROOT / "config" / "fusion-drive.default.json"
SCHEMA_PATH = PLUGIN_ROOT / "schemas" / "fusion-drive.schema.json"


def runtime_dir() -> Path:
    configured = os.environ.get("CODEX_FUSION_DRIVE_HOME")
    path = Path(configured).expanduser() if configured else Path.home() / ".codex" / "codex-fusion-drive"
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_config_path() -> Path:
    return runtime_dir() / "config.json"


def load_schema() -> dict[str, Any]:
    return read_json(SCHEMA_PATH)


def load_config(*, include_user: bool = True, validate: bool = True) -> dict[str, Any]:
    config = read_json(DEFAULT_CONFIG_PATH)
    if include_user and user_config_path().exists():
        config = deep_merge(config, read_json(user_config_path()))
    if validate:
        errors = validate_config(config)
        if errors:
            raise ConfigurationError("Invalid Codex Fusion Drive configuration:\n- " + "\n- ".join(errors))
    return config


# The eight canonical lifecycle gate stages. lifecycle.py owns the transition
# semantics; this set is what every shipped gate set must configure, and a test
# pins it against lifecycle's own transition table so the two cannot diverge.
# Codex has no statusline contract, so the orchestration toggles the host reads
# live in the configuration itself, where the gates' exact-hash approval already
# covers them.
REVIEW_LEVELS = frozenset({"off", "light", "exaflop"})
PRESET_LEVELS = frozenset({"off", "low", "medium", "high"})

CANONICAL_GATE_STAGES = frozenset(
    {
        "synthesis",
        "plan",
        "pre_execution",
        "subagent_pre_execution",
        "subagent_post_execution",
        "post_execution",
        "final",
        "summarize",
    }
)

REPORTING_DEFAULTS: dict[str, Any] = {
    "return_mermaid_after_planning": True,
    "return_full_redacted_config_after_planning": True,
    "return_reasoning_normalization": True,
    "return_updated_report_for_config_proposals": True,
    "max_inline_response_chars": 24000,
}


def reporting_flags(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the `reporting` block, filling in anything the config omits.

    Falls back to the defaults rather than raising, because this is read on the
    response path: a malformed reporting block must not break a tool call that
    otherwise succeeded.
    """

    try:
        block = (config if config is not None else load_config(validate=False)).get("reporting")
    except (ConfigurationError, OSError, ValueError):
        block = None
    flags = dict(REPORTING_DEFAULTS)
    if isinstance(block, Mapping):
        flags.update({key: block[key] for key in REPORTING_DEFAULTS if key in block})
    return flags


def _required_mapping(value: Mapping[str, Any], key: str, errors: list[str]) -> Mapping[str, Any]:
    child = value.get(key)
    if not isinstance(child, Mapping):
        errors.append(f"{key} must be an object")
        return {}
    return child


def orchestration_toggles(config: Mapping[str, Any]) -> dict[str, Any]:
    """The host-orchestration toggles, with quiet defaults.

    Returns the resolved review seats too, so a caller never has to hardcode
    which seats a rung dispatches.
    """
    raw = config.get("orchestration")
    raw = raw if isinstance(raw, Mapping) else {}
    review = str(raw.get("subagent_review", "off"))
    rungs = raw.get("review_rungs")
    rungs = rungs if isinstance(rungs, Mapping) else {}
    return {
        "fusion_plan": bool(raw.get("fusion_plan", False)),
        "preset": str(raw.get("preset", "high")),
        "subagent_review": review,
        "review_seats": list(rungs.get(review, [])) if review != "off" else [],
    }


def _host_model_or_fallback(config: Mapping[str, Any]) -> set[str]:
    """Models that satisfy a rule pinning the Codex host model.

    The pin exists so these roles cannot be quietly downgraded. A declared
    `model_fallbacks` entry is not a quiet downgrade, so the configured
    substitute is accepted alongside the original.
    """

    return {"gpt-5.6-sol", resolve_model("gpt-5.6-sol", config)}


def validate_config(config: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if config.get("schema_version") != 2:
        errors.append("schema_version must equal 2")

    providers = _required_mapping(config, "providers", errors)
    seats = _required_mapping(config, "seats", errors)
    engines = _required_mapping(config, "engines", errors)
    profiles = _required_mapping(config, "profiles", errors)
    gate_sets = _required_mapping(config, "gate_sets", errors)
    presets = _required_mapping(config, "subagent_presets", errors)

    active_profile = config.get("active_profile")
    if active_profile not in profiles:
        errors.append("active_profile must reference profiles")

    for profile_name, profile in profiles.items():
        if not isinstance(profile, Mapping):
            errors.append(f"profiles.{profile_name} must be an object")
            continue
        if profile.get("engine") not in engines:
            errors.append(f"profiles.{profile_name}.engine must reference engines")
        if profile.get("gate_set") not in gate_sets:
            errors.append(f"profiles.{profile_name}.gate_set must reference gate_sets")

    for provider_name, provider in providers.items():
        if not isinstance(provider, Mapping):
            errors.append(f"providers.{provider_name} must be an object")
            continue
        auth = provider.get("auth", {})
        if not isinstance(auth, Mapping):
            errors.append(f"providers.{provider_name}.auth must be an object")
        api_key_env = auth.get("api_key_env") if isinstance(auth, Mapping) else None
        if api_key_env is not None and (not isinstance(api_key_env, str) or not api_key_env):
            errors.append(f"providers.{provider_name}.auth.api_key_env must be a nonempty environment-variable name")
        max_concurrency = provider.get("max_concurrency")
        if not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool) or max_concurrency < 1:
            errors.append(f"providers.{provider_name}.max_concurrency must be a positive integer")
        max_retries = provider.get("max_retries")
        if max_retries is not None and (
            not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0
        ):
            errors.append(f"providers.{provider_name}.max_retries must be a nonnegative integer")
        request_timeout = provider.get("request_timeout_seconds")
        if request_timeout is not None and (
            not isinstance(request_timeout, (int, float))
            or isinstance(request_timeout, bool)
            or request_timeout <= 0
        ):
            errors.append(f"providers.{provider_name}.request_timeout_seconds must be positive")

    for seat_name, seat in seats.items():
        if not isinstance(seat, Mapping):
            errors.append(f"seats.{seat_name} must be an object")
            continue
        provider_name = seat.get("provider")
        if provider_name not in providers:
            errors.append(f"seats.{seat_name}.provider references unknown provider {provider_name!r}")
            continue
        requested = str(seat.get("reasoning", ""))
        normalized = normalize_reasoning(providers[provider_name], str(seat.get("model", "")), requested)
        configured_effective = seat.get("effective_reasoning")
        if configured_effective != normalized["effective"]:
            errors.append(
                f"seats.{seat_name}.effective_reasoning must be {normalized['effective']!r} "
                f"for requested {requested!r}"
            )

    required_panel = ["grok45-panel", "gpt56sol-panel", "fable5-panel"]
    in_harness = engines.get("in_harness", {})
    if not isinstance(in_harness, Mapping):
        errors.append("engines.in_harness must be an object")
    else:
        if in_harness.get("panel") != required_panel:
            errors.append("engines.in_harness.panel must contain Grok 4.5, GPT 5.6 sol, and Fable 5 in canonical order")
        if in_harness.get("judge") != "gpt56sol-judge":
            errors.append("engines.in_harness.judge must be gpt56sol-judge")
        if in_harness.get("fuser") != "gpt56sol-fuser":
            errors.append("engines.in_harness.fuser must be gpt56sol-fuser")
        for seat_name in required_panel + ["gpt56sol-judge", "gpt56sol-fuser"]:
            if seat_name not in seats:
                errors.append(f"canonical in-harness seat is missing: {seat_name}")
            elif seats[seat_name].get("reasoning") != "xhigh":
                errors.append(f"canonical in-harness seat {seat_name} must request xhigh")

    openrouter_fusion = engines.get("openrouter_fusion")
    if not isinstance(openrouter_fusion, Mapping):
        errors.append("engines.openrouter_fusion must be independently configurable")
    elif openrouter_fusion is in_harness:
        errors.append("OpenRouter Fusion and in-harness Fusion must be separate configuration objects")

    maximum = profiles.get("maximum-intelligence", {})
    if not isinstance(maximum, Mapping):
        errors.append("profiles.maximum-intelligence must be an object")
    else:
        budgets = maximum.get("budgets", {})
        if not isinstance(budgets, Mapping):
            errors.append("profiles.maximum-intelligence.budgets must be an object")
        else:
            if budgets.get("max_reasoning_tokens", "missing") is not None:
                errors.append("maximum-intelligence max_reasoning_tokens must be null (unbounded)")
            if budgets.get("max_wall_seconds", "missing") is not None:
                errors.append("maximum-intelligence max_wall_seconds must be null (unbounded)")

    required_stages = set(CANONICAL_GATE_STAGES)
    approval_gates = gate_sets.get("approval-gates", {})
    if not isinstance(approval_gates, Mapping):
        errors.append("gate_sets.approval-gates must be an object")
    else:
        for reviewer in approval_gates.get("reviewers", []):
            seat = seats.get(reviewer, {})
            if seat.get("model") != "grok-4.5" or seat.get("reasoning") != "xhigh":
                errors.append("approval-gates reviewers must use Grok 4.5 with requested xhigh")
        configured_stages = set(approval_gates.get("stages", {}))
        missing = sorted(required_stages - configured_stages)
        if missing:
            errors.append(f"approval-gates is missing stages: {', '.join(missing)}")

    subscription_engine = engines.get("subscription_oauth", {})
    expected_subscription_panel = [
        "grok45-oauth-panel",
        "grok45-oauth-panel-b",
        "fable5-oauth-panel",
    ]
    if not isinstance(subscription_engine, Mapping):
        errors.append("engines.subscription_oauth must be an object")
    else:
        if subscription_engine.get("panel") != expected_subscription_panel:
            errors.append(
                "engines.subscription_oauth.panel must contain two Grok OAuth seats "
                "and one Claude OAuth seat in canonical order"
            )
        if subscription_engine.get("judge") != "grok45-oauth-judge":
            errors.append("engines.subscription_oauth.judge must be grok45-oauth-judge")
        if subscription_engine.get("fuser") != "fable5-oauth-fuser":
            errors.append("engines.subscription_oauth.fuser must be fable5-oauth-fuser")
        for seat_name in expected_subscription_panel + [
            "grok45-oauth-judge",
            "fable5-oauth-fuser",
        ]:
            if seat_name not in seats:
                errors.append(f"subscription OAuth seat is missing: {seat_name}")

    oauth_gates = gate_sets.get("oauth-approval-gates", {})
    expected_oauth_reviewers = [
        "grok45-oauth-gate-primary",
        "grok45-oauth-gate-secondary",
    ]
    if not isinstance(oauth_gates, Mapping):
        errors.append("gate_sets.oauth-approval-gates must be an object")
    else:
        if oauth_gates.get("reviewers") != expected_oauth_reviewers:
            errors.append(
                "oauth-approval-gates reviewers must be the two canonical Grok OAuth gate seats"
            )
        if oauth_gates.get("max_concurrency") != 1:
            errors.append("oauth-approval-gates.max_concurrency must be 1")
        configured_stages = set(oauth_gates.get("stages", {}))
        missing = sorted(required_stages - configured_stages)
        if missing:
            errors.append(f"oauth-approval-gates is missing stages: {', '.join(missing)}")
        for reviewer in expected_oauth_reviewers:
            seat = seats.get(reviewer, {})
            if (
                seat.get("provider") != "grok_oauth"
                or seat.get("model") != "grok-4.5"
                or seat.get("reasoning") != "xhigh"
            ):
                errors.append(
                    "oauth-approval-gates reviewers must use Grok OAuth 4.5 with requested xhigh"
                )

    subscription_profile = profiles.get("subscription-oauth", {})
    if not isinstance(subscription_profile, Mapping):
        errors.append("profiles.subscription-oauth must be an object")
    else:
        if subscription_profile.get("engine") != "subscription_oauth":
            errors.append("profiles.subscription-oauth.engine must be subscription_oauth")
        if subscription_profile.get("gate_set") != "oauth-approval-gates":
            errors.append("profiles.subscription-oauth.gate_set must be oauth-approval-gates")
        budgets = subscription_profile.get("budgets", {})
        if not isinstance(budgets, Mapping):
            errors.append("profiles.subscription-oauth.budgets must be an object")
        elif budgets.get("unknown_cost_policy") != "report_unknown":
            errors.append(
                "profiles.subscription-oauth.budgets.unknown_cost_policy must be report_unknown"
            )

    hybrid_engine = engines.get("xai_claude_oauth", {})
    expected_hybrid_panel = [
        "all-grok-panel-a",
        "all-grok-panel-b",
        "fable5-oauth-panel",
    ]
    if not isinstance(hybrid_engine, Mapping):
        errors.append("engines.xai_claude_oauth must be an object")
    else:
        if hybrid_engine.get("panel") != expected_hybrid_panel:
            errors.append(
                "engines.xai_claude_oauth.panel must contain two direct xAI Grok seats "
                "and one Claude OAuth seat in canonical order"
            )
        if hybrid_engine.get("judge") != "all-grok-judge":
            errors.append("engines.xai_claude_oauth.judge must be all-grok-judge")
        if hybrid_engine.get("fuser") != "fable5-oauth-fuser":
            errors.append("engines.xai_claude_oauth.fuser must be fable5-oauth-fuser")
        if hybrid_engine.get("max_concurrency") != 3:
            errors.append("engines.xai_claude_oauth.max_concurrency must be 3")
        expected_hybrid_providers = {
            "all-grok-panel-a": "xai_api",
            "all-grok-panel-b": "xai_api",
            "fable5-oauth-panel": "claude_oauth",
            "all-grok-judge": "xai_api",
            "fable5-oauth-fuser": "claude_oauth",
        }
        for seat_name, provider_name in expected_hybrid_providers.items():
            seat = seats.get(seat_name, {})
            if seat.get("provider") != provider_name:
                errors.append(
                    f"xai_claude_oauth seat {seat_name} must use provider {provider_name}"
                )

    hybrid_gates = gate_sets.get("xai-serialized-approval-gates", {})
    expected_hybrid_reviewers = [
        "grok45-gate-primary",
        "grok45-gate-secondary",
    ]
    if not isinstance(hybrid_gates, Mapping):
        errors.append("gate_sets.xai-serialized-approval-gates must be an object")
    else:
        if hybrid_gates.get("reviewers") != expected_hybrid_reviewers:
            errors.append(
                "xai-serialized-approval-gates reviewers must be the two canonical direct xAI Grok gate seats"
            )
        if hybrid_gates.get("max_concurrency") != 1:
            errors.append("xai-serialized-approval-gates.max_concurrency must be 1")
        configured_stages = set(hybrid_gates.get("stages", {}))
        missing = sorted(required_stages - configured_stages)
        if missing:
            errors.append(
                "xai-serialized-approval-gates is missing stages: "
                + ", ".join(missing)
            )
        for reviewer in expected_hybrid_reviewers:
            seat = seats.get(reviewer, {})
            if (
                seat.get("provider") != "xai_api"
                or seat.get("model") != "grok-4.5"
                or seat.get("reasoning") != "xhigh"
            ):
                errors.append(
                    "xai-serialized-approval-gates reviewers must use direct xAI Grok 4.5 with requested xhigh"
                )

    hybrid_profile = profiles.get("xai-claude-oauth", {})
    if not isinstance(hybrid_profile, Mapping):
        errors.append("profiles.xai-claude-oauth must be an object")
    else:
        if hybrid_profile.get("engine") != "xai_claude_oauth":
            errors.append("profiles.xai-claude-oauth.engine must be xai_claude_oauth")
        if hybrid_profile.get("gate_set") != "xai-serialized-approval-gates":
            errors.append(
                "profiles.xai-claude-oauth.gate_set must be xai-serialized-approval-gates"
            )
        budgets = hybrid_profile.get("budgets", {})
        if not isinstance(budgets, Mapping):
            errors.append("profiles.xai-claude-oauth.budgets must be an object")
        elif budgets.get("unknown_cost_policy") != "report_unknown":
            errors.append(
                "profiles.xai-claude-oauth.budgets.unknown_cost_policy must be report_unknown"
            )
        execution = hybrid_profile.get("execution", {})
        if not isinstance(execution, Mapping):
            errors.append("profiles.xai-claude-oauth.execution must be an object")
        elif (
            execution.get("owner") != "codex_host"
            or execution.get("model") not in _host_model_or_fallback(config)
            or execution.get("reasoning") != "xhigh"
            or execution.get("require_confirmed_plan") is not True
        ):
            errors.append(
                "profiles.xai-claude-oauth.execution must use Codex host GPT-5.6-sol "
                "(or its configured model_fallbacks target) at xhigh after plan confirmation"
            )

    all_grok = engines.get("all_grok_4_5", {})
    if isinstance(all_grok, Mapping):
        panel = all_grok.get("panel", [])
        if len(panel) != 2:
            errors.append("engines.all_grok_4_5 must have exactly two panel seats")
        role_names = list(panel) + [all_grok.get("judge"), all_grok.get("fuser")]
        if any(seats.get(name, {}).get("model") != "grok-4.5" for name in role_names):
            errors.append("engines.all_grok_4_5 must use Grok 4.5 for two panels, judge, and fuser")
    else:
        errors.append("engines.all_grok_4_5 must be an object")

    drive = presets.get("grok-fusion-drive", {})
    driver = drive.get("driver", {}) if isinstance(drive, Mapping) else {}
    if driver.get("model") not in _host_model_or_fallback(config) or driver.get("reasoning") != "ultra":
        errors.append(
            "grok-fusion-drive driver must be GPT 5.6 sol (or its configured "
            "model_fallbacks target) at ultra"
        )
    if drive.get("worker_engine") != "all_grok_4_5":
        errors.append("grok-fusion-drive workers must use all_grok_4_5")
    if drive.get("gate_set") != "approval-gates":
        errors.append("grok-fusion-drive must inherit approval-gates")

    lifecycle = config.get("lifecycle", {})
    if not isinstance(lifecycle, Mapping) or not lifecycle.get("require_explicit_user_confirmation"):
        errors.append("lifecycle must require explicit user confirmation")
    if not isinstance(lifecycle, Mapping) or not lifecycle.get("require_claude_goal_before_execution"):
        errors.append("lifecycle must require a Codex goal before execution")
    if (
        not isinstance(lifecycle, Mapping)
        or not isinstance(lifecycle.get("host_goal_creation_tool"), str)
        or not lifecycle.get("host_goal_creation_tool")
    ):
        errors.append("lifecycle.host_goal_creation_tool must be a nonempty string")

    orchestration = config.get("orchestration")
    if orchestration is not None:
        if not isinstance(orchestration, Mapping):
            errors.append("orchestration must be an object")
        else:
            review = orchestration.get("subagent_review", "off")
            if review not in REVIEW_LEVELS:
                errors.append(
                    "orchestration.subagent_review must be one of "
                    + ", ".join(sorted(REVIEW_LEVELS))
                )
            preset = orchestration.get("preset", "high")
            if preset not in PRESET_LEVELS:
                errors.append(
                    "orchestration.preset must be one of " + ", ".join(sorted(PRESET_LEVELS))
                )
            if not isinstance(orchestration.get("fusion_plan", False), bool):
                errors.append("orchestration.fusion_plan must be a boolean")
            rungs = orchestration.get("review_rungs", {})
            if not isinstance(rungs, Mapping):
                errors.append("orchestration.review_rungs must be an object")
            else:
                for rung_name, rung_seats in rungs.items():
                    if not isinstance(rung_seats, list) or not rung_seats:
                        errors.append(
                            f"orchestration.review_rungs.{rung_name} must be a nonempty array"
                        )
                        continue
                    # A rung that names a seat which does not exist would fail
                    # only once execution reached it, long after approval.
                    for seat_name in rung_seats:
                        if seat_name not in seats:
                            errors.append(
                                f"orchestration.review_rungs.{rung_name} references "
                                f"unknown seat {seat_name!r}"
                            )
                if review != "off" and review not in rungs:
                    errors.append(
                        f"orchestration.subagent_review is {review!r} but "
                        f"orchestration.review_rungs has no {review!r} entry"
                    )

    auto_eval = config.get("auto_eval", {})
    if not isinstance(auto_eval, Mapping) or not auto_eval.get("standalone_html"):
        errors.append("auto_eval.standalone_html must be true")
    if isinstance(auto_eval, Mapping) and auto_eval.get("external_assets"):
        errors.append("auto_eval.external_assets must be false")
    if isinstance(auto_eval, Mapping) and auto_eval.get("quantstats"):
        errors.append("auto_eval.quantstats must be false")

    errors.extend(validate_e2e_policy(config.get("e2e_policy")))
    errors.extend(reject_plaintext_secrets(config))
    return sorted(set(errors))


def config_get(dotted_path: str) -> Any:
    return deep_get(load_config(), dotted_path)


def set_config_value(dotted_path: str, new_value: Any) -> dict[str, Any]:
    current = read_json(user_config_path()) if user_config_path().exists() else {}
    deep_set(current, dotted_path, new_value)
    candidate = deep_merge(read_json(DEFAULT_CONFIG_PATH), current)
    errors = validate_config(candidate)
    if errors:
        raise ConfigurationError("Rejected configuration change:\n- " + "\n- ".join(errors))
    atomic_write_json(user_config_path(), current)
    return candidate


def proposal_path(proposal_hash: str) -> Path:
    return runtime_dir() / "proposals" / f"{validate_identifier(proposal_hash, 'proposal hash')}.json"


def _reject_unroutable_changes(changes: Mapping[str, Any]) -> list[str]:
    """Catch changes that would merge into a section that does not exist.

    `deep_merge` happily creates whatever key it is given, so a dotted path
    passed here (config_set takes those; this takes nested objects) or a typo'd
    section name lands in the user's config as inert junk and silently does
    nothing. Nested keys stay unchecked because adding a profile or a seat is a
    legitimate new key.
    """

    known_sections = set(read_json(DEFAULT_CONFIG_PATH))
    errors = []
    for key in changes:
        if "." in str(key):
            errors.append(
                f"{key} looks like a dotted path; propose_config takes a nested object "
                "(use config_set for dotted paths)"
            )
        elif key not in known_sections:
            errors.append(f"{key} is not a Codex Fusion Drive configuration section")
    return errors


def propose_config(changes: Mapping[str, Any], *, rationale: str = "") -> dict[str, Any]:
    secret_errors = reject_plaintext_secrets(changes)
    if secret_errors:
        raise ConfigurationError("Rejected proposed secrets:\n- " + "\n- ".join(secret_errors))
    routing_errors = _reject_unroutable_changes(changes)
    if routing_errors:
        raise ConfigurationError("Rejected configuration proposal:\n- " + "\n- ".join(routing_errors))
    current = load_config()
    candidate = deep_merge(current, changes)
    errors = validate_config(candidate)
    if errors:
        raise ConfigurationError("Rejected configuration proposal:\n- " + "\n- ".join(errors))
    base_hash = canonical_hash(current)
    candidate_hash = canonical_hash(candidate)
    proposal_hash = canonical_hash({"base_hash": base_hash, "candidate_hash": candidate_hash, "changes": changes})
    proposal = {
        "schema_version": 1,
        "proposal_hash": proposal_hash,
        "base_config_hash": base_hash,
        "candidate_config_hash": candidate_hash,
        "rationale": rationale,
        "changes": json_copy(changes),
        "candidate": candidate,
        "created_at": now_utc(),
        "status": "proposed",
    }
    path = proposal_path(proposal_hash)
    if path.exists():
        persisted = read_json(path)
        immutable_fields = ("proposal_hash", "base_config_hash", "candidate_config_hash", "changes", "candidate")
        if any(persisted.get(key) != proposal.get(key) for key in immutable_fields):
            raise ConfigurationError("Proposal hash collision with non-identical content")
        proposal = persisted
    else:
        atomic_write_json(path, proposal)
    return {
        "proposal_hash": proposal_hash,
        "base_config_hash": base_hash,
        "candidate_config_hash": candidate_hash,
        "rationale": rationale,
        "changes": redact(changes),
        "reasoning": seat_reasoning_report(candidate),
        "candidate": redact(candidate),
        "requires_final_approval": True,
    }


def approve_config(proposal_hash: str, *, confirmed: bool) -> dict[str, Any]:
    if not confirmed:
        raise ConfigurationError("Configuration approval requires confirmed=true from an explicit user decision")
    # The base-hash compare-and-swap below is only atomic while no concurrent
    # approval can interleave between load_config and the user-config write.
    with exclusive_lock(runtime_dir() / "proposals" / ".config.lock"):
        proposal = read_json(proposal_path(proposal_hash))
        current = load_config()
        if canonical_hash(current) != proposal.get("base_config_hash"):
            raise ConfigurationError("Configuration changed after proposal; create a fresh proposal before approval")
        candidate = proposal.get("candidate")
        if not isinstance(candidate, Mapping) or canonical_hash(candidate) != proposal.get("candidate_config_hash"):
            raise ConfigurationError("Proposal candidate hash does not match persisted content")
        errors = validate_config(candidate)
        if errors:
            raise ConfigurationError("Persisted proposal no longer validates:\n- " + "\n- ".join(errors))
        defaults = read_json(DEFAULT_CONFIG_PATH)
        # Store the complete candidate to preserve the exact approved hash. The file
        # contains no credentials because proposal validation rejects them.
        atomic_write_json(user_config_path(), candidate)
        applied = load_config()
        if canonical_hash(applied) != canonical_hash(deep_merge(defaults, candidate)):
            raise ConfigurationError("Applied configuration hash mismatch")
        proposal["status"] = "approved"
        proposal["approved_at"] = now_utc()
        atomic_write_json(proposal_path(proposal_hash), proposal)
    return {
        "approved": True,
        "proposal_hash": proposal_hash,
        "config_hash": canonical_hash(applied),
        "config_path": str(user_config_path()),
        "config": redact(applied),
        "reasoning": seat_reasoning_report(applied),
    }


def effective_config_report(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    config = dict(config or load_config())
    return {
        "config_hash": canonical_hash(config),
        "active_profile": config.get("active_profile"),
        "reasoning": seat_reasoning_report(config),
        "config": redact(config),
        "validation": {"ok": not validate_config(config), "errors": validate_config(config)},
    }


def config_history(limit: int = 50) -> dict[str, Any]:
    """List persisted configuration proposals, newest first, fully redacted."""

    directory = runtime_dir() / "proposals"
    entries = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            proposal = read_json(path)
            entries.append(
                {
                    "proposal_hash": proposal.get("proposal_hash"),
                    "status": proposal.get("status"),
                    "rationale": proposal.get("rationale", ""),
                    "created_at": proposal.get("created_at"),
                    "approved_at": proposal.get("approved_at"),
                    "base_config_hash": proposal.get("base_config_hash"),
                    "candidate_config_hash": proposal.get("candidate_config_hash"),
                    "changes": redact(proposal.get("changes", {})),
                }
            )
    entries.sort(key=lambda entry: (str(entry.get("created_at") or ""), str(entry.get("proposal_hash") or "")), reverse=True)
    bounded_limit = max(1, int(limit))
    return {
        "count": len(entries),
        "proposals": entries[:bounded_limit],
        "current_config_hash": canonical_hash(load_config()),
    }


def config_rollback_propose(proposal_hash: str, *, rationale: str = "") -> dict[str, Any]:
    """Propose restoring a previously approved configuration; approval still applies."""

    proposal = read_json(proposal_path(proposal_hash))
    if proposal.get("status") != "approved":
        raise ConfigurationError("Rollback target must be a previously approved proposal")
    candidate = proposal.get("candidate")
    if not isinstance(candidate, Mapping) or canonical_hash(candidate) != proposal.get("candidate_config_hash"):
        raise ConfigurationError("Rollback target candidate hash does not match persisted content")
    rollback = propose_config(
        candidate,
        rationale=rationale or f"Roll back to approved proposal {proposal_hash}",
    )
    rollback["rollback_of"] = proposal_hash
    return rollback
