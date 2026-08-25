"""Receipt-backed host lifecycle with compare-and-swap transitions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import runtime_dir
from .errors import ConfigurationError, LifecycleError
from .util import (
    atomic_write_json,
    canonical_hash,
    exclusive_lock,
    json_copy,
    now_utc,
    read_json,
    validate_identifier,
)


MAIN_GATE_TRANSITIONS = {
    ("awaiting_plan_gate", "plan"): "awaiting_user_confirmation",
    ("awaiting_pre_execution_gate", "pre_execution"): "ready_for_execution",
    ("awaiting_post_execution", "post_execution"): "awaiting_final",
    ("awaiting_final", "final"): "awaiting_summary",
    ("awaiting_summary", "summarize"): "complete",
}
NON_TRANSITION_GATES = {"synthesis", "subagent_pre_execution", "subagent_post_execution"}
VERDICTS = {"PASS", "NEEDS_WORK", "FAIL"}
TERMINAL_STATES = {"complete", "aborted"}
# Deliberately a module constant, not configuration: putting it in the config
# would change canonical_hash(config), which fuse() stamps as config_sha256, and
# every pre-upgrade workflow would then fail to resume on immutable-input drift.
WORKFLOW_EXPIRY_SECONDS = 7 * 24 * 3600


def workflow_dir(workflow_id: str) -> Path:
    return runtime_dir() / "workflows" / validate_identifier(workflow_id, "workflow_id")


def lifecycle_path(workflow_id: str) -> Path:
    return workflow_dir(workflow_id) / "host-lifecycle.json"


def _prepare_workflow_directory(workflow_id: str) -> Path:
    """Create one real workflow directory without following a symlink target."""

    root = runtime_dir() / "workflows"
    if root.is_symlink():
        raise LifecycleError("Workflow root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise LifecycleError("Workflow root must be a real directory")
    directory = workflow_dir(workflow_id)
    if directory.is_symlink():
        raise LifecycleError("Workflow directory must not be a symlink")
    if directory.exists() and not directory.is_dir():
        raise LifecycleError("Workflow path must be a directory")
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise LifecycleError("Workflow directory must remain a real directory")
    return directory


def _existing_workflow_directory(workflow_id: str) -> Path:
    directory = workflow_dir(workflow_id)
    if directory.is_symlink() or not directory.is_dir():
        raise LifecycleError(f"Unknown or unsafe workflow_id: {workflow_id}")
    return directory


def _without_hash(state: Mapping[str, Any]) -> dict[str, Any]:
    value = json_copy(dict(state))
    value.pop("lifecycle_sha256", None)
    return value


def _state_hash(state: Mapping[str, Any]) -> str:
    return canonical_hash(_without_hash(state))


def _validated_state(state: Mapping[str, Any]) -> dict[str, Any]:
    if (
        type(state.get("schema_version")) is not int
        or state.get("schema_version") != 1
    ):
        raise LifecycleError("Unsupported host lifecycle schema")
    expected = _state_hash(state)
    if state.get("lifecycle_sha256") != expected:
        raise LifecycleError("Host lifecycle hash mismatch")
    if not isinstance(state.get("events"), list):
        raise LifecycleError("Host lifecycle events must be a list")
    previous: str | None = None
    for index, event in enumerate(state["events"]):
        if not isinstance(event, Mapping):
            raise LifecycleError(f"Lifecycle event {index} is not an object")
        event_payload = dict(event)
        event_hash = event_payload.pop("event_sha256", None)
        if event_payload.get("previous_event_sha256") != previous:
            raise LifecycleError(f"Lifecycle event {index} breaks the hash chain")
        if event_hash != canonical_hash(event_payload):
            raise LifecycleError(f"Lifecycle event {index} hash mismatch")
        previous = event_hash
    if state.get("last_event_sha256") != previous:
        raise LifecycleError("Host lifecycle last_event_sha256 mismatch")
    return json_copy(state)


def load_lifecycle(workflow_id: str) -> dict[str, Any]:
    directory = _existing_workflow_directory(workflow_id)
    path = directory / "host-lifecycle.json"
    if path.is_symlink() or not path.is_file():
        raise LifecycleError(f"Unknown workflow_id: {workflow_id}")
    return _validated_state(read_json(path))


def _append_event(state: dict[str, Any], event_type: str, payload: Mapping[str, Any]) -> None:
    event_payload = {
        "sequence": len(state["events"]),
        "event_type": event_type,
        "created_at": now_utc(),
        "previous_event_sha256": state.get("last_event_sha256"),
        "payload": json_copy(payload),
    }
    event = {**event_payload, "event_sha256": canonical_hash(event_payload)}
    state["events"].append(event)
    state["last_event_sha256"] = event["event_sha256"]
    state["updated_at"] = event["created_at"]


def _save_state(state: dict[str, Any]) -> dict[str, Any]:
    state["lifecycle_sha256"] = _state_hash(state)
    path = lifecycle_path(str(state["workflow_id"]))
    atomic_write_json(path, state)
    return _validated_state(state)


def initialize_lifecycle(
    workflow_id: str,
    *,
    run_id: str,
    plan_sha256: str,
    config_sha256: str,
    profile_name: str | None = None,
    engine_name: str | None = None,
    host_goal_creation_tool: str = "create_goal",
    synthesis_gate_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    validate_identifier(workflow_id, "workflow_id")
    validate_identifier(run_id, "run_id")
    if not isinstance(host_goal_creation_tool, str) or not host_goal_creation_tool:
        raise LifecycleError("host_goal_creation_tool must be a nonempty string")
    directory = _prepare_workflow_directory(workflow_id)
    lock_path = directory / ".lifecycle.lock"
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise LifecycleError("Workflow lifecycle lock path is unsafe")
    with exclusive_lock(lock_path):
        path = lifecycle_path(workflow_id)
        if path.is_symlink():
            raise LifecycleError("Host lifecycle path must not be a symlink")
        if path.exists():
            existing = load_lifecycle(workflow_id)
            immutable = {
                "run_id": run_id,
                "plan_sha256": plan_sha256,
                "config_sha256": config_sha256,
            }
            if any(existing.get(key) != value for key, value in immutable.items()):
                raise LifecycleError("workflow_id already exists with different immutable inputs")
            return existing
        created_at = now_utc()
        state: dict[str, Any] = {
            "schema_version": 1,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "plan_sha256": plan_sha256,
            "config_sha256": config_sha256,
            "profile_name": profile_name,
            "engine_name": engine_name,
            "host_goal_creation_tool": host_goal_creation_tool,
            "state": "awaiting_plan_gate",
            "created_at": created_at,
            "updated_at": created_at,
            "gates": {},
            "confirmation": None,
            "claude_goal": None,
            "execution": None,
            "events": [],
            "last_event_sha256": None,
            "confirmation_limit": "A host receipt records an explicit confirmation event but cannot cryptographically prove human identity.",
        }
        if synthesis_gate_receipt:
            state["gates"]["synthesis"] = json_copy(synthesis_gate_receipt)
        _append_event(
            state,
            "initialized",
            {
                "run_id": run_id,
                "plan_sha256": plan_sha256,
                "config_sha256": config_sha256,
                "profile_name": profile_name,
                "engine_name": engine_name,
                "host_goal_creation_tool": host_goal_creation_tool,
                "synthesis_gate_recorded": bool(synthesis_gate_receipt),
            },
        )
        return _save_state(state)


def validate_initialized_lifecycle(
    lifecycle: Mapping[str, Any],
    *,
    workflow_id: str,
    run_id: str,
    plan_sha256: str,
    config_sha256: str,
    profile_name: str,
    engine_name: str,
    host_goal_creation_tool: str,
    synthesis_gate_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Require the exact new lifecycle identity used by a continuation."""

    state = _validated_state(lifecycle)
    expected_fields = {
        "workflow_id": workflow_id,
        "run_id": run_id,
        "plan_sha256": plan_sha256,
        "config_sha256": config_sha256,
        "profile_name": profile_name,
        "engine_name": engine_name,
        "host_goal_creation_tool": host_goal_creation_tool,
        "state": "awaiting_plan_gate",
    }
    if any(state.get(key) != value for key, value in expected_fields.items()):
        raise LifecycleError(
            "Initialized continuation lifecycle has conflicting immutable identity"
        )
    gates = state.get("gates")
    if (
        not isinstance(gates, Mapping)
        or dict(gates) != {"synthesis": json_copy(synthesis_gate_receipt)}
    ):
        raise LifecycleError(
            "Initialized continuation lifecycle has a different synthesis receipt"
        )
    expected_keys = {
        "schema_version",
        "workflow_id",
        "run_id",
        "plan_sha256",
        "config_sha256",
        "profile_name",
        "engine_name",
        "host_goal_creation_tool",
        "state",
        "created_at",
        "updated_at",
        "gates",
        "confirmation",
        "claude_goal",
        "execution",
        "events",
        "last_event_sha256",
        "confirmation_limit",
        "lifecycle_sha256",
    }
    events = state.get("events")
    if set(state) != expected_keys or not isinstance(events, list) or len(events) != 1:
        raise LifecycleError(
            "Initialized continuation lifecycle is not a fresh lifecycle receipt"
        )
    initialized_event = events[0]
    expected_event_payload = {
        "run_id": run_id,
        "plan_sha256": plan_sha256,
        "config_sha256": config_sha256,
        "profile_name": profile_name,
        "engine_name": engine_name,
        "host_goal_creation_tool": host_goal_creation_tool,
        "synthesis_gate_recorded": True,
    }
    if (
        type(state.get("schema_version")) is not int
        or state.get("schema_version") != 1
        or state.get("confirmation") is not None
        or state.get("claude_goal") is not None
        or state.get("execution") is not None
        or state.get("confirmation_limit")
        != "A host receipt records an explicit confirmation event but cannot cryptographically prove human identity."
        or not isinstance(initialized_event, Mapping)
        or initialized_event.get("sequence") != 0
        or initialized_event.get("event_type") != "initialized"
        or initialized_event.get("previous_event_sha256") is not None
        or initialized_event.get("payload") != expected_event_payload
        or initialized_event.get("created_at") != state.get("updated_at")
        or initialized_event.get("event_sha256")
        != state.get("last_event_sha256")
    ):
        raise LifecycleError(
            "Initialized continuation lifecycle contains unexpected state"
        )
    return state


