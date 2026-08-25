"""Read-only validation for immutable 0.2.1 panel continuations.

This module deliberately supports one recovery boundary: a terminal 0.2.1
``fuse`` run whose complete panel is valid and whose only later provider
response is a Grok judge rejected as ``null_or_empty_structured_output``.
It never dispatches a provider and never writes a source artifact.
"""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import math
import os
import random
import re
import stat
import string
from pathlib import Path
from typing import Any, Mapping, Sequence

from relentless_inception.config import active_profile, runtime_data_dir
from relentless_inception.errors import ConfigError as LegacyConfigError
from relentless_inception.errors import ProviderError
from relentless_inception.orchestrator import (
    _invocation_payload,
    _judge_contract,
    _panel_context_bundle,
    _validate_quality_floor,
    _validated_persisted_call_response,
)
from relentless_inception.prompts import judge_prompt, judge_system, panel_prompt, panel_system
from relentless_inception.state import (
    BudgetTracker,
    RunStore,
    attempt_receipt_id,
    call_receipt_entry_id,
    canonical_json_hash,
    text_hash,
)

from .config import runtime_dir
from .errors import ConfigurationError
from .util import canonical_hash, read_only_existing_lock

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - this recovery path is POSIX-only
    _fcntl = None  # type: ignore[assignment]


SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
SOURCE_PLUGIN_VERSION = "0.2.1"
_SOURCE_BINDING_SEAL = object()
LEGACY_CACHE_ERROR = (
    "Provider returned invalid cached token usage: cannot exceed input tokens"
)
MODEL_RESPONSE_FIELDS = {
    "text",
    "provider",
    "requested_model",
    "actual_model",
    "usage",
    "latency_seconds",
    "request_id",
    "route",
    "raw_status",
}
USAGE_FIELDS = {
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "tool_calls",
    "cost_usd",
    "unknown_cost_fail_closed",
    "input_output_usage_complete",
    "raw_usage_invalid",
    "accounting_error",
}
ATTEMPT_FIELDS = {
    "attempt_index",
    "attempt_id",
    "stage",
    "seat",
    "invocation_sha256",
}
LEDGER_ENTRY_FIELDS = {
    *ATTEMPT_FIELDS,
    "entry_id",
    "response_sha256",
    "response_artifact",
    "provider",
    "requested_model",
    "actual_model",
    "request_id",
    "route",
    "raw_status",
    "latency_seconds",
    "usage",
}
LEDGER_FIELDS = {
    "schema_version",
    "calls",
    "attempts",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "total_tokens",
    "tool_calls",
    "known_cost_usd",
    "provider_cost_usd",
    "unknown_cost_calls",
    "accounting_failure",
    "stop_reason",
    "wall_seconds",
    "attempt_entries",
    "entries",
    "warnings",
}


def validate_sha256(value: Any, field_name: str) -> str:
    """Return one lowercase SHA-256 digest or fail closed."""

    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ConfigurationError(
            f"{field_name} must be a lowercase SHA-256 digest"
        )
    return value


def validate_sha256_mapping(value: Any, field_name: str) -> dict[str, str]:
    """Validate and detach a string-keyed SHA-256 mapping."""

    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{field_name} must be an object")
    normalized: dict[str, str] = {}
    for key, digest in value.items():
        if not isinstance(key, str) or not key:
            raise ConfigurationError(f"{field_name} keys must be nonempty strings")
        normalized[key] = validate_sha256(digest, f"{field_name}.{key}")
    return normalized


def _detached_json(value: Any, field_name: str) -> Any:
    try:
        return json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{field_name} must be JSON-safe") from exc


def _regular_file_bytes(path: Path, label: str) -> tuple[bytes, int]:
    if path.is_symlink():
        raise ConfigurationError(f"{label} must not be a symlink")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ConfigurationError(f"Unable to read {label}: {path}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ConfigurationError(f"{label} must be a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), stat.S_IMODE(file_stat.st_mode)
    finally:
        os.close(descriptor)


def _regular_json_object(path: Path, label: str) -> dict[str, Any]:
    payload, _mode = _regular_file_bytes(path, label)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} must be a JSON object")
    return value


