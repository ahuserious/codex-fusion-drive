"""Bridge schema-v2 Fusion Drive profiles into the proven fusion runtime."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Mapping, Optional

from relentless_inception.config import load_config as load_legacy_config
from relentless_inception.orchestrator import FusionOrchestrator
from relentless_inception.providers import ProviderRegistry

from .batching import execute_microbatch
from .config import load_config, runtime_dir, validate_config
from .continuation import (
    hash_source_run_tree,
    validated_execution_source_snapshot,
)
from .errors import (
    CapabilityError,
    ConfigurationError,
    ExternalActionRequired,
    LifecycleError,
    LockTimeout,
)
from .fallback import resolve_model
from .lifecycle import (
    initialize_lifecycle,
    record_gate,
    validate_initialized_lifecycle,
)
from .oauth import CLI_OAUTH_TRANSPORTS, SubscriptionCliAdapter
from .report import workflow_report
from .util import canonical_hash, json_copy, text_hash


class HybridProviderRegistry(ProviderRegistry):
    """Delegate HTTP seats to the inherited registry and OAuth seats to CLIs."""

    def __init__(self, legacy_config: Mapping[str, Any], drive_config: Mapping[str, Any]):
        super().__init__(legacy_config)
        self.drive_config = dict(drive_config)
        self.oauth_adapter = SubscriptionCliAdapter(self.drive_config)

    def complete(
        self,
        seat_name: str,
        *,
        system: str,
        prompt: str,
        response_schema: Optional[Mapping[str, Any]] = None,
        schema_name: str = "structured_response",
        before_attempt: Any = None,
        on_semantic_failure_response: Any = None,
    ) -> Any:
        drive_seat = self.drive_config.get("seats", {}).get(seat_name, {})
        provider_name = drive_seat.get("provider")
        transport = self.drive_config.get("providers", {}).get(provider_name, {}).get("transport")
        if transport in CLI_OAUTH_TRANSPORTS:
            return self.oauth_adapter.complete(
                seat_name,
                system=system,
                prompt=prompt,
                response_schema=response_schema,
                schema_name=schema_name,
                before_attempt=before_attempt,
                on_semantic_failure_response=on_semantic_failure_response,
            )
        return super().complete(
            seat_name,
            system=system,
            prompt=prompt,
            response_schema=response_schema,
            schema_name=schema_name,
            before_attempt=before_attempt,
            on_semantic_failure_response=on_semantic_failure_response,
        )


def _legacy_provider(
    legacy: Mapping[str, Any],
    drive_provider: Mapping[str, Any],
) -> dict[str, Any]:
    transport = drive_provider["transport"]
    template_name = {
        "xai_responses": "xai_direct",
        "openrouter_chat": "openrouter",
        "openrouter_fusion": "openrouter_native_fusion",
        "openai_responses": "openai_direct",
        "anthropic_messages": "anthropic_direct",
    }.get(transport)
    if template_name:
        provider = copy.deepcopy(legacy["providers"][template_name])
    else:
        provider = {
            "enabled": True,
            "type": transport,
            "base_url": "cli://local",
            "api_key_env": drive_provider.get("auth", {}).get("api_key_env", "UNUSED_API_KEY"),
            "connect_timeout_seconds": 30,
            "request_timeout_seconds": drive_provider.get("request_timeout_seconds", 1800),
            "max_retries": 0,
            "max_concurrency": 1,
            "retry_statuses": [],
            "capabilities": {
                "tools": False,
                "structured_outputs": True,
                "reasoning": True,
                "streaming": False,
            },
        }
    provider["enabled"] = bool(drive_provider.get("enabled"))
    provider["max_concurrency"] = int(drive_provider.get("max_concurrency", 1))
    if "request_timeout_seconds" in drive_provider:
        provider["request_timeout_seconds"] = drive_provider["request_timeout_seconds"]
    if "max_retries" in drive_provider:
        provider["max_retries"] = drive_provider["max_retries"]
    api_key_env = drive_provider.get("auth", {}).get("api_key_env")
    if api_key_env:
        provider["api_key_env"] = api_key_env
    return provider


def _legacy_seat(
    legacy: Mapping[str, Any],
    seat: Mapping[str, Any],
    drive_config: Mapping[str, Any],
) -> dict[str, Any]:
    role = str(seat["role"])
    panel_template = (
        "grok45_researcher"
        if seat.get("provider") == "xai_api"
        else "openrouter_sol_pro_panel"
    )
    template_name = {
        "panel": panel_template,
        "judge": "grok45_judge",
        "fuser": "grok45_synthesizer",
        "verifier": "grok45_verifier",
    }[role]
    template = legacy["seats"][template_name]
    seat_model = resolve_model(str(seat["model"]), drive_config)
    translated = copy.deepcopy(template)
    translated.update(
        {
            "enabled": bool(seat.get("enabled", True)),
            "provider": seat["provider"],
            "model": seat_model,
            "role": "synthesizer" if role == "fuser" else role,
            "persona": seat["persona"],
            "reasoning_effort": seat["effective_reasoning"],
            "reasoning_max_tokens": seat.get("reasoning_max_tokens"),
            "max_output_tokens": seat["max_output_tokens"],
            "timeout_seconds": seat["timeout_seconds"],
            "tool_policy": seat.get("tool_policy", "none"),
            "server_tools": [],
            "first_tool_required": False,
            "structured_output": seat["structured_output"],
            "allow_model_fallbacks": False,
            "fallback_models": [],
            "fallback_seats": [],
        }
    )
    if "openrouter_fusion" in seat:
        translated["fusion"] = copy.deepcopy(seat["openrouter_fusion"])
    else:
        translated.pop("fusion", None)
    if seat_model != template.get("model"):
        # Template pricing tables are per-model; a retargeted seat must report an
        # honest unknown cost rather than bill at the template model's rates.
        translated.pop("pricing", None)
    return translated


def translate_config(
    drive_config: Mapping[str, Any],
    *,
    profile_name: str | None = None,
) -> tuple[dict[str, Any], str]:
    errors = validate_config(drive_config)
    if errors:
        raise ConfigurationError("Cannot translate invalid Fusion Drive configuration:\n- " + "\n- ".join(errors))
    profile_name = profile_name or str(drive_config["active_profile"])
    drive_profile = drive_config.get("profiles", {}).get(profile_name)
    if not isinstance(drive_profile, Mapping):
        raise ConfigurationError(f"Unknown Fusion Drive profile: {profile_name}")
    engine_name = str(drive_profile["engine"])
    engine = drive_config["engines"][engine_name]
    gate_set = drive_config["gate_sets"][drive_profile["gate_set"]]

    legacy = load_legacy_config(include_user=False, validate=False)
    legacy["providers"] = {
        name: _legacy_provider(legacy, provider)
        for name, provider in drive_config["providers"].items()
        if provider["transport"] != "codex_host"
    }
    if engine_name == "openrouter_fusion":
        legacy["providers"]["openrouter_fusion_api"]["enabled"] = True
    legacy["seats"] = {
        name: _legacy_seat(load_legacy_config(include_user=False, validate=False), seat, drive_config)
        for name, seat in drive_config["seats"].items()
        if drive_config["providers"][seat["provider"]]["transport"] != "codex_host"
    }

    base_profile = copy.deepcopy(load_legacy_config(include_user=False, validate=False)["profiles"]["maximum_intelligence"])
    fallback_engine = drive_config["engines"]["in_harness"] if engine_name == "openrouter_fusion" else engine
    panel = list(fallback_engine.get("panel", []))
    base_profile["fusion"].update(
        {
            "engine": "client_orchestrated",
            "panel": panel,
            "optional_panel": list(fallback_engine.get("optional_panel", [])),
            "judge": fallback_engine.get("judge", "gpt56sol-judge"),
            "synthesizer": fallback_engine.get("fuser", "gpt56sol-fuser"),
            "native_fusion_seat": engine.get("seat", "openrouter-fusion-seat"),
            "min_live_seats": int(engine.get("min_live_seats", max(1, len(panel)))),
            "max_panel_seats": max(1, len(panel) + len(fallback_engine.get("optional_panel", []))),
            "max_concurrency": int(fallback_engine.get("max_concurrency", 1)),
            "independent_first_pass": bool(fallback_engine.get("independent_first_pass", True)),
            "anonymize_model_identity": bool(fallback_engine.get("anonymize_model_identity", True)),
            "prohibit_majority_vote": bool(fallback_engine.get("prohibit_majority_vote", True)),
            "preserve_minority_findings": bool(fallback_engine.get("preserve_minority_findings", True)),
        }
    )
    base_profile["fusion"]["native_openrouter_fusion"] = {
        "enabled": engine_name == "openrouter_fusion",
        "fallback_to_client_orchestrated": engine.get("fallback_engine") == "in_harness",
    }
    base_profile["gates"].update(
        {
            "enabled": bool(gate_set["enabled"]),
            "fail_closed": bool(gate_set["fail_closed"]),
            "reviewers": list(gate_set["reviewers"]),
            "max_concurrency": int(gate_set["max_concurrency"]),
            "required_passes": int(gate_set["required_passes"]),
            "max_revision_cycles": int(gate_set["max_revision_cycles"]),
        }
    )
    original_stages = copy.deepcopy(base_profile["gates"]["stages"])
    translated_stages = {}
    for stage_name, stage in gate_set["stages"].items():
        if stage_name == "synthesis":
            continue
        template = original_stages.get(stage_name, original_stages["pre_execution"])
        translated_stages[stage_name] = {
            **template,
            "enabled": True,
            "required_evidence": copy.deepcopy(stage["required_evidence"]),
        }
    base_profile["gates"]["stages"] = translated_stages
    for key, value in drive_profile["budgets"].items():
        if key in base_profile["budgets"]:
            base_profile["budgets"][key] = copy.deepcopy(value)
    base_profile["execution"].update(
        {
            "model": resolve_model(drive_profile["execution"]["model"], drive_config),
            "reasoning_effort": drive_profile["execution"]["reasoning"],
            "allow_recursive_codex_cli": False,
            "require_fused_plan": True,
            "require_pre_execution_gate": True,
            "require_post_execution_gate": True,
        }
    )
    base_profile["observability"]["enabled"] = True
    base_profile["observability"]["artifact_directory"] = str(runtime_dir() / "engine" / "runs")

    translated_profile_name = "fusion_drive"
    legacy["profiles"] = {translated_profile_name: base_profile}
    legacy["active_profile"] = translated_profile_name
    legacy["native_claude"].update(
        {
            "enabled": True,
            "mode": "host_handoff",
            "executor_model": resolve_model(drive_profile["execution"]["model"], drive_config),
            "executor_reasoning_effort": drive_profile["execution"]["reasoning"],
            "reviewer_models": [resolve_model("claude-fable-5", drive_config)],
            "reviewer_reasoning_effort": "xhigh",
            "require_fusion_before_execution": True,
            "require_gate_after_execution": True,
        }
    )
    return legacy, translated_profile_name


def _gate_verdict(gate: Mapping[str, Any]) -> str:
    """Derive the lifecycle verdict from an orchestrator gate result dict."""
    explicit = str(gate.get("verdict") or "").upper()
    if explicit in {"PASS", "NEEDS_WORK", "FAIL"}:
        return explicit
    if gate.get("passed"):
        return "PASS"
    negative_reviews = gate.get("negative_verdicts") or []
    blocked_beyond_reviews = (
        gate.get("mechanical_blocked")
        or gate.get("schema_blocked")
        or gate.get("blind_spot_blocked")
    )
    if (
        negative_reviews
        and not blocked_beyond_reviews
        and all(
            (review.get("verdict") or {}).get("verdict") == "NEEDS_WORK"
            for review in negative_reviews
        )
    ):
        return "NEEDS_WORK"
    return "FAIL"


class FusionDriveEngine:
    def __init__(self, config: Mapping[str, Any] | None = None):
        self.config = dict(config or load_config())
        os.environ.setdefault("RELENTLESS_INCEPTION_HOME", str(runtime_dir() / "engine"))
        # The inherited runtime actually resolves DATA_DIR; keep its run store
        # co-located with this plugin's configured control-plane home.
        os.environ["RELENTLESS_INCEPTION_DATA_DIR"] = str(runtime_dir() / "engine")

    def _orchestrator(self, profile_name: str | None = None) -> tuple[FusionOrchestrator, str]:
        legacy, translated_profile = translate_config(self.config, profile_name=profile_name)
        registry = HybridProviderRegistry(legacy, self.config)
        return FusionOrchestrator(legacy, registry=registry), translated_profile

    def _seat_names_for_role(self, profile_name: str, role: str) -> list[str]:
        profile = self.config.get("profiles", {}).get(profile_name)
        if not isinstance(profile, Mapping):
            raise ConfigurationError(f"Unknown Fusion Drive profile: {profile_name}")
        engine_name = str(profile["engine"])
        engine = self.config["engines"][engine_name]

        # A server-managed Fusion endpoint is itself the fuser. Independent
        # panel/judge nodes use its explicitly configured client fallback so a
        # graph never pretends one server-managed call is several seats.
        role_engine = engine
        if engine.get("kind") == "server_managed" and role != "fuser":
            fallback_name = engine.get("fallback_engine")
            fallback = self.config.get("engines", {}).get(fallback_name)
            if not isinstance(fallback, Mapping):
                raise ConfigurationError(
                    f"Engine {engine_name!r} has no client fallback for {role!r} nodes"
                )
            role_engine = fallback

        if role == "panel":
            names = [
                *role_engine.get("panel", []),
                *role_engine.get("optional_panel", []),
            ]
        elif role == "judge":
            names = [role_engine.get("judge")]
        elif role == "fuser":
            names = [
                engine.get("seat")
                if engine.get("kind") == "server_managed"
                else role_engine.get("fuser")
            ]
        elif role == "verifier":
            gate_set = self.config["gate_sets"][profile["gate_set"]]
            names = list(gate_set.get("reviewers", []))
        else:
            raise ConfigurationError(
                "Seat role must be one of: panel, judge, fuser, verifier"
            )

        resolved = [str(name) for name in names if isinstance(name, str) and name]
        if not resolved:
            raise ConfigurationError(
                f"Profile {profile_name!r} has no configured {role!r} seats"
            )
        return resolved

    def seat_run(
        self,
        task: str,
        *,
        context: str = "",
        profile_name: str | None = None,
        role: str = "panel",
        seat_index: int = 0,
        cycle: bool = False,
        seat_name: str | None = None,
        resume_run_id: str | None = None,
        graph_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Run one role-bound external model seat for a workflow graph node."""

        selected_profile = profile_name or str(self.config["active_profile"])
        role_seats = self._seat_names_for_role(selected_profile, role)
        if seat_name is not None:
            if seat_name not in role_seats:
                raise ConfigurationError(
                    f"Seat {seat_name!r} is not a configured {role!r} seat for "
                    f"profile {selected_profile!r}; allowed={role_seats}"
                )
            selected_seat = seat_name
        else:
            if isinstance(seat_index, bool) or not isinstance(seat_index, int):
                raise ConfigurationError("seat_index must be an integer")
            if cycle:
                selected_seat = role_seats[seat_index % len(role_seats)]
            else:
                minimum = -len(role_seats)
                maximum = len(role_seats) - 1
                if seat_index < minimum or seat_index > maximum:
                    raise ConfigurationError(
                        f"seat_index {seat_index} is outside [{minimum}, {maximum}] "
                        f"for {role!r} seats"
                    )
                selected_seat = role_seats[seat_index]

        seat = self.config["seats"][selected_seat]
        provider = self.config["providers"][seat["provider"]]
        if provider.get("transport") == "codex_host":
            raise CapabilityError(
                "seat_run only dispatches external tool-free seats; native Claude "
                "workflow agents must use agent()"
            )
        if seat.get("enabled", True) is not True or provider.get("enabled", True) is not True:
            raise CapabilityError(f"Selected seat or provider is disabled: {selected_seat}")

        orchestrator, translated_profile = self._orchestrator(selected_profile)
        budget_stage = {
            "panel": "panel",
            "fuser": "synthesis",
            "judge": "gate",
            "verifier": "gate",
        }[role]
        result = orchestrator.run_seat(
            task,
            selected_seat,
            context=context,
            profile_name=translated_profile,
            run_id=resume_run_id,
            graph_run_id=graph_run_id,
            graph_profile_name=selected_profile,
            budget_stage=budget_stage,
        )
        return {
            **result,
            "profile": selected_profile,
            "engine": str(self.config["profiles"][selected_profile]["engine"]),
            "selection": {
                "role": role,
                "seat_index": seat_index,
                "cycled": cycle,
                "seat_name": selected_seat,
                "provider": seat["provider"],
                "transport": provider["transport"],
                "requested_model": seat["model"],
                "requested_reasoning": seat["reasoning"],
                "effective_reasoning": seat["effective_reasoning"],
            },
        }

    def fuse(
        self,
        task: str,
        *,
        context: str = "",
        mechanical_evidence: str = "",
        profile_name: str | None = None,
        resume_run_id: str | None = None,
    ) -> dict[str, Any]:
        if not task.strip():
            raise ConfigurationError("Fusion task cannot be empty")
        selected_profile = profile_name or str(self.config["active_profile"])
        orchestrator, translated_profile = self._orchestrator(selected_profile)
        result = orchestrator.fuse(
            task,
            context=context,
            mechanical_evidence=mechanical_evidence,
            profile_name=translated_profile,
            run_id=resume_run_id,
        ).to_dict()
        workflow_id = str(result["run_id"])
        gate = result.get("gate", {})
        gate_passed = bool(gate.get("passed") or gate.get("verdict") == "PASS" or gate.get("status") == "passed")
        synthesis_receipt = {
            "stage": "synthesis",
            "verdict": "PASS" if gate_passed else "NEEDS_WORK",
            "artifact_sha256": text_hash(str(result.get("synthesis", ""))),
            "engine_gate": json_copy(gate),
        }
        lifecycle = initialize_lifecycle(
            workflow_id,
            run_id=str(result["run_id"]),
            plan_sha256=text_hash(str(result["synthesis"])),
            config_sha256=canonical_hash(self.config),
            profile_name=selected_profile,
            engine_name=str(self.config["profiles"][selected_profile]["engine"]),
            host_goal_creation_tool=str(
                self.config["lifecycle"]["host_goal_creation_tool"]
            ),
            synthesis_gate_receipt=synthesis_receipt,
        )
        return {
            **result,
            "workflow_id": workflow_id,
            "profile": selected_profile,
            "engine": str(self.config["profiles"][selected_profile]["engine"]),
            "host_lifecycle": lifecycle,
            "workflow_report": workflow_report(self.config, profile_name=selected_profile),
            "next_action": (
                "Run the plan gate, then show the fused plan, Mermaid graph, and complete effective configuration. "
                "Do not execute until the user explicitly confirms the exact plan."
            ),
        }

    def _fuse_continue_validated(
        self,
        task: str,
        *,
        source_binding: Any = None,
        source_snapshot: Mapping[str, Any] | None = None,
        context: str = "",
        mechanical_evidence: str = "",
        profile_name: str | None = None,
        resume_run_id: str | None = None,
        source_integrity_check: Any = None,
    ) -> dict[str, Any]:
        """Continue a validated failed fusion as a distinct child run."""

        # Legacy prototype arguments remain only so callers receive a closed,
        # explicit error instead of accidentally dispatching from a snapshot
        # plus an arbitrary callable.
        del source_snapshot, source_integrity_check
        if not task.strip():
            raise ConfigurationError("Fusion continuation task cannot be empty")
        if not isinstance(resume_run_id, str) or not resume_run_id:
            raise ConfigurationError(
                "Fusion continuation requires its deterministic child run id"
            )
        selected_profile = profile_name or str(self.config["active_profile"])
        orchestrator, translated_profile = self._orchestrator(selected_profile)
        validated_snapshot = validated_execution_source_snapshot(
            source_binding,
            reverify=False,
            task=task,
            context=context,
            mechanical_evidence=mechanical_evidence,
            source_profile_name=selected_profile,
            current_schema_v2_sha256=canonical_hash(self.config),
            translated_profile_name=translated_profile,
            translated_engine_sha256=canonical_hash(orchestrator.config),
            child_run_id=resume_run_id,
        )
        result = orchestrator._continue_fuse_validated(
            task,
            source_binding=source_binding,
            context=context,
            mechanical_evidence=mechanical_evidence,
            source_profile_name=selected_profile,
            profile_name=translated_profile,
            run_id=resume_run_id,
        ).to_dict()
        continuation_child_tree_sha256 = hash_source_run_tree(
            Path(str(result["artifacts_dir"]))
        )
        workflow_id = str(result["run_id"])
        gate = result.get("gate", {})
        gate_passed = bool(
            gate.get("passed")
            or gate.get("verdict") == "PASS"
            or gate.get("status") == "passed"
        )
        synthesis_receipt = {
            "stage": "synthesis",
            "verdict": "PASS" if gate_passed else "NEEDS_WORK",
            "artifact_sha256": text_hash(str(result.get("synthesis", ""))),
            "engine_gate": json_copy(gate),
            "continuation": json_copy(validated_snapshot.get("lineage", {})),
        }
        # The orchestrator rechecks before committing its child artifacts. The
        # engine owns the host lifecycle, so recheck once more immediately
        # before that separate commit boundary.
        validated_snapshot = validated_execution_source_snapshot(
            source_binding,
            reverify=True,
            task=task,
            context=context,
            mechanical_evidence=mechanical_evidence,
            source_profile_name=selected_profile,
            current_schema_v2_sha256=canonical_hash(self.config),
            translated_profile_name=translated_profile,
            translated_engine_sha256=canonical_hash(orchestrator.config),
            child_run_id=resume_run_id,
        )
        lifecycle = None
        lifecycle_status = "not_created_gate_rejected"
        lifecycle_reconciliation = None
        if result.get("status") == "completed" and gate_passed:
            try:
                lifecycle = initialize_lifecycle(
                    workflow_id,
                    run_id=str(result["run_id"]),
                    plan_sha256=text_hash(str(result["synthesis"])),
                    config_sha256=canonical_hash(self.config),
                    profile_name=selected_profile,
                    engine_name=str(
                        self.config["profiles"][selected_profile]["engine"]
                    ),
                    host_goal_creation_tool=str(
                        self.config["lifecycle"]["host_goal_creation_tool"]
                    ),
                    synthesis_gate_receipt=synthesis_receipt,
                )
                lifecycle = validate_initialized_lifecycle(
                    lifecycle,
                    workflow_id=workflow_id,
                    run_id=str(result["run_id"]),
                    plan_sha256=text_hash(str(result["synthesis"])),
                    config_sha256=canonical_hash(self.config),
                    profile_name=selected_profile,
                    engine_name=str(
                        self.config["profiles"][selected_profile]["engine"]
                    ),
                    host_goal_creation_tool=str(
                        self.config["lifecycle"]["host_goal_creation_tool"]
                    ),
                    synthesis_gate_receipt=synthesis_receipt,
                )
                lifecycle_status = "created"
            except (
                ConfigurationError,
                LifecycleError,
                LockTimeout,
                OSError,
            ) as exc:
                # Provider work and its gated child result are already durable.
                # Preserve them as a completed job and require the dedicated,
                # provider-free reconciliation path to create the lifecycle.
                lifecycle_status = "reconciliation_required"
                lifecycle_reconciliation = {
                    "required": True,
                    "error_type": type(exc).__name__,
                    "provider_work_complete": True,
                }
        return {
            **result,
            "workflow_id": workflow_id,
            "profile": selected_profile,
            "engine": str(
                self.config["profiles"][selected_profile]["engine"]
            ),
            "continuation_lineage": json_copy(
                validated_snapshot.get("lineage", {})
            ),
            "continuation_child_tree_sha256": (
                continuation_child_tree_sha256
            ),
            "host_lifecycle": lifecycle,
            "lifecycle_status": lifecycle_status,
            "lifecycle_reconciliation": lifecycle_reconciliation,
            "workflow_report": workflow_report(
                self.config, profile_name=selected_profile
            ),
            "next_action": (
                "Run the plan gate, then show the fused plan, Mermaid graph, and complete effective configuration. "
                "Do not execute until the user explicitly confirms the exact plan."
                if lifecycle is not None
                else (
                    "Provider work completed, but host lifecycle creation requires provider-free reconciliation."
                    if lifecycle_status == "reconciliation_required"
                    else "The continuation synthesis gate rejected the child. No host lifecycle or execution authority was created."
                )
            ),
        }

    def approval_gate(
        self,
        task: str,
        artifact: str,
        *,
        stage: str,
        mechanical_evidence: str = "",
        profile_name: str | None = None,
        workflow_id: str | None = None,
        expected_lifecycle_sha256: str | None = None,
        resume_run_id: str | None = None,
    ) -> dict[str, Any]:
        selected_profile = profile_name or str(self.config["active_profile"])
        orchestrator, translated_profile = self._orchestrator(selected_profile)
        gate_run = orchestrator.adversarial_gate(
            task,
            artifact,
            mechanical_evidence=mechanical_evidence,
            profile_name=translated_profile,
            run_id=resume_run_id,
        )
        # adversarial_gate returns {"run_id", "artifacts_dir", "gate": {...}, "ledger"};
        # the pass/fail data lives one level down on the inner gate dict.
        inner_gate = gate_run.get("gate")
        gate = inner_gate if isinstance(inner_gate, Mapping) else gate_run
        verdict = _gate_verdict(gate)
        output: dict[str, Any] = {
            "stage": stage,
            "verdict": verdict,
            "gate": gate_run,
            "artifact_sha256": text_hash(artifact),
            "profile": selected_profile,
            "engine": str(self.config["profiles"][selected_profile]["engine"]),
        }
        if workflow_id:
            if not expected_lifecycle_sha256:
                raise ConfigurationError("expected_lifecycle_sha256 is required when recording a workflow gate")
            gate_set = self.config["gate_sets"][
                self.config["profiles"][selected_profile]["gate_set"]
            ]
            reviewer_models = [
                self.config["seats"][seat_name]["model"] for seat_name in gate_set["reviewers"]
            ]
            output["host_lifecycle"] = record_gate(
                workflow_id,
                stage=stage,
                verdict=verdict,
                artifact_sha256=text_hash(artifact),
                evidence=[mechanical_evidence] if mechanical_evidence else [],
                reviewer_models=reviewer_models,
                expected_lifecycle_sha256=expected_lifecycle_sha256,
            )
        return output

    def batch_fuse(
        self,
        tasks: list[Mapping[str, Any]],
        *,
        profile_name: str | None = None,
        confirmed_external_costs: bool,
        max_concurrency: int | None = None,
    ) -> dict[str, Any]:
        if not confirmed_external_costs:
            raise ExternalActionRequired("Batch fusion can incur multiple provider charges and requires confirmation")
        selected_profile = profile_name or str(self.config["active_profile"])
        engine_name = self.config["profiles"][selected_profile]["engine"]
        configured = int(self.config["engines"][engine_name].get("max_concurrency", 1))
        concurrency = min(max_concurrency or configured, configured)

        def worker(item: Mapping[str, Any]) -> dict[str, Any]:
            return self.fuse(
                str(item["task"]),
                context=str(item.get("context", "")),
                mechanical_evidence=str(item.get("mechanical_evidence", "")),
                profile_name=selected_profile,
            )

        results = execute_microbatch(tasks, worker, max_concurrency=concurrency)
        return {
            "profile": selected_profile,
            "engine": engine_name,
            "batch_mode": "bounded_microbatch",
            "max_concurrency": concurrency,
            "results": results,
            "cost_statement": "Concurrency is not represented as a provider discount; each fusion run retains its own ledger.",
        }