def initialized_lifecycle_receipt(
    lifecycle: Mapping[str, Any],
) -> dict[str, Any]:
    """Recover the immutable creation receipt from a valid live descendant."""

    state = _validated_state(lifecycle)
    events = state.get("events")
    gates = state.get("gates")
    if (
        not isinstance(events, list)
        or not events
        or not isinstance(events[0], Mapping)
        or not isinstance(gates, Mapping)
        or not isinstance(gates.get("synthesis"), Mapping)
    ):
        raise LifecycleError(
            "Continuation lifecycle has no recoverable initialization receipt"
        )
    initialized_event = json_copy(events[0])
    initial = {
        "schema_version": 1,
        "workflow_id": state.get("workflow_id"),
        "run_id": state.get("run_id"),
        "plan_sha256": state.get("plan_sha256"),
        "config_sha256": state.get("config_sha256"),
        "profile_name": state.get("profile_name"),
        "engine_name": state.get("engine_name"),
        "host_goal_creation_tool": state.get("host_goal_creation_tool"),
        "state": "awaiting_plan_gate",
        "created_at": state.get("created_at"),
        "updated_at": initialized_event.get("created_at"),
        "gates": {"synthesis": json_copy(gates["synthesis"])},
        "confirmation": None,
        "claude_goal": None,
        "execution": None,
        "events": [initialized_event],
        "last_event_sha256": initialized_event.get("event_sha256"),
        "confirmation_limit": state.get("confirmation_limit"),
    }
    initial["lifecycle_sha256"] = _state_hash(initial)
    required_strings = (
        "workflow_id",
        "run_id",
        "plan_sha256",
        "config_sha256",
        "profile_name",
        "engine_name",
        "host_goal_creation_tool",
    )
    if any(not isinstance(initial.get(key), str) for key in required_strings):
        raise LifecycleError(
            "Continuation lifecycle initialization identity is invalid"
        )
    return validate_initialized_lifecycle(
        initial,
        workflow_id=str(initial["workflow_id"]),
        run_id=str(initial["run_id"]),
        plan_sha256=str(initial["plan_sha256"]),
        config_sha256=str(initial["config_sha256"]),
        profile_name=str(initial["profile_name"]),
        engine_name=str(initial["engine_name"]),
        host_goal_creation_tool=str(initial["host_goal_creation_tool"]),
        synthesis_gate_receipt=initial["gates"]["synthesis"],
    )