def _source_tree_inventory(directory: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    if directory.is_symlink() or not directory.is_dir():
        raise ConfigurationError("Fusion continuation source run must be a real directory")
    records: list[dict[str, Any]] = []
    file_hashes: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        relative_name = path.relative_to(directory).as_posix()
        if path.is_symlink():
            raise ConfigurationError(
                f"Fusion continuation source contains a symlink: {relative_name}"
            )
        if path.is_dir():
            if relative_name != "responses":
                raise ConfigurationError(
                    "Fusion continuation source contains an unexpected directory: "
                    + relative_name
                )
            continue
        if not path.is_file():
            raise ConfigurationError(
                f"Fusion continuation source contains a non-regular entry: {relative_name}"
            )
        if relative_name == ".run.lock":
            continue
        payload, mode = _regular_file_bytes(path, f"source artifact {relative_name}")
        digest = hashlib.sha256(payload).hexdigest()
        records.append({"path": relative_name, "mode": mode, "sha256": digest})
        file_hashes[relative_name] = digest
    return records, file_hashes


def hash_source_run_tree(directory: Path) -> str:
    """Hash sorted ``{path, mode, sha256}`` records, excluding only ``.run.lock``."""

    records, _file_hashes = _source_tree_inventory(directory)
    return canonical_hash(records)


class _ReadOnlySourceStore:
    """Lease and read an existing source run without creating or chmodding it."""

    def __init__(
        self,
        *,
        directory: Path,
        run_id: str,
        task_hash: str,
        config_hash: str,
        input_hash: str,
    ) -> None:
        self.directory = directory
        self.run_id = run_id
        self.task_hash = task_hash
        self.config_hash = config_hash
        self.input_hash = input_hash
        self._lease_descriptor: int | None = None

    def __enter__(self) -> "_ReadOnlySourceStore":
        if _fcntl is None:
            raise ConfigurationError(
                "Fusion continuation source inspection requires POSIX file locking"
            )
        lease_path = self.directory / ".run.lock"
        if lease_path.is_symlink() or not lease_path.is_file():
            raise ConfigurationError(
                "Fusion continuation source lease is missing or symlinked"
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(lease_path, flags)
            lease_stat = os.fstat(descriptor)
            if not stat.S_ISREG(lease_stat.st_mode):
                raise ConfigurationError(
                    "Fusion continuation source lease must be a regular file"
                )
            _fcntl.flock(descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as exc:
            if "descriptor" in locals():
                os.close(descriptor)
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise ConfigurationError(
                    "Fusion continuation source run is already active"
                ) from exc
            raise ConfigurationError(
                "Unable to acquire the read-only source run lease"
            ) from exc
        except BaseException:
            if "descriptor" in locals():
                os.close(descriptor)
            raise
        self._lease_descriptor = descriptor
        return self

    def __exit__(self, exception_type: Any, exception: Any, traceback: Any) -> None:
        del exception_type, exception, traceback
        descriptor = self._lease_descriptor
        self._lease_descriptor = None
        if descriptor is None:
            return
        try:
            if _fcntl is not None:
                _fcntl.flock(descriptor, _fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def path(self, relative_name: str) -> Path:
        relative_path = Path(relative_name)
        if (
            not relative_name
            or relative_path.is_absolute()
            or any(part in {"", ".", ".."} for part in relative_path.parts)
        ):
            raise ConfigurationError("Source artifact path is not a safe relative path")
        return self.directory / relative_path

    def exists(self, relative_name: str) -> bool:
        artifact_path = self.path(relative_name)
        return not artifact_path.is_symlink() and artifact_path.is_file()

    def read_json(self, relative_name: str) -> dict[str, Any]:
        artifact_path = self.path(relative_name)
        return _regular_json_object(
            artifact_path,
            f"source artifact {relative_name}",
        )


def _expected_child_run_id(source_run_id: str) -> str:
    source_claim_key = "source-" + canonical_hash(
        {"schema_version": 1, "source_job_id": source_run_id}
    )[:48]
    return "job-" + canonical_hash(
        {
            "operation": "fuse_continue",
            "idempotency_key": source_claim_key,
        }
    )[:24]


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ConfigurationError(
            f"{label} schema mismatch; missing={sorted(expected - set(value))}, "
            f"unexpected={sorted(set(value) - expected)}"
        )


def _nonnegative_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigurationError(f"{label} must be a nonnegative integer")
    return value


def _nonnegative_number(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigurationError(f"{label} must be a nonnegative finite number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ConfigurationError(f"{label} must be a nonnegative finite number")
    return result


def _profile_panel_seats(
    translated_config: Mapping[str, Any], profile: Mapping[str, Any]
) -> list[str]:
    fusion = profile.get("fusion")
    if not isinstance(fusion, Mapping) or fusion.get("engine") != "client_orchestrated":
        raise ConfigurationError("Only client-orchestrated source panels can continue")
    required = list(fusion.get("panel", []))
    optional = list(fusion.get("optional_panel", []))
    if any(not isinstance(name, str) or not name for name in [*required, *optional]):
        raise ConfigurationError("Source panel seat names are invalid")
    if len(set(required)) != len(required) or set(required) & set(optional):
        raise ConfigurationError("Source panel seat configuration is ambiguous")
    seats = list(required)
    configured_seats = translated_config.get("seats", {})
    providers = translated_config.get("providers", {})
    for name in optional:
        seat = configured_seats.get(name, {}) if isinstance(configured_seats, Mapping) else {}
        provider = providers.get(seat.get("provider"), {}) if isinstance(seat, Mapping) and isinstance(providers, Mapping) else {}
        if seat.get("enabled", True) is True and isinstance(provider, Mapping) and provider.get("enabled", True) is True:
            seats.append(name)
    maximum = int(fusion.get("max_panel_seats", len(seats)))
    seats = seats[:maximum]
    if len(seats) < int(fusion.get("min_live_seats", len(seats))):
        raise ConfigurationError("Source profile cannot satisfy its minimum live panel")
    return seats


def _panel_invocations(
    *,
    store: RunStore,
    task: str,
    context: str,
    mechanical_evidence: str,
    translated_config: Mapping[str, Any],
    profile: Mapping[str, Any],
    seat_names: Sequence[str],
) -> dict[str, dict[str, Any]]:
    fusion = profile["fusion"]
    objective = str(
        profile.get("objective", "Deliver the most correct, complete, and executable result.")
    )
    result: dict[str, dict[str, Any]] = {}
    for seat_name in seat_names:
        seat = translated_config.get("seats", {}).get(seat_name)
        if not isinstance(seat, Mapping):
            raise ConfigurationError(f"Unknown translated panel seat: {seat_name}")
        role = str(seat.get("role", "domain analyst"))
        system = panel_system(
            role,
            str(seat.get("persona", "Find the most important truth other reviewers may miss.")),
            objective,
        )
        prompt = panel_prompt(
            task,
            _panel_context_bundle(
                context,
                mechanical_evidence,
                str(seat.get("context_bundle", "full_task_and_evidence")),
                fusion.get("partition_context", True) is True,
            ),
        )
        result[seat_name] = _invocation_payload(
            store, "panel", seat_name, system, prompt, None, "structured_response"
        )
    return result


def _expected_panel_result_order(
    seat_names: Sequence[str], task_hash: str, profile: Mapping[str, Any]
) -> list[str]:
    ordered = list(seat_names)
    fusion = profile["fusion"]
    if fusion.get("randomize_panel_order", True):
        random.Random(task_hash).shuffle(ordered)
    return ordered


def _validate_panel(
    *,
    store: RunStore,
    panel: Mapping[str, Any],
    translated_config: Mapping[str, Any],
    profile: Mapping[str, Any],
    seat_names: Sequence[str],
    invocations: Mapping[str, Mapping[str, Any]],
    expected_hashes: Mapping[str, str],
) -> list[dict[str, Any]]:
    _require_exact_keys(
        panel,
        {"results", "attempts", "live_count", "failed_count", "degraded"},
        "Source panel artifact",
    )
    results = panel.get("results")
    attempts = panel.get("attempts")
    if not isinstance(results, list) or not isinstance(attempts, list):
        raise ConfigurationError("Source panel results and attempts must be arrays")
    if panel.get("live_count") != len(seat_names) or panel.get("failed_count") != 0 or panel.get("degraded") is not False:
        raise ConfigurationError("Source panel must contain an exact non-degraded completed panel")
    if len(results) != len(seat_names) or len(attempts) != len(seat_names):
        raise ConfigurationError("Source panel has an unexpected result or attempt count")
    ordered_seats = _expected_panel_result_order(seat_names, store.task_hash, profile)
    if [row.get("seat_name") if isinstance(row, Mapping) else None for row in results] != ordered_seats:
        raise ConfigurationError("Source panel completed seat order does not match the bound task")
    if {row.get("seat_name") for row in attempts if isinstance(row, Mapping)} != set(seat_names):
        raise ConfigurationError("Source panel attempts do not bind the exact configured seat set")

    quality_floor = profile["fusion"].get("quality_floor", {})
    normalized: list[dict[str, Any]] = []
    results_by_seat: dict[str, Mapping[str, Any]] = {}
    for index, row_value in enumerate(results):
        if not isinstance(row_value, Mapping):
            raise ConfigurationError("Source panel result must be an object")
        row = dict(row_value)
        _require_exact_keys(
            row,
            {"seat_name", "anonymous_label", "role", "status", "response", "response_evidence", "error"},
            "Source panel result",
        )
        seat_name = ordered_seats[index]
        seat = translated_config["seats"][seat_name]
        if row.get("seat_name") != seat_name or row.get("status") != "completed" or row.get("error") is not None:
            raise ConfigurationError(f"Source panel seat {seat_name!r} is not exactly completed")
        expected_label = f"Seat {string.ascii_uppercase[index]}" if profile["fusion"].get("anonymize_model_identity", True) else seat_name
        if row.get("anonymous_label") != expected_label or row.get("role") != str(seat.get("role", "domain analyst")):
            raise ConfigurationError(f"Source panel seat {seat_name!r} metadata mismatch")
        try:
            response = _validated_persisted_call_response(
                store,
                row.get("response"),
                row.get("response_evidence"),
                invocation=invocations[seat_name],
                label=f"continued panel result for {seat_name}",
            )
            if isinstance(quality_floor, Mapping):
                _validate_quality_floor(response["text"], quality_floor, f"Stored seat {seat_name}")
        except (LegacyConfigError, ProviderError) as exc:
            raise ConfigurationError(str(exc)) from exc
        evidence = row["response_evidence"]
        if evidence.get("response_sha256") != expected_hashes[seat_name]:
            raise ConfigurationError(f"Source panel response hash mismatch for {seat_name}")
        if response.get("raw_status") != "completed" or response.get("provider") != seat.get("provider") or response.get("requested_model") != seat.get("model") or response.get("actual_model") != seat.get("model"):
            raise ConfigurationError(f"Source panel response identity mismatch for {seat_name}")
        detached = _detached_json(row, f"source panel result {seat_name}")
        normalized.append(detached)
        results_by_seat[seat_name] = row

    for attempt_value in attempts:
        if not isinstance(attempt_value, Mapping):
            raise ConfigurationError("Source panel attempt must be an object")
        seat_name = attempt_value.get("seat_name")
        saved = results_by_seat.get(str(seat_name))
        if saved is None or attempt_value.get("status") != "completed" or attempt_value.get("error") is not None:
            raise ConfigurationError("Source panel attempt is not reusable")
        for field in ("seat_name", "role", "status", "response", "response_evidence", "error"):
            if canonical_json_hash(attempt_value.get(field)) != canonical_json_hash(saved.get(field)):
                raise ConfigurationError(f"Source panel attempt mismatch for {seat_name}")
        if attempt_value.get("anonymous_label") not in ("", saved.get("anonymous_label")):
            raise ConfigurationError(f"Source panel attempt label mismatch for {seat_name}")
    return normalized


def _read_response_artifact(store: RunStore, entry: Mapping[str, Any], label: str) -> dict[str, Any]:
    entry_id = validate_sha256(entry.get("entry_id"), f"{label}.entry_id")
    relative_name = entry.get("response_artifact")
    if relative_name != f"responses/{entry_id}.json" or not store.exists(str(relative_name)):
        raise ConfigurationError(f"{label} has no matching raw response artifact")
    artifact = store.read_json(str(relative_name))
    _require_exact_keys(artifact, {"schema_version", "invocation", "receipt", "response"}, f"{label} raw response")
    if artifact.get("schema_version") != 1:
        raise ConfigurationError(f"{label} raw response schema is unsupported")
    return artifact


def _validate_failed_judge(
    *,
    store: RunStore,
    task: str,
    mechanical_evidence: str,
    reports: Sequence[Mapping[str, Any]],
    translated_config: Mapping[str, Any],
    profile: Mapping[str, Any],
    ledger: Mapping[str, Any],
    expected_response_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    fusion = profile["fusion"]
    judge_name = str(fusion.get("judge", ""))
    judge_seat = translated_config.get("seats", {}).get(judge_name)
    if not judge_name or not isinstance(judge_seat, Mapping):
        raise ConfigurationError("Source judge configuration is invalid")
    judge_schema, _required_fields = _judge_contract(profile)
    objective = str(profile.get("objective", "Deliver the most correct, complete, and executable result."))
    invocation = _invocation_payload(
        store,
        "judge",
        judge_name,
        judge_system(objective, str(judge_seat.get("persona", "")), str(judge_seat.get("context_bundle", ""))),
        judge_prompt(task, reports, mechanical_evidence),
        judge_schema,
        "fusion_judgment",
    )
    invocation_sha256 = canonical_json_hash(invocation)
    entries = ledger["entries"]
    judge_entries = [entry for entry in entries if isinstance(entry, Mapping) and entry.get("stage") == "judge"]
    if len(judge_entries) != 1:
        raise ConfigurationError("Source run must contain exactly one failed judge response")
    entry = judge_entries[0]
    _require_exact_keys(entry, LEDGER_ENTRY_FIELDS, "Source failed judge ledger entry")
    if entry.get("seat") != judge_name or entry.get("raw_status") != "failed" or entry.get("invocation_sha256") != invocation_sha256:
        raise ConfigurationError("Source failed judge receipt is bound to a different invocation")
    artifact = _read_response_artifact(store, entry, "Source failed judge")
    if canonical_json_hash(artifact.get("invocation")) != canonical_json_hash(invocation):
        raise ConfigurationError("Source failed judge raw invocation mismatch")
    response = artifact.get("response")
    receipt = artifact.get("receipt")
    if not isinstance(response, Mapping) or not isinstance(receipt, Mapping):
        raise ConfigurationError("Source failed judge response or receipt is invalid")
    _require_exact_keys(response, MODEL_RESPONSE_FIELDS, "Source failed judge response")
    usage = response.get("usage")
    route = response.get("route")
    if not isinstance(usage, Mapping) or not isinstance(route, Mapping):
        raise ConfigurationError("Source failed judge usage and route must be objects")
    _require_exact_keys(usage, USAGE_FIELDS, "Source failed judge usage")
    for field in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens", "tool_calls"):
        _nonnegative_integer(usage.get(field), f"Source failed judge usage.{field}")
    if usage["reasoning_tokens"] > usage["output_tokens"] or usage["cached_tokens"] <= usage["input_tokens"]:
        raise ConfigurationError("Source failed judge does not have the known disjoint-cache shape")
    if response.get("text") != "" or response.get("raw_status") != "failed" or response.get("provider") != judge_seat.get("provider") or response.get("requested_model") != judge_seat.get("model") or response.get("actual_model") != judge_seat.get("model"):
        raise ConfigurationError("Source failed judge response identity mismatch")
    if response.get("request_id") is not None or usage.get("cost_usd") is not None or usage.get("raw_usage_invalid") is not False or usage.get("accounting_error") is not None or usage.get("input_output_usage_complete") is not True:
        raise ConfigurationError("Source failed judge raw usage shape is unsupported")
    semantic_failure = route.get("semantic_failure")
    if not isinstance(semantic_failure, Mapping) or semantic_failure.get("category") != "null_or_empty_structured_output" or semantic_failure.get("type") != "null" or semantic_failure.get("exit_status") != 0:
        raise ConfigurationError("Source failed judge is not the supported null structured-output failure")
    _nonnegative_integer(semantic_failure.get("length"), "Source failed judge semantic length")
    validate_sha256(semantic_failure.get("sha256"), "Source failed judge semantic sha256")
    if route.get("transport") != "grok_cli_oauth" or route.get("tools_disabled") is not True:
        raise ConfigurationError("Source failed judge is not the supported legacy Grok route")

    response_sha256 = canonical_json_hash(response)
    if response_sha256 != expected_response_sha256 or entry.get("response_sha256") != response_sha256:
        raise ConfigurationError("Source failed judge response hash mismatch")
    expected_attempt_id = attempt_receipt_id(invocation_sha256, int(entry.get("attempt_index", -1)))
    expected_entry_id = call_receipt_entry_id(expected_attempt_id, invocation_sha256, response_sha256)
    if receipt.get("schema_version") != 1 or receipt.get("attempt_id") != expected_attempt_id or receipt.get("entry_id") != expected_entry_id or receipt.get("invocation_sha256") != invocation_sha256 or receipt.get("response_sha256") != response_sha256 or entry.get("attempt_id") != expected_attempt_id or entry.get("entry_id") != expected_entry_id:
        raise ConfigurationError("Source failed judge receipt chain is invalid")

    expected_recorded_usage = dict(usage)
    expected_recorded_usage["cached_tokens"] = int(usage["input_tokens"])
    expected_recorded_usage["raw_usage_invalid"] = True
    expected_recorded_usage["accounting_error"] = LEGACY_CACHE_ERROR
    expected_entry = {
        "attempt_index": entry["attempt_index"],
        "attempt_id": expected_attempt_id,
        "entry_id": expected_entry_id,
        "invocation_sha256": invocation_sha256,
        "response_sha256": response_sha256,
        "response_artifact": f"responses/{expected_entry_id}.json",
        "stage": "judge",
        "seat": judge_name,
        "provider": response["provider"],
        "requested_model": response["requested_model"],
        "actual_model": response["actual_model"],
        "request_id": response["request_id"],
        "route": response["route"],
        "raw_status": response["raw_status"],
        "latency_seconds": response["latency_seconds"],
        "usage": expected_recorded_usage,
    }
    if canonical_json_hash(entry) != canonical_json_hash(expected_entry):
        raise ConfigurationError("Source failed judge ledger does not exactly match the legacy clamped response")
    return _detached_json(response, "source failed judge response"), {
        "seat_name": judge_name,
        "entry_id": expected_entry_id,
        "attempt_id": expected_attempt_id,
        "invocation_sha256": invocation_sha256,
        "response_sha256": response_sha256,
        "response_artifact": f"responses/{expected_entry_id}.json",
        "semantic_failure": _detached_json(semantic_failure, "judge semantic failure"),
    }


def _corrected_source_usage(
    *,
    ledger: Mapping[str, Any],
    responses_by_entry_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    totals = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0, "tool_calls": 0}
    known_cost = 0.0
    provider_cost: dict[str, float] = {}
    unknown_cost_calls = 0
    normalizations: list[dict[str, Any]] = []
    for entry in ledger["entries"]:
        response = responses_by_entry_id[str(entry["entry_id"])]
        usage = response["usage"]
        input_tokens = int(usage["input_tokens"])
        cached_tokens = int(usage["cached_tokens"])
        if response["route"].get("transport") == "grok_cli_oauth":
            input_tokens += cached_tokens
            normalizations.append(
                {
                    "entry_id": entry["entry_id"],
                    "seat_name": entry["seat"],
                    "rule": "legacy_grok_disjoint_cache_read_added_to_input",
                    "uncached_input_tokens": usage["input_tokens"],
                    "cache_read_tokens": cached_tokens,
                    "normalized_input_tokens": input_tokens,
                    "response_sha256": entry["response_sha256"],
                }
            )
        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += int(usage["output_tokens"])
        totals["reasoning_tokens"] += int(usage["reasoning_tokens"])
        totals["cached_tokens"] += cached_tokens
        totals["tool_calls"] += int(usage["tool_calls"])
        cost = usage.get("cost_usd")
        if cost is None:
            unknown_cost_calls += 1
        else:
            normalized_cost = _nonnegative_number(cost, "Source response cost")
            known_cost += normalized_cost
            provider = str(response["provider"])
            provider_cost[provider] = provider_cost.get(provider, 0.0) + normalized_cost
    return {
        "schema_version": 1,
        "calls": len(ledger["attempt_entries"]),
        "attempts": len(ledger["attempt_entries"]),
        **totals,
        "total_tokens": totals["input_tokens"] + totals["output_tokens"],
        "known_cost_usd": known_cost,
        "provider_cost_usd": provider_cost,
        "unknown_cost_calls": unknown_cost_calls,
        "wall_seconds": _nonnegative_number(ledger.get("wall_seconds"), "Source ledger wall_seconds"),
        "warnings": list(ledger.get("warnings", [])),
        "source_accounting_failure": ledger.get("accounting_failure"),
        "source_stop_reason": ledger.get("stop_reason"),
        "normalizations": normalizations,
    }


def _residual_budget(budgets: Mapping[str, Any], usage: Mapping[str, Any]) -> dict[str, Any]:
    residual = copy.deepcopy(dict(budgets))
    capacities = {
        "max_calls": "calls",
        "max_total_tokens": "total_tokens",
        "max_input_tokens": "input_tokens",
        "max_output_tokens": "output_tokens",
        "max_reasoning_tokens": "reasoning_tokens",
        "max_tool_calls": "tool_calls",
        "max_wall_seconds": "wall_seconds",
        "max_cost_usd": "known_cost_usd",
        "approval_threshold_usd": "known_cost_usd",
    }
    for budget_name, usage_name in capacities.items():
        limit = residual.get(budget_name)
        if limit is None:
            continue
        remaining = limit - usage[usage_name]
        if remaining < 0:
            raise ConfigurationError(f"Source usage already exceeds profile budget {budget_name}")
        residual[budget_name] = remaining
    provider_limits = residual.get("per_provider_max_cost_usd")
    if isinstance(provider_limits, Mapping):
        adjusted: dict[str, Any] = {}
        for provider, limit in provider_limits.items():
            remaining = limit - usage["provider_cost_usd"].get(provider, 0.0)
            if remaining < 0:
                raise ConfigurationError(f"Source usage already exceeds provider budget for {provider}")
            adjusted[str(provider)] = remaining
        residual["per_provider_max_cost_usd"] = adjusted
    return _detached_json(residual, "residual budget")


def _validate_live_source_job_documents(
    *,
    source_run_id: str,
    expected_manifest: Mapping[str, Any],
    expected_request: Mapping[str, Any],
) -> None:
    source_job_directory = runtime_dir() / "jobs" / source_run_id
    if source_job_directory.is_symlink() or not source_job_directory.is_dir():
        raise ConfigurationError(
            "Fusion continuation source job directory is missing or symlinked"
        )
    manifest_path = source_job_directory / "job.json"
    request_path = source_job_directory / "request.json"
    lock_path = source_job_directory / ".job.lock"
    required_names = {"job.json", "request.json", ".job.lock"}
    allowed_names = {*required_names, ".execution.lock"}
    observed_entries = list(source_job_directory.iterdir())
    observed_names = {path.name for path in observed_entries}
    if (
        not required_names.issubset(observed_names)
        or not observed_names.issubset(allowed_names)
        or any(path.is_symlink() or not path.is_file() for path in observed_entries)
    ):
        raise ConfigurationError(
            "Fusion continuation source job must contain its manifest, request, "
            "job lock, and at most the regular execution lock"
        )
    if any(path.is_symlink() or not path.is_file() for path in (manifest_path, request_path, lock_path)):
        raise ConfigurationError(
            "Fusion continuation source job artifacts are missing or symlinked"
        )
    with read_only_existing_lock(lock_path):
        live_manifest = _regular_json_object(
            manifest_path,
            "source job manifest",
        )
        live_request = _regular_json_object(
            request_path,
            "source job request",
        )
    if (
        canonical_hash(live_manifest) != canonical_hash(expected_manifest)
        or live_manifest != expected_manifest
        or canonical_hash(live_request) != canonical_hash(expected_request)
        or live_request != expected_request
    ):
        raise ConfigurationError(
            "Fusion continuation source job documents changed after binding"
        )


def inspect_source_run(
    *,
    source_run_id: str,
    task: str,
    context: str,
    mechanical_evidence: str,
    profile_name: str,
    translated_config: Mapping[str, Any],
    translated_profile_name: str,
    expected_panel_response_hashes: Mapping[str, Any],
    expected_source_tree_sha256: str,
    expected_source_engine_manifest_file_sha256: str,
    expected_source_ledger_file_sha256: str,
    expected_failed_judge_response_sha256: str,
    source_schema_v2_sha256: str,
    current_schema_v2_sha256: str | None,
    source_job_manifest: Mapping[str, Any],
    source_request: Mapping[str, Any],
) -> dict[str, Any]:
    """Inspect and bind the supported failed source without modifying it."""

    for value, label in (
        (expected_source_tree_sha256, "expected_source_tree_sha256"),
        (expected_source_engine_manifest_file_sha256, "expected_source_engine_manifest_file_sha256"),
        (expected_source_ledger_file_sha256, "expected_source_ledger_file_sha256"),
        (expected_failed_judge_response_sha256, "expected_failed_judge_response_sha256"),
        (source_schema_v2_sha256, "source_schema_v2_sha256"),
        (current_schema_v2_sha256, "current_schema_v2_sha256"),
    ):
        validate_sha256(value, label)
    expected_hashes = validate_sha256_mapping(expected_panel_response_hashes, "expected_panel_response_hashes")
    if not isinstance(source_run_id, str) or not source_run_id.replace("-", "").isalnum():
        raise ConfigurationError("source_run_id may contain only letters, digits, and hyphens")
    if not all(isinstance(value, str) for value in (task, context, mechanical_evidence, profile_name, translated_profile_name)) or not task.strip():
        raise ConfigurationError("Source request text and profiles are invalid")
    translated_config_copy = _detached_json(translated_config, "translated_config")
    job_manifest = _detached_json(source_job_manifest, "source_job_manifest")
    request = _detached_json(source_request, "source_request")
    if job_manifest.get("manifest_sha256") != canonical_hash({key: value for key, value in job_manifest.items() if key != "manifest_sha256"}):
        raise ConfigurationError("Source job manifest hash mismatch")
    if job_manifest.get("job_id") != source_run_id or job_manifest.get("run_id") != source_run_id or job_manifest.get("operation") != "fuse" or job_manifest.get("profile") != profile_name or job_manifest.get("status") != "failed" or job_manifest.get("worker_state") != "failed" or job_manifest.get("result_sha256") is not None:
        raise ConfigurationError("Fusion continuation source job must be a terminal failed fuse job")
    if job_manifest.get("plugin_version") != SOURCE_PLUGIN_VERSION:
        raise ConfigurationError("Fusion continuation supports only the audited 0.2.1 source shape")
    if job_manifest.get("config_sha256") != source_schema_v2_sha256 or job_manifest.get("request_sha256") != canonical_hash(request):
        raise ConfigurationError("Source job schema-v2 or request binding mismatch")
    if request.get("schema_version") != 1 or request.get("job_id") != source_run_id or request.get("operation") != "fuse" or request.get("profile") != profile_name or set(request.get("arguments", {})) != {"task", "context", "mechanical_evidence"} or request["arguments"] != {"task": task, "context": context, "mechanical_evidence": mechanical_evidence}:
        raise ConfigurationError("Source request identity mismatch")

    source_directory = runtime_data_dir() / "runs" / source_run_id
    if source_directory.is_symlink() or not source_directory.is_dir() or (source_directory / ".run.lock").is_symlink() or not (source_directory / ".run.lock").is_file():
        raise ConfigurationError("Fusion continuation source run or lease is missing or symlinked")
    profile = active_profile(translated_config_copy, translated_profile_name)
    translated_engine_sha256 = canonical_json_hash(translated_config_copy)
    input_identity = {"operation": "fuse", "task": task, "context": context, "mechanical_evidence": mechanical_evidence, "profile_name": translated_profile_name}
    _validate_live_source_job_documents(
        source_run_id=source_run_id,
        expected_manifest=job_manifest,
        expected_request=request,
    )
    try:
        with _ReadOnlySourceStore(
            directory=source_directory,
            run_id=source_run_id,
            task_hash=text_hash(task),
            config_hash=translated_engine_sha256,
            input_hash=canonical_json_hash(input_identity),
        ) as store:
            before_records, before_hashes = _source_tree_inventory(store.directory)
            before_tree_sha256 = canonical_hash(before_records)
            manifest = store.read_json("manifest.json")
            if manifest.get("run_id") != source_run_id or manifest.get("status") != "failed":
                raise ConfigurationError("Fusion continuation source run must be failed")
            if manifest.get("task_hash") != text_hash(task) or manifest.get("config_hash") != translated_engine_sha256 or manifest.get("input_hash") != canonical_json_hash(input_identity):
                raise ConfigurationError("Fusion continuation source run task/config/input hash mismatch")
            stages = manifest.get("stages")
            if not isinstance(stages, Mapping) or set(stages) != {"panel"} or not isinstance(stages.get("panel"), Mapping) or stages["panel"].get("status") != "completed" or stages["panel"].get("artifact") != "panel.json":
                raise ConfigurationError("Fusion continuation source panel stage must be exactly completed")
            panel = store.read_json("panel.json")
            ledger = store.read_json("ledger.json")
            _require_exact_keys(ledger, LEDGER_FIELDS, "Source ledger")
            if not isinstance(ledger.get("attempt_entries"), list) or not isinstance(ledger.get("entries"), list) or len(ledger["attempt_entries"]) != 4 or len(ledger["entries"]) != 4:
                raise ConfigurationError("Source ledger must contain exactly three panels and one failed judge")
            for index, attempt in enumerate(ledger["attempt_entries"]):
                if not isinstance(attempt, Mapping):
                    raise ConfigurationError("Source ledger attempt must be an object")
                _require_exact_keys(attempt, ATTEMPT_FIELDS, "Source ledger attempt")
                if attempt.get("attempt_index") != index or attempt.get("attempt_id") != attempt_receipt_id(validate_sha256(attempt.get("invocation_sha256"), "attempt invocation"), index):
                    raise ConfigurationError("Source ledger attempt chain is invalid")
            for entry in ledger["entries"]:
                if not isinstance(entry, Mapping):
                    raise ConfigurationError("Source ledger entry must be an object")
                _require_exact_keys(entry, LEDGER_ENTRY_FIELDS, "Source ledger entry")
            try:
                BudgetTracker(profile.get("budgets", {})).restore(ledger)
            except LegacyConfigError as exc:
                raise ConfigurationError(str(exc)) from exc
            seat_names = _profile_panel_seats(translated_config_copy, profile)
            if set(expected_hashes) != set(seat_names):
                raise ConfigurationError("Expected panel response hashes do not match the configured seat set")
            invocations = _panel_invocations(store=store, task=task, context=context, mechanical_evidence=mechanical_evidence, translated_config=translated_config_copy, profile=profile, seat_names=seat_names)
            reports = _validate_panel(store=store, panel=panel, translated_config=translated_config_copy, profile=profile, seat_names=seat_names, invocations=invocations, expected_hashes=expected_hashes)
            judge_response, judge_evidence = _validate_failed_judge(store=store, task=task, mechanical_evidence=mechanical_evidence, reports=reports, translated_config=translated_config_copy, profile=profile, ledger=ledger, expected_response_sha256=expected_failed_judge_response_sha256)

            responses_by_entry_id: dict[str, Mapping[str, Any]] = {}
            required_files = {"manifest.json", "panel.json", "ledger.json"}
            for entry in ledger["entries"]:
                artifact = _read_response_artifact(store, entry, f"Source response {entry['entry_id']}")
                responses_by_entry_id[str(entry["entry_id"])] = artifact["response"]
                required_files.add(str(entry["response_artifact"]))
            if set(before_hashes) != required_files:
                raise ConfigurationError("Source run contains unexpected or missing continuation artifacts")
            source_usage = _corrected_source_usage(ledger=ledger, responses_by_entry_id=responses_by_entry_id)
            residual_budget = _residual_budget(profile.get("budgets", {}), source_usage)
            after_records, after_hashes = _source_tree_inventory(store.directory)
            after_tree_sha256 = canonical_hash(after_records)
            if before_records != after_records or before_hashes != after_hashes:
                raise ConfigurationError("Source run changed during continuation inspection")
    except LegacyConfigError as exc:
        raise ConfigurationError(str(exc)) from exc

    _validate_live_source_job_documents(
        source_run_id=source_run_id,
        expected_manifest=job_manifest,
        expected_request=request,
    )

    if before_tree_sha256 != expected_source_tree_sha256:
        raise ConfigurationError("Source run tree does not match the caller-audited hash")
    if before_hashes.get("manifest.json") != expected_source_engine_manifest_file_sha256:
        raise ConfigurationError("Source engine manifest file does not match the caller-audited hash")
    if before_hashes.get("ledger.json") != expected_source_ledger_file_sha256:
        raise ConfigurationError("Source ledger file does not match the caller-audited hash")
    lineage: dict[str, Any] = {
        "schema_version": 1,
        "kind": "immutable_failed_fusion_panel_continuation",
        "source_run_id": source_run_id,
        "expected_child_run_id": _expected_child_run_id(source_run_id),
        "source_plugin_version": SOURCE_PLUGIN_VERSION,
        "source_profile_name": profile_name,
        "translated_profile_name": translated_profile_name,
        "source_job_manifest_sha256": job_manifest["manifest_sha256"],
        "source_request_sha256": job_manifest["request_sha256"],
        "source_schema_v2_sha256": source_schema_v2_sha256,
        "current_schema_v2_sha256": current_schema_v2_sha256,
        "translated_engine_sha256": translated_engine_sha256,
        "task_sha256": text_hash(task),
        "context_sha256": text_hash(context),
        "mechanical_evidence_sha256": text_hash(mechanical_evidence),
        "source_input_sha256": canonical_json_hash(input_identity),
        "source_tree_sha256": before_tree_sha256,
        "source_artifact_file_sha256": before_hashes,
        "panel": {
            "status": "imported_source_bound_evidence",
            "seat_order": [report["seat_name"] for report in reports],
            "response_sha256_by_seat": expected_hashes,
            "receipt_scope": "source_run_only",
        },
        "failed_judge": {
            **judge_evidence,
            "status": "spend_only_not_semantic_input",
            "raw_usage": judge_response["usage"],
            "normalization": "legacy_grok_disjoint_cache_read_added_to_input",
        },
        "provenance_taints": [
            {
                "id": "legacy_0_2_1_grok_tool_isolation_unverified",
                "applies_to": [report["seat_name"] for report in reports if report["response"]["route"].get("transport") == "grok_cli_oauth"] + [judge_evidence["seat_name"]],
                "statement": "The 0.2.1 tools_disabled route claim is retained but is not proof of Grok tool isolation.",
            }
        ],
    }
    lineage["lineage_sha256"] = canonical_json_hash(lineage)
    return {
        "lineage": lineage,
        "reports": reports,
        "source_usage": source_usage,
        "residual_budget": residual_budget,
        "source_tree_sha256": before_tree_sha256,
    }


def verify_source_run(*, expected_binding: Mapping[str, Any], **inspection_arguments: Any) -> dict[str, Any]:
    """Reinspect and exact-compare the complete immutable source snapshot."""

    if not isinstance(expected_binding, Mapping):
        raise ConfigurationError("expected_binding must be a continuation snapshot object")
    fresh = inspect_source_run(**inspection_arguments)
    expected = _detached_json(expected_binding, "expected_binding")
    if canonical_json_hash(fresh) != canonical_json_hash(expected) or fresh != expected:
        raise ConfigurationError("Fusion continuation source binding changed after inspection")
    return fresh


class _ValidatedSourceBinding:
    """In-process capability backed only by a complete source inspection."""

    __slots__ = ("_seal", "_inspection_arguments", "_snapshot")

    def __init__(
        self,
        seal: object,
        inspection_arguments: Mapping[str, Any],
        snapshot: Mapping[str, Any],
    ) -> None:
        if seal is not _SOURCE_BINDING_SEAL:
            raise ConfigurationError(
                "Validated continuation bindings cannot be constructed directly"
            )
        self._seal = seal
        self._inspection_arguments = _detached_json(
            inspection_arguments,
            "source binding inspection arguments",
        )
        self._snapshot = _detached_json(snapshot, "source binding snapshot")

    def snapshot(self, *, reverify: bool) -> dict[str, Any]:
        if self._seal is not _SOURCE_BINDING_SEAL:
            raise ConfigurationError("Fusion continuation source binding is invalid")
        if reverify:
            return verify_source_run(
                expected_binding=self._snapshot,
                **self._inspection_arguments,
            )
        return _detached_json(self._snapshot, "source binding snapshot")


def bind_source_run(
    *,
    expected_binding: Mapping[str, Any] | None = None,
    **inspection_arguments: Any,
) -> _ValidatedSourceBinding:
    """Validate an exact source and return a sealed, recheckable binding."""

    if expected_binding is None:
        snapshot = inspect_source_run(**inspection_arguments)
    else:
        snapshot = verify_source_run(
            expected_binding=expected_binding,
            **inspection_arguments,
        )
    return _ValidatedSourceBinding(
        _SOURCE_BINDING_SEAL,
        inspection_arguments,
        snapshot,
    )


def validated_source_snapshot(
    binding: Any,
    *,
    reverify: bool,
) -> dict[str, Any]:
    """Extract a snapshot only from the validator's exact sealed type."""

    if type(binding) is not _ValidatedSourceBinding:
        raise ConfigurationError(
            "Fusion continuation requires a validator-owned source binding"
        )
    return binding.snapshot(reverify=reverify)


def validated_execution_source_snapshot(
    binding: Any,
    *,
    reverify: bool,
    task: str,
    context: str,
    mechanical_evidence: str,
    source_profile_name: str,
    current_schema_v2_sha256: str,
    translated_profile_name: str,
    translated_engine_sha256: str,
    child_run_id: str,
) -> dict[str, Any]:
    """Require one sealed binding to match the exact child execution identity."""

    if not all(
        isinstance(value, str)
        for value in (
            task,
            context,
            mechanical_evidence,
            source_profile_name,
            translated_profile_name,
            child_run_id,
        )
    ):
        raise ConfigurationError(
            "Fusion continuation execution identity must contain only strings"
        )
    if current_schema_v2_sha256 is not None:
        validate_sha256(current_schema_v2_sha256, "current_schema_v2_sha256")
    validate_sha256(translated_engine_sha256, "translated_engine_sha256")
    snapshot = validated_source_snapshot(binding, reverify=reverify)
    lineage = snapshot.get("lineage")
    if not isinstance(lineage, Mapping):
        raise ConfigurationError("Fusion continuation binding has no lineage")
    expected_identity = {
        "task_sha256": text_hash(task),
        "context_sha256": text_hash(context),
        "mechanical_evidence_sha256": text_hash(mechanical_evidence),
        "source_profile_name": source_profile_name,
        "translated_profile_name": translated_profile_name,
        "translated_engine_sha256": translated_engine_sha256,
        "expected_child_run_id": child_run_id,
    }
    if current_schema_v2_sha256 is not None:
        expected_identity["current_schema_v2_sha256"] = current_schema_v2_sha256
    mismatches = [
        field_name
        for field_name, expected_value in expected_identity.items()
        if lineage.get(field_name) != expected_value
    ]
    if mismatches:
        raise ConfigurationError(
            "Fusion continuation execution does not match its source binding: "
            + ", ".join(sorted(mismatches))
        )
    return snapshot


__all__ = [
    "bind_source_run",
    "hash_source_run_tree",
    "inspect_source_run",
    "validated_execution_source_snapshot",
    "validated_source_snapshot",
    "validate_sha256",
    "validate_sha256_mapping",
    "verify_source_run",
]
