"""Manifest-driven simulated-user testing campaigns."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import load_config, runtime_dir
from .errors import ConfigurationError
from .util import (
    atomic_write_json,
    canonical_hash,
    exclusive_lock,
    json_copy,
    now_utc,
    read_json,
    validate_identifier,
)


QUESTIONNAIRE = [
    {"key": "platform_runtime", "question": "Which platforms, browsers, devices, and runtime versions must pass?"},
    {"key": "ui_ux", "question": "What UI/UX outcomes, reference states, and unacceptable visual regressions matter?"},
    {"key": "viewports", "question": "Which desktop, tablet, mobile, zoom, and orientation viewports should be exercised?"},
    {"key": "personas", "question": "Which user personas, expertise levels, locales, and assistive technologies should be simulated?"},
    {"key": "accessibility", "question": "Which accessibility standard, keyboard flow, focus, contrast, and screen-reader expectations apply?"},
    {"key": "logs", "question": "Which console, network, server, and telemetry warnings are forbidden or expected?"},
    {"key": "performance", "question": "What latency, responsiveness, memory, CPU, bundle, and throughput budgets are pass/fail?"},
    {"key": "privacy_security", "question": "Which privacy, authentication, authorization, injection, and data-egress scenarios must be tested?"},
    {"key": "data_integrity", "question": "Which empty, malformed, boundary, concurrent, offline, and partial-failure data states matter?"},
    {"key": "external_writes", "question": "May the campaign create accounts, send messages, modify remote data, or incur charges?"},
]


def human_sim_questions() -> dict[str, Any]:
    return {
        "questions": json_copy(QUESTIONNAIRE),
        "goal_note": (
            "An extra Codex goal loop is optional and may be created only after explicit confirmation. "
            "The loop is manifest-driven and bounded, never an unbounded shell process."
        ),
    }


def _campaign_dir(campaign_id: str) -> Path:
    return runtime_dir() / "human-sim-users" / validate_identifier(campaign_id, "campaign_id")


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    value = json_copy(dict(manifest))
    value.pop("manifest_sha256", None)
    return canonical_hash(value)


def _load(campaign_id: str) -> dict[str, Any]:
    path = _campaign_dir(campaign_id) / "manifest.json"
    if not path.exists():
        raise ConfigurationError(f"Unknown human-sim campaign: {campaign_id}")
    value = read_json(path)
    if value.get("manifest_sha256") != _manifest_hash(value):
        raise ConfigurationError("Human-sim manifest hash mismatch")
    return value


def _save(manifest: dict[str, Any]) -> dict[str, Any]:
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    atomic_write_json(_campaign_dir(str(manifest["campaign_id"])) / "manifest.json", manifest)
    return json_copy(manifest)


def create_campaign(
    *,
    preferences: Mapping[str, Any],
    acceptance_criteria: Sequence[str],
    scenarios: Sequence[Mapping[str, Any]],
    request_extra_goal: bool = False,
    confirmed_extra_goal: bool = False,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = dict(config or load_config())
    missing = [item["key"] for item in QUESTIONNAIRE if item["key"] not in preferences]
    if missing:
        return {
            "created": False,
            "missing_preferences": missing,
            "questions": [item for item in QUESTIONNAIRE if item["key"] in missing],
        }
    if not acceptance_criteria:
        raise ConfigurationError("Human-sim campaign requires acceptance criteria")
    if request_extra_goal and not confirmed_extra_goal:
        raise ConfigurationError("An extra continuous goal requires explicit confirmed_extra_goal=true")
    normalized_scenarios = []
    for index, scenario in enumerate(scenarios):
        scenario_id = str(scenario.get("scenario_id") or f"scenario-{index:03d}")
        validate_identifier(scenario_id, "scenario_id")
        objective = str(scenario.get("objective", "")).strip()
        if not objective:
            raise ConfigurationError(f"Scenario {scenario_id} has no objective")
        normalized_scenarios.append(
            {
                "scenario_id": scenario_id,
                "objective": objective,
                "persona": str(scenario.get("persona", "default")),
                "viewport": str(scenario.get("viewport", "configured")),
                "status": "pending",
                "iterations": [],
            }
        )
    immutable = {
        "preferences": json_copy(preferences),
        "acceptance_criteria": list(acceptance_criteria),
        "scenarios": [
            {key: item[key] for key in ("scenario_id", "objective", "persona", "viewport")}
            for item in normalized_scenarios
        ],
        "request_extra_goal": request_extra_goal,
    }
    campaign_id = canonical_hash(immutable)[:24]
    directory = _campaign_dir(campaign_id)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "manifest.json").exists():
        return _load(campaign_id)
    manifest = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "status": "active",
        "created_at": now_utc(),
        "updated_at": now_utc(),
        "preferences": json_copy(preferences),
        "acceptance_criteria": list(acceptance_criteria),
        "scenarios": normalized_scenarios,
        "open_errors": [],
        "performance_pass": False,
        "all_criteria_evidenced": False,
        "stalled_subagents": [],
        "extra_goal": {
            "requested": request_extra_goal,
            "confirmed": confirmed_extra_goal,
            "host_action_required": request_extra_goal,
            "host_tool": (
                config["lifecycle"]["host_goal_creation_tool"]
                if request_extra_goal
                else None
            ),
            "recorded_goal_thread_id": None,
        },
        "loop": {
            "kind": "manifest_driven",
            "handoff_after_same_failure": config["human_sim_users"]["handoff_after_same_failure"],
            "stop_when": json_copy(config["human_sim_users"]["stop_when"]),
        },
    }
    return _save(manifest)


def record_campaign_iteration(
    campaign_id: str,
    *,
    scenario_id: str,
    passed: bool,
    evidence: Sequence[str],
    errors: Sequence[Mapping[str, Any]] = (),
    performance_pass: bool | None = None,
    criteria_evidenced: bool | None = None,
    stalled_subagents: Sequence[str] = (),
    expected_manifest_sha256: str,
) -> dict[str, Any]:
    directory = _campaign_dir(campaign_id)
    with exclusive_lock(directory / ".campaign.lock"):
        manifest = _load(campaign_id)
        if manifest["manifest_sha256"] != expected_manifest_sha256:
            raise ConfigurationError("Stale human-sim manifest; reload before recording")
        if manifest["status"] not in {"active", "human_handoff"}:
            raise ConfigurationError(f"Human-sim campaign is {manifest['status']}")
        scenario = next((item for item in manifest["scenarios"] if item["scenario_id"] == scenario_id), None)
        if not scenario:
            raise ConfigurationError(f"Unknown human-sim scenario: {scenario_id}")
        normalized_errors = []
        for error in errors:
            message = str(error.get("message", "")).strip()
            fingerprint = str(error.get("fingerprint") or canonical_hash({"message": message})[:16])
            normalized_errors.append(
                {
                    "fingerprint": fingerprint,
                    "message": message,
                    "severity": str(error.get("severity", "error")),
                    "source": str(error.get("source", "unknown")),
                }
            )
        iteration = {
            "index": len(scenario["iterations"]),
            "passed": bool(passed),
            "evidence": sorted(set(str(item) for item in evidence)),
            "errors": normalized_errors,
            "recorded_at": now_utc(),
        }
        iteration["iteration_sha256"] = canonical_hash(iteration)
        scenario["iterations"].append(iteration)
        scenario["status"] = "passed" if passed and not normalized_errors else "failed"
        manifest["open_errors"] = [
            error
            for item in manifest["scenarios"]
            if item["iterations"]
            for error in item["iterations"][-1]["errors"]
        ]
        if performance_pass is not None:
            manifest["performance_pass"] = bool(performance_pass)
        if criteria_evidenced is not None:
            # Refuse a manifest that claims full evidence next to unfinished
            # scenarios; the pair reads as a stop condition to anyone skimming
            # the report, even though `complete` is computed independently.
            pending = [
                str(item["scenario_id"])
                for item in manifest["scenarios"]
                if item["status"] != "passed"
            ]
            if criteria_evidenced and pending:
                raise ConfigurationError(
                    "Cannot record all_criteria_evidenced=true while these scenarios have not "
                    "passed: " + ", ".join(sorted(pending))
                )
            manifest["all_criteria_evidenced"] = bool(criteria_evidenced)
        manifest["stalled_subagents"] = sorted(set(str(item) for item in stalled_subagents))
        manifest["updated_at"] = iteration["recorded_at"]

        fingerprints = [
            error["fingerprint"]
            for item in manifest["scenarios"]
            for iteration_item in item["iterations"]
            for error in iteration_item["errors"]
        ]
        repeated = Counter(fingerprints).most_common(1)
        if repeated and repeated[0][1] >= int(manifest["loop"]["handoff_after_same_failure"]):
            manifest["status"] = "human_handoff"
            manifest["handoff"] = {
                "reason": "same_failure_fingerprint",
                "fingerprint": repeated[0][0],
                "count": repeated[0][1],
            }
        complete = (
            not manifest["open_errors"]
            and manifest["all_criteria_evidenced"]
            and not manifest["stalled_subagents"]
            and manifest["performance_pass"]
            and all(item["status"] == "passed" for item in manifest["scenarios"])
        )
        if complete:
            manifest["status"] = "complete"
        return _save(manifest)


def record_campaign_goal(
    campaign_id: str,
    *,
    goal_thread_id: str,
    expected_manifest_sha256: str,
) -> dict[str, Any]:
    directory = _campaign_dir(campaign_id)
    with exclusive_lock(directory / ".campaign.lock"):
        manifest = _load(campaign_id)
        if manifest["manifest_sha256"] != expected_manifest_sha256:
            raise ConfigurationError("Stale human-sim manifest; reload before recording goal")
        goal = manifest["extra_goal"]
        if not goal["requested"] or not goal["confirmed"]:
            raise ConfigurationError("Campaign has no explicitly confirmed extra goal")
        if goal["recorded_goal_thread_id"] and goal["recorded_goal_thread_id"] != goal_thread_id:
            raise ConfigurationError("A different goal is already recorded")
        goal["recorded_goal_thread_id"] = goal_thread_id
        goal["host_action_required"] = False
        manifest["updated_at"] = now_utc()
        return _save(manifest)


def campaign_status(campaign_id: str) -> dict[str, Any]:
    manifest = _load(campaign_id)
    pending = [
        item["scenario_id"] for item in manifest["scenarios"] if item["status"] != "passed"
    ]
    return {
        "campaign_id": campaign_id,
        "status": manifest["status"],
        "manifest_sha256": manifest["manifest_sha256"],
        "pending_scenarios": pending,
        "open_errors": json_copy(manifest["open_errors"]),
        "performance_pass": manifest["performance_pass"],
        "all_criteria_evidenced": manifest["all_criteria_evidenced"],
        "stalled_subagents": json_copy(manifest["stalled_subagents"]),
        "extra_goal": json_copy(manifest["extra_goal"]),
        "next_action": (
            "Stop: all completion conditions are evidenced."
            if manifest["status"] == "complete"
            else "Hand off the repeated failure with preserved evidence."
            if manifest["status"] == "human_handoff"
            else "Run the next pending scenario and append its evidence to this manifest."
        ),
    }


def campaign_plan(campaign_id: str) -> dict[str, Any]:
    """Return the persisted campaign plan without iteration bodies."""

    manifest = _load(campaign_id)
    scenarios = []
    for scenario in manifest["scenarios"]:
        summary = {
            key: json_copy(value)
            for key, value in scenario.items()
            if key != "iterations"
        }
        summary["iteration_count"] = len(scenario["iterations"])
        scenarios.append(summary)
    return {
        "campaign_id": campaign_id,
        "status": manifest["status"],
        "manifest_sha256": manifest["manifest_sha256"],
        "loop": json_copy(manifest.get("loop", {})),
        "preferences": json_copy(manifest.get("preferences", {})),
        "scenarios": scenarios,
    }


def _transition_campaign(
    campaign_id: str,
    *,
    expected_manifest_sha256: str,
    allowed_from: set[str],
    new_status: str,
    reason: str = "",
) -> dict[str, Any]:
    directory = _campaign_dir(campaign_id)
    with exclusive_lock(directory / ".campaign.lock"):
        manifest = _load(campaign_id)
        if manifest["manifest_sha256"] != expected_manifest_sha256:
            raise ConfigurationError("Stale human-sim manifest; reload before changing state")
        if manifest["status"] not in allowed_from:
            raise ConfigurationError(
                f"Cannot move campaign from {manifest['status']!r} to {new_status!r}"
            )
        event = {
            "from": manifest["status"],
            "to": new_status,
            "reason": reason,
            "recorded_at": now_utc(),
        }
        manifest.setdefault("lifecycle_events", []).append(event)
        manifest["status"] = new_status
        manifest["updated_at"] = event["recorded_at"]
        return _save(manifest)


def pause_campaign(campaign_id: str, *, expected_manifest_sha256: str, reason: str = "") -> dict[str, Any]:
    return _transition_campaign(
        campaign_id,
        expected_manifest_sha256=expected_manifest_sha256,
        allowed_from={"active"},
        new_status="paused",
        reason=reason,
    )


def resume_campaign(campaign_id: str, *, expected_manifest_sha256: str, reason: str = "") -> dict[str, Any]:
    return _transition_campaign(
        campaign_id,
        expected_manifest_sha256=expected_manifest_sha256,
        allowed_from={"paused"},
        new_status="active",
        reason=reason,
    )


def abort_campaign(campaign_id: str, *, expected_manifest_sha256: str, reason: str) -> dict[str, Any]:
    if not str(reason).strip():
        raise ConfigurationError("Aborting a campaign requires an explicit reason")
    return _transition_campaign(
        campaign_id,
        expected_manifest_sha256=expected_manifest_sha256,
        allowed_from={"active", "paused", "human_handoff"},
        new_status="aborted",
        reason=str(reason),
    )


def campaign_report(campaign_id: str) -> dict[str, Any]:
    """Summarize campaign evidence: per-scenario outcomes, errors, and stalls."""

    manifest = _load(campaign_id)
    scenario_rows = []
    total_iterations = 0
    for scenario in manifest["scenarios"]:
        iterations = scenario["iterations"]
        total_iterations += len(iterations)
        last = iterations[-1] if iterations else None
        scenario_rows.append(
            {
                "scenario_id": scenario["scenario_id"],
                "status": scenario["status"],
                "iterations": len(iterations),
                "last_passed": bool(last["passed"]) if last else None,
                "last_recorded_at": last["recorded_at"] if last else None,
                "open_error_count": len(last["errors"]) if last else 0,
                "evidence_count": len(last["evidence"]) if last else 0,
            }
        )
    return {
        "campaign_id": campaign_id,
        "status": manifest["status"],
        "manifest_sha256": manifest["manifest_sha256"],
        "total_iterations": total_iterations,
        "scenarios": scenario_rows,
        "open_errors": json_copy(manifest["open_errors"]),
        "all_criteria_evidenced": manifest["all_criteria_evidenced"],
        "performance_pass": manifest["performance_pass"],
        "stalled_subagents": json_copy(manifest["stalled_subagents"]),
        "lifecycle_events": json_copy(manifest.get("lifecycle_events", [])),
        "handoff": json_copy(manifest.get("handoff")) if manifest.get("handoff") else None,
    }