def validate_lifecycle_descendant(
    lifecycle: Mapping[str, Any],
    initialized_lifecycle: Mapping[str, Any],
) -> dict[str, Any]:
    """Require a live lifecycle to retain an exact initialization prefix."""

    state = _validated_state(lifecycle)
    initial = initialized_lifecycle_receipt(initialized_lifecycle)
    recovered_initial = initialized_lifecycle_receipt(state)
    if recovered_initial != initial:
        raise LifecycleError(
            "Live continuation lifecycle does not descend from its creation receipt"
        )
    initial_events = initial["events"]
    live_events = state.get("events")
    if (
        not isinstance(live_events, list)
        or live_events[: len(initial_events)] != initial_events
    ):
        raise LifecycleError(
            "Live continuation lifecycle lost its initialization event prefix"
        )
    return state


def _mutate(
    workflow_id: str,
    expected_lifecycle_sha256: str,
    mutator: Any,
) -> dict[str, Any]:
    directory = _existing_workflow_directory(workflow_id)
    lock_path = directory / ".lifecycle.lock"
    if lock_path.is_symlink() or not lock_path.is_file():
        raise LifecycleError("Workflow lifecycle lock path is unsafe")
    with exclusive_lock(lock_path):
        state = load_lifecycle(workflow_id)
        if state["lifecycle_sha256"] != expected_lifecycle_sha256:
            raise LifecycleError(
                "Stale lifecycle receipt; reload the workflow and retry with its current lifecycle_sha256"
            )
        mutator(state)
        return _save_state(state)


