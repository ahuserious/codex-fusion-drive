"""Bounded rescue packets and resumable evidence checkpoints."""

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


def _rescue_dir(packet_id: str) -> Path:
    return runtime_dir() / "rescue" / validate_identifier(packet_id, "packet_id")


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    value = json_copy(dict(manifest))
    value.pop("manifest_sha256", None)
    return canonical_hash(value)


def _load(packet_id: str) -> dict[str, Any]:
    path = _rescue_dir(packet_id) / "manifest.json"
    if not path.exists():
        raise ConfigurationError(f"Unknown rescue packet: {packet_id}")
    manifest = read_json(path)
    if manifest.get("manifest_sha256") != _manifest_hash(manifest):
        raise ConfigurationError("Rescue manifest hash mismatch")
    return manifest


def _save(manifest: dict[str, Any]) -> dict[str, Any]:
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    atomic_write_json(_rescue_dir(str(manifest["packet_id"])) / "manifest.json", manifest)
    return json_copy(manifest)


def create_rescue_packet(
    *,
    problem: str,
    acceptance_criteria: Sequence[str],
    work_units: Sequence[Mapping[str, Any]],
    constraints: Sequence[str] = (),
    evidence_bar: Sequence[str] = (),
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = dict(config or load_config())
    if not problem.strip():
        raise ConfigurationError("Rescue problem cannot be empty")
    if not acceptance_criteria:
        raise ConfigurationError("Rescue packet requires acceptance criteria")
    normalized_units = []
    for index, unit in enumerate(work_units):
        unit_id = str(unit.get("unit_id") or f"unit-{index:03d}")
        validate_identifier(unit_id, "work unit id")
        objective = str(unit.get("objective", "")).strip()
        if not objective:
            raise ConfigurationError(f"Rescue work unit {unit_id} has no objective")
        normalized_units.append(
            {
                "unit_id": unit_id,
                "objective": objective,
                "dependencies": sorted(set(str(item) for item in unit.get("dependencies", []))),
                "status": "pending",
                "attempts": [],
                "last_proven_checkpoint": None,
            }
        )
    immutable = {
        "problem": problem,
        "acceptance_criteria": list(acceptance_criteria),
        "constraints": list(constraints),
        "evidence_bar": list(evidence_bar),
        "work_units": [
            {
                "unit_id": unit["unit_id"],
                "objective": unit["objective"],
                "dependencies": unit["dependencies"],
            }
            for unit in normalized_units
        ],
    }
    packet_id = canonical_hash(immutable)[:24]
    directory = _rescue_dir(packet_id)
    directory.mkdir(parents=True, exist_ok=True)
    immutable_path = directory / "problem-packet.json"
    immutable_packet = {
        "schema_version": 1,
        "packet_id": packet_id,
        "packet_sha256": canonical_hash(immutable),
        **immutable,
    }
    if immutable_path.exists() and read_json(immutable_path) != immutable_packet:
        raise ConfigurationError("Immutable rescue packet collision")
    if not immutable_path.exists():
        atomic_write_json(immutable_path, immutable_packet)
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        return _load(packet_id)
    created_at = now_utc()
    manifest = {
        "schema_version": 1,
        "packet_id": packet_id,
        "packet_sha256": immutable_packet["packet_sha256"],
        "status": "active",
        "created_at": created_at,
        "updated_at": created_at,
        "total_cycles": 0,
        "work_units": normalized_units,
        "strategy": {
            "fresh_context_diagnosis": True,
            "cross_perspective_critique": True,
            "bounded_retries": True,
            "preserve_failed_attempts": True,
            "resume_from_last_proven_checkpoint": True,
        },
        "limits": {
            "max_attempts_per_unit": config["rescue"]["max_attempts_per_unit"],
            "same_failure_handoff_after": config["rescue"]["same_failure_fingerprint_handoff_after"],
            "max_total_cycles": config["rescue"]["max_total_cycles"],
        },
    }
    return _save(manifest)


def record_rescue_attempt(
    packet_id: str,
    *,
    unit_id: str,
    outcome: str,
    evidence: Sequence[str] = (),
    failure_fingerprint: str | None = None,
    diagnosis: str = "",
    checkpoint: Mapping[str, Any] | None = None,
    expected_manifest_sha256: str,
) -> dict[str, Any]:
    if outcome not in {"passed", "failed", "blocked"}:
        raise ConfigurationError(f"Unsupported rescue outcome: {outcome}")
    directory = _rescue_dir(packet_id)
    with exclusive_lock(directory / ".rescue.lock"):
        manifest = _load(packet_id)
        if manifest["manifest_sha256"] != expected_manifest_sha256:
            raise ConfigurationError("Stale rescue manifest; reload before recording an attempt")
        if manifest["status"] != "active":
            raise ConfigurationError(f"Rescue packet is {manifest['status']}")
        unit = next((item for item in manifest["work_units"] if item["unit_id"] == unit_id), None)
        if not unit:
            raise ConfigurationError(f"Unknown rescue work unit: {unit_id}")
        limits = manifest["limits"]
        if len(unit["attempts"]) >= int(limits["max_attempts_per_unit"]):
            raise ConfigurationError(f"Work unit {unit_id} exhausted its bounded attempts")
        attempt = {
            "attempt_index": len(unit["attempts"]),
            "outcome": outcome,
            "evidence": sorted(set(str(item) for item in evidence)),
            "failure_fingerprint": failure_fingerprint,
            "diagnosis": diagnosis,
            "checkpoint": json_copy(checkpoint) if checkpoint else None,
            "recorded_at": now_utc(),
        }
        attempt["attempt_sha256"] = canonical_hash(attempt)
        unit["attempts"].append(attempt)
        manifest["total_cycles"] += 1
        manifest["updated_at"] = attempt["recorded_at"]
        if outcome == "passed":
            unit["status"] = "passed"
            unit["last_proven_checkpoint"] = json_copy(checkpoint) if checkpoint else {
                "evidence": attempt["evidence"],
                "attempt_sha256": attempt["attempt_sha256"],
            }
        elif outcome == "blocked":
            unit["status"] = "blocked"
        else:
            unit["status"] = "retryable"

        fingerprints = [
            item["failure_fingerprint"]
            for work_unit in manifest["work_units"]
            for item in work_unit["attempts"]
            if item.get("failure_fingerprint")
        ]
        repeated = Counter(fingerprints).most_common(1)
        same_failure_stalled = bool(
            repeated and repeated[0][1] >= int(limits["same_failure_handoff_after"])
        )
        attempts_exhausted = len(unit["attempts"]) >= int(limits["max_attempts_per_unit"]) and outcome != "passed"
        total_exhausted = manifest["total_cycles"] >= int(limits["max_total_cycles"])
        if same_failure_stalled or attempts_exhausted or total_exhausted:
            manifest["status"] = "human_handoff"
            manifest["handoff"] = {
                "reason": (
                    "same_failure_fingerprint"
                    if same_failure_stalled
                    else "unit_attempts_exhausted"
                    if attempts_exhausted
                    else "total_cycles_exhausted"
                ),
                "fingerprint": repeated[0][0] if repeated else failure_fingerprint,
                "preserved_attempts": sum(len(item["attempts"]) for item in manifest["work_units"]),
                "created_at": now_utc(),
            }
        elif all(item["status"] == "passed" for item in manifest["work_units"]):
            manifest["status"] = "complete"
        return _save(manifest)


def resume_rescue(packet_id: str) -> dict[str, Any]:
    manifest = _load(packet_id)
    pending = [
        {
            "unit_id": item["unit_id"],
            "objective": item["objective"],
            "status": item["status"],
            "attempts": len(item["attempts"]),
            "resume_checkpoint": json_copy(item["last_proven_checkpoint"]),
        }
        for item in manifest["work_units"]
        if item["status"] != "passed"
    ]
    return {
        "packet_id": packet_id,
        "status": manifest["status"],
        "manifest_sha256": manifest["manifest_sha256"],
        "pending_units": pending,
        "next_strategy": (
            "Use a fresh-context diagnosis, compare it with the preserved failed attempts, "
            "then perform one bounded attempt against the explicit evidence bar."
        ),
        "human_handoff": json_copy(manifest.get("handoff")),
    }