def record_gate(
    workflow_id: str,
    *,
    stage: str,
    verdict: str,
    artifact_sha256: str,
    evidence: list[str],
    reviewer_models: list[str],
    expected_lifecycle_sha256: str,
) -> dict[str, Any]:
    verdict = verdict.upper()
    if verdict not in VERDICTS:
        raise LifecycleError(f"Unsupported gate verdict: {verdict}")
    if stage not in {item[1] for item in MAIN_GATE_TRANSITIONS} | NON_TRANSITION_GATES:
        raise LifecycleError(f"Unknown gate stage: {stage}")

    def apply(state: dict[str, Any]) -> None:
        if stage == "synthesis" and "synthesis" in state.get("gates", {}):
            raise LifecycleError(
                "The lifecycle synthesis receipt is immutable after initialization"
            )
        receipt = {
            "stage": stage,
            "verdict": verdict,
            "artifact_sha256": artifact_sha256,
            "evidence": sorted(set(evidence)),
            "reviewer_models": list(reviewer_models),
            "recorded_at": now_utc(),
        }
        state["gates"][stage] = receipt
        prior_state = state["state"]
        # Without this, aborted is not actually terminal: the NON_TRANSITION_GATES
        # branch below sets transition = prior_state, which skips the rejection
        # that would otherwise catch an illegal stage.
        if prior_state == "aborted":
            raise LifecycleError(f"Gate {stage} cannot be recorded while workflow is aborted")
        transition = MAIN_GATE_TRANSITIONS.get((prior_state, stage))
        if stage in NON_TRANSITION_GATES:
            transition = prior_state
        elif transition is None:
            raise LifecycleError(f"Gate {stage} cannot be recorded while workflow is {prior_state}")
        if verdict == "PASS":
            state["state"] = transition
        _append_event(
            state,
            "gate_recorded",
            {"stage": stage, "verdict": verdict, "state_before": prior_state, "state_after": state["state"]},
        )

    return _mutate(workflow_id, expected_lifecycle_sha256, apply)


def confirm_plan(
    workflow_id: str,
    *,
    confirmed: bool,
    user_message_sha256: str,
    expected_plan_sha256: str,
    expected_lifecycle_sha256: str,
) -> dict[str, Any]:
    if not confirmed:
        raise LifecycleError("Plan confirmation requires confirmed=true")

    def apply(state: dict[str, Any]) -> None:
        if state["state"] != "awaiting_user_confirmation":
            raise LifecycleError(f"Plan cannot be confirmed while workflow is {state['state']}")
        if state["plan_sha256"] != expected_plan_sha256:
            raise LifecycleError("Confirmed plan hash does not match the fused plan")
        state["confirmation"] = {
            "confirmed": True,
            "user_message_sha256": user_message_sha256,
            "plan_sha256": expected_plan_sha256,
            "recorded_at": now_utc(),
            "proof_scope": "host_event_receipt_only",
        }
        state["state"] = "awaiting_claude_goal"
        _append_event(state, "plan_confirmed", state["confirmation"])

    return _mutate(workflow_id, expected_lifecycle_sha256, apply)


def record_claude_goal(
    workflow_id: str,
    *,
    goal_thread_id: str,
    objective_sha256: str,
    host_tool: str,
    expected_lifecycle_sha256: str,
) -> dict[str, Any]:
    def apply(state: dict[str, Any]) -> None:
        if state["state"] != "awaiting_claude_goal":
            raise LifecycleError(f"Codex goal cannot be recorded while workflow is {state['state']}")
        expected_host_tool = str(state.get("host_goal_creation_tool") or "create_goal")
        if host_tool != expected_host_tool:
            raise LifecycleError(
                f"Goal receipt must identify the configured Codex host tool {expected_host_tool}"
            )
        state["claude_goal"] = {
            "thread_id": goal_thread_id,
            "objective_sha256": objective_sha256,
            "host_tool": host_tool,
            "recorded_at": now_utc(),
        }
        state["state"] = "awaiting_pre_execution_gate"
        _append_event(state, "claude_goal_recorded", state["claude_goal"])

    return _mutate(workflow_id, expected_lifecycle_sha256, apply)


def start_execution(
    workflow_id: str,
    *,
    execution_scope_sha256: str,
    expected_lifecycle_sha256: str,
) -> dict[str, Any]:
    def apply(state: dict[str, Any]) -> None:
        if state["state"] != "ready_for_execution":
            raise LifecycleError(f"Execution cannot start while workflow is {state['state']}")
        state["execution"] = {
            "scope_sha256": execution_scope_sha256,
            "started_at": now_utc(),
            "finished_at": None,
            "result_sha256": None,
        }
        state["state"] = "executing"
        _append_event(state, "execution_started", {"scope_sha256": execution_scope_sha256})

    return _mutate(workflow_id, expected_lifecycle_sha256, apply)


def finish_execution(
    workflow_id: str,
    *,
    result_sha256: str,
    expected_lifecycle_sha256: str,
) -> dict[str, Any]:
    def apply(state: dict[str, Any]) -> None:
        if state["state"] != "executing":
            raise LifecycleError(f"Execution cannot finish while workflow is {state['state']}")
        if not isinstance(state.get("execution"), dict):
            raise LifecycleError("Execution receipt is missing")
        state["execution"]["finished_at"] = now_utc()
        state["execution"]["result_sha256"] = result_sha256
        state["state"] = "awaiting_post_execution"
        _append_event(state, "execution_finished", {"result_sha256": result_sha256})

    return _mutate(workflow_id, expected_lifecycle_sha256, apply)


def abort_workflow(
    workflow_id: str,
    *,
    reason: str,
    expected_lifecycle_sha256: str,
) -> dict[str, Any]:
    """Terminally abort an abandoned workflow, preserving its whole hash chain.

    Abort is a decision, not the passage of time, so it is a real compare-and-swap
    transition with an appended event, while staleness stays a read-time view.
    Nothing is removed: the workflow directory and every prior event stay exactly
    as they were, and the aborted receipt is readable forever.
    """

    if not str(reason).strip():
        raise LifecycleError("Aborting a workflow requires an explicit reason")

    def apply(state: dict[str, Any]) -> None:
        prior_state = state["state"]
        if prior_state in TERMINAL_STATES:
            raise LifecycleError(f"Workflow is already {prior_state} and cannot be aborted")
        state["abort"] = {
            "reason": str(reason).strip(),
            "state_before": prior_state,
            "recorded_at": now_utc(),
        }
        state["state"] = "aborted"
        _append_event(state, "workflow_aborted", state["abort"])

    return _mutate(workflow_id, expected_lifecycle_sha256, apply)


def _age_seconds(updated_at: Any) -> float | None:
    """Seconds since `updated_at`, or None when the timestamp is unusable.

    Returns None rather than raising: this feeds a pure read that must keep
    serving files it served before. A trailing `Z` is normalized because older
    interpreters reject it, which would otherwise make the staleness flag depend
    on the Python version. A naive timestamp is read as UTC, and the result is
    clamped at zero so a backwards clock reports age 0 rather than a negative.
    """

    if not isinstance(updated_at, str):
        return None
    if updated_at.endswith("Z"):
        updated_at = updated_at[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(updated_at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())


def _staleness(state: Mapping[str, Any]) -> dict[str, Any]:
    age = _age_seconds(state.get("updated_at"))
    terminal = state["state"] in TERMINAL_STATES
    return {
        "updated_at": state.get("updated_at"),
        "age_seconds": None if age is None else int(age),
        "expiry_seconds": WORKFLOW_EXPIRY_SECONDS,
        # A terminal workflow is never stale: nothing is waiting on it.
        "stale": None if age is None else (not terminal and age > WORKFLOW_EXPIRY_SECONDS),
    }


def list_workflows(limit: int = 50) -> dict[str, Any]:
    """List known workflows oldest-updated first so stale ones surface.

    Unreadable or hash-invalid files are skipped rather than raising: this is a
    discovery read whose whole purpose is finding workflows that went wrong.
    """

    directory = runtime_dir() / "workflows"
    entries: list[dict[str, Any]] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*/host-lifecycle.json")):
            try:
                state = _validated_state(read_json(path))
            except (ConfigurationError, LifecycleError, OSError, ValueError):
                continue
            staleness = _staleness(state)
            entries.append(
                {
                    "workflow_id": state["workflow_id"],
                    "state": state["state"],
                    "updated_at": staleness["updated_at"],
                    "age_seconds": staleness["age_seconds"],
                    "stale": staleness["stale"],
                }
            )
    entries.sort(key=lambda entry: (str(entry.get("updated_at") or ""), entry["workflow_id"]))
    return {
        "count": len(entries),
        "expiry_seconds": WORKFLOW_EXPIRY_SECONDS,
        "workflows": entries[: max(1, int(limit))],
    }


def lifecycle_summary(workflow_id: str) -> dict[str, Any]:
    state = load_lifecycle(workflow_id)
    host_goal_creation_tool = str(
        state.get("host_goal_creation_tool") or "create_goal"
    )
    return {
        "workflow_id": workflow_id,
        "state": state["state"],
        "run_id": state["run_id"],
        "profile_name": state.get("profile_name"),
        "engine_name": state.get("engine_name"),
        "host_goal_creation_tool": host_goal_creation_tool,
        "plan_sha256": state["plan_sha256"],
        "lifecycle_sha256": state["lifecycle_sha256"],
        "gates": json_copy(state["gates"]),
        "confirmation": json_copy(state["confirmation"]),
        "claude_goal": json_copy(state["claude_goal"]),
        "abort": json_copy(state.get("abort")),
        "staleness": _staleness(state),
        "next_action": {
            "awaiting_plan_gate": "Run and record the plan approval gate.",
            "awaiting_user_confirmation": "Show the plan, Mermaid graph, and full configuration; wait for explicit user confirmation.",
            "awaiting_claude_goal": (
                "After the user requests execution, call the Codex host "
                f"{host_goal_creation_tool} tool and record its receipt."
            ),
            "awaiting_pre_execution_gate": "Run and record the pre_execution Grok approval gate.",
            "ready_for_execution": "Record the execution scope and start host-owned execution.",
            "executing": "Complete the scoped implementation and record its result hash.",
            "awaiting_post_execution": "Run and record the post_execution gate.",
            "awaiting_final": "Run and record the final gate.",
            "awaiting_summary": "Run and record the summarize gate.",
            "complete": "Workflow is complete.",
            "aborted": "Workflow was aborted; no further transitions are legal.",
        }.get(state["state"], "Inspect lifecycle state."),
        "confirmation_limit": state["confirmation_limit"],
    }
