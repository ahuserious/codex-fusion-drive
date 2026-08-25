"""Durable non-blocking Fusion Drive jobs.

The MCP request starts a detached worker and returns immediately. Each job is
bound to an immutable request and configuration hash so a repeated
idempotency key cannot silently dispatch duplicate provider work.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from relentless_inception.errors import ConfigError as LegacyConfigError
from relentless_inception.execution import persisted_execution_contract

from . import __version__
from .config import load_config, runtime_dir, validate_config
from .continuation import (
    bind_source_run,
    hash_source_run_tree,
    validated_execution_source_snapshot,
    validated_source_snapshot,
)
from .engine import FusionDriveEngine, translate_config
from .errors import ConfigurationError, ExternalActionRequired
from .lifecycle import (
    initialized_lifecycle_receipt,
    initialize_lifecycle,
    lifecycle_path,
    load_lifecycle,
    validate_initialized_lifecycle,
    validate_lifecycle_descendant,
)
from .util import (
    atomic_write_json,
    canonical_hash,
    exclusive_lock,
    json_copy,
    now_utc,
    read_json,
    read_only_existing_lock,
    text_hash,
    validate_identifier,
)


TERMINAL_STATUSES = {"completed", "failed", "aborted"}
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
TOKEN_PATTERN = re.compile(
    r"\b(?:sk|xai|oauth|bearer)[-_A-Za-z0-9.]{12,}\b",
    re.IGNORECASE,
)


def _safe_error(value: str, limit: int = 500) -> str:
    redacted = EMAIL_PATTERN.sub("<redacted-email>", value)
    redacted = TOKEN_PATTERN.sub("<redacted-token>", redacted)
    return redacted.strip()[:limit]


def _safe_error_notes(error: BaseException) -> list[str]:
    raw_notes: list[Any] = [
        getattr(error, "receipt_callback_note", None),
        getattr(error, "continuation_accounting_note", None),
    ]
    exception_notes = getattr(error, "__notes__", None)
    if isinstance(exception_notes, list):
        raw_notes.extend(exception_notes)
    notes: list[str] = []
    for raw_note in raw_notes:
        if not isinstance(raw_note, str) or not raw_note.strip():
            continue
        note = _safe_error(raw_note)
        if note and note not in notes:
            notes.append(note)
    return notes


def _jobs_root() -> Path:
    root = runtime_dir() / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _job_directory(job_id: str) -> Path:
    path = _jobs_root() / validate_identifier(job_id, "job_id")
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _manifest_path(job_id: str) -> Path:
    return _job_directory(job_id) / "job.json"


def _request_path(job_id: str) -> Path:
    return _job_directory(job_id) / "request.json"


def _result_path(job_id: str) -> Path:
    return _job_directory(job_id) / "result.json"


def _completion_path(job_id: str) -> Path:
    return _job_directory(job_id) / "job-completion.json"


def _reconciliation_path(job_id: str) -> Path:
    return _job_directory(job_id) / "continuation-lifecycle-reconciliation.json"


def _lock_path(job_id: str) -> Path:
    return _job_directory(job_id) / ".job.lock"


def _execution_lock_path(job_id: str) -> Path:
    return _job_directory(job_id) / ".execution.lock"


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    value = json_copy(dict(manifest))
    value.pop("manifest_sha256", None)
    return canonical_hash(value)


def _completion_hash(receipt: Mapping[str, Any]) -> str:
    value = json_copy(dict(receipt))
    value.pop("completion_sha256", None)
    return canonical_hash(value)


def _read_completion_receipt(job_id: str) -> dict[str, Any]:
    path = _completion_path(job_id)
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(
            "Fusion Drive completion receipt is missing or symlinked"
        )
    value = read_json(path)
    if not isinstance(value, Mapping):
        raise ConfigurationError("Fusion Drive completion receipt must be an object")
    receipt = dict(value)
    expected_keys = {
        "schema_version",
        "kind",
        "job_id",
        "operation",
        "profile",
        "request_sha256",
        "config_sha256",
        "plugin_version",
        "preterminal_manifest_sha256",
        "abort_requested",
        "status",
        "finished_at",
        "result_sha256",
        "result",
        "completion_sha256",
    }
    result = receipt.get("result")
    if (
        set(receipt) != expected_keys
        or type(receipt.get("schema_version")) is not int
        or receipt.get("schema_version") != 1
        or receipt.get("kind") != "job_completion"
        or receipt.get("job_id") != job_id
        or receipt.get("abort_requested") is not False
        or receipt.get("status") != "completed"
        or not isinstance(receipt.get("finished_at"), str)
        or not isinstance(result, Mapping)
        or canonical_hash(result) != receipt.get("result_sha256")
        or receipt.get("completion_sha256") != _completion_hash(receipt)
    ):
        raise ConfigurationError("Fusion Drive completion receipt is invalid")
    return receipt


def _reconciliation_hash(receipt: Mapping[str, Any]) -> str:
    value = json_copy(dict(receipt))
    value.pop("reconciliation_sha256", None)
    return canonical_hash(value)


def _read_reconciliation_receipt(job_id: str) -> dict[str, Any]:
    path = _reconciliation_path(job_id)
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(
            "Fusion continuation reconciliation receipt is missing or symlinked"
        )
    receipt_value = read_json(path)
    if not isinstance(receipt_value, Mapping):
        raise ConfigurationError(
            "Fusion continuation reconciliation receipt must be an object"
        )
    receipt = dict(receipt_value)
    expected_keys = {
        "schema_version",
        "kind",
        "job_id",
        "base_result_sha256",
        "effective_result_sha256",
        "lifecycle_sha256",
        "reconciled_at",
        "result",
        "reconciliation_sha256",
    }
    effective_result = receipt.get("result")
    if (
        set(receipt) != expected_keys
        or type(receipt.get("schema_version")) is not int
        or receipt.get("schema_version") != 1
        or receipt.get("kind")
        != "continuation_lifecycle_reconciliation"
        or receipt.get("job_id") != job_id
        or not isinstance(receipt.get("reconciled_at"), str)
        or not isinstance(effective_result, Mapping)
    ):
        raise ConfigurationError(
            "Fusion continuation reconciliation receipt has an invalid shape"
        )
    base_result_sha256 = _required_sha256(
        receipt.get("base_result_sha256"),
        "reconciliation base_result_sha256",
    )
    effective_result_sha256 = _required_sha256(
        receipt.get("effective_result_sha256"),
        "reconciliation effective_result_sha256",
    )
    lifecycle_sha256 = _required_sha256(
        receipt.get("lifecycle_sha256"),
        "reconciliation lifecycle_sha256",
    )
    host_lifecycle = effective_result.get("host_lifecycle")
    if (
        canonical_hash(effective_result) != effective_result_sha256
        or not isinstance(host_lifecycle, Mapping)
        or host_lifecycle.get("lifecycle_sha256") != lifecycle_sha256
        or base_result_sha256 == effective_result_sha256
    ):
        raise ConfigurationError(
            "Fusion continuation reconciliation receipt is internally inconsistent"
        )
    if receipt.get("reconciliation_sha256") != _reconciliation_hash(receipt):
        raise ConfigurationError(
            "Fusion continuation reconciliation receipt hash mismatch"
        )
    return receipt


def _load_manifest(job_id: str) -> dict[str, Any]:
    path = _manifest_path(job_id)
    if not path.exists():
        raise ConfigurationError(f"Unknown Fusion Drive job: {job_id}")
    manifest = read_json(path)
    if manifest.get("manifest_sha256") != _manifest_hash(manifest):
        raise ConfigurationError("Fusion Drive job manifest hash mismatch")
    return manifest


def _save_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    manifest["updated_at"] = now_utc()
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    atomic_write_json(_manifest_path(str(manifest["job_id"])), manifest)
    return json_copy(manifest)


def _build_completion_receipt(
    manifest: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    finished_at: str,
) -> dict[str, Any]:
    if manifest.get("abort_requested") is not False:
        raise ConfigurationError(
            "An aborted Fusion Drive job cannot commit a completed result"
        )
    detached_result = json_copy(dict(result))
    receipt = {
        "schema_version": 1,
        "kind": "job_completion",
        "job_id": manifest.get("job_id"),
        "operation": manifest.get("operation"),
        "profile": manifest.get("profile"),
        "request_sha256": manifest.get("request_sha256"),
        "config_sha256": manifest.get("config_sha256"),
        "plugin_version": manifest.get("plugin_version"),
        "preterminal_manifest_sha256": manifest.get("manifest_sha256"),
        "abort_requested": False,
        "status": "completed",
        "finished_at": finished_at,
        "result_sha256": canonical_hash(detached_result),
        "result": detached_result,
    }
    receipt["completion_sha256"] = _completion_hash(receipt)
    return receipt


def _promote_completion_receipt(
    manifest: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    identity_fields = (
        "job_id",
        "operation",
        "profile",
        "request_sha256",
        "config_sha256",
        "plugin_version",
    )
    if (
        any(manifest.get(key) != receipt.get(key) for key in identity_fields)
        or manifest.get("abort_requested") is not False
    ):
        raise ConfigurationError(
            "Fusion Drive completion receipt conflicts with its job claim"
        )
    if manifest.get("status") == "completed":
        if manifest.get("result_sha256") != receipt.get("result_sha256"):
            raise ConfigurationError(
                "Fusion Drive completed manifest conflicts with its completion receipt"
            )
    elif (
        manifest.get("status") in TERMINAL_STATUSES
        or manifest.get("manifest_sha256")
        != receipt.get("preterminal_manifest_sha256")
    ):
        raise ConfigurationError(
            "Fusion Drive completion receipt does not bind the current preterminal manifest"
        )
    result_path = _result_path(str(manifest["job_id"]))
    if result_path.is_symlink():
        raise ConfigurationError("Fusion Drive result path must not be a symlink")
    if result_path.exists():
        persisted_result = read_json(result_path)
        if (
            canonical_hash(persisted_result) != receipt.get("result_sha256")
            or persisted_result != receipt.get("result")
        ):
            raise ConfigurationError(
                "Fusion Drive job result hash mismatch with its completion receipt"
            )
    else:
        atomic_write_json(result_path, receipt["result"])
    if manifest.get("status") == "completed":
        return json_copy(dict(manifest))
    promoted = json_copy(dict(manifest))
    promoted["status"] = "completed"
    promoted["worker_state"] = "completed"
    promoted["finished_at"] = receipt["finished_at"]
    promoted["result_sha256"] = receipt["result_sha256"]
    promoted["error"] = None
    return _save_manifest(promoted)


def _validated_completed_job_manifest(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind every current-version completed job to its append-only journal."""

    if (
        manifest.get("status") == "completed"
        and manifest.get("plugin_version") == __version__
    ):
        return _promote_completion_receipt(
            manifest,
            _read_completion_receipt(str(manifest["job_id"])),
        )
    return json_copy(dict(manifest))


def _public_manifest(manifest: Mapping[str, Any], *, reused: bool | None = None) -> dict[str, Any]:
    result = {
        key: json_copy(manifest.get(key))
        for key in (
            "schema_version",
            "job_id",
            "run_id",
            "operation",
            "profile",
            "status",
            "abort_requested",
            "request_sha256",
            "config_sha256",
            "plugin_version",
            "worker_pid",
            "worker_started_at",
            "worker_state",
            "created_at",
            "started_at",
            "finished_at",
            "updated_at",
            "result_sha256",
            "manifest_sha256",
            "error",
        )
    }
    result["recoverable"] = True
    result["automatic_redispatch"] = False
    if reused is not None:
        result["reused"] = reused
    return result


def _job_id(operation: str, idempotency_key: str) -> str:
    validate_identifier(idempotency_key, "idempotency_key")
    return "job-" + canonical_hash(
        {"operation": operation, "idempotency_key": idempotency_key}
    )[:24]


def _worker_command(job_id: str) -> tuple[list[str], dict[str, str], Path]:
    plugin_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(plugin_root)
        if not existing_python_path
        else str(plugin_root) + os.pathsep + existing_python_path
    )
    command = [
        sys.executable,
        "-m",
        "codex_fusion_drive.jobs",
        "--worker",
        job_id,
    ]
    return command, environment, plugin_root


def _start_job(
    operation: str,
    *,
    idempotency_key: str,
    profile_name: str,
    arguments: Mapping[str, Any],
    verified_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = (
        json_copy(dict(verified_config))
        if verified_config is not None
        else load_config()
    )
    errors = validate_config(config)
    if errors:
        raise ConfigurationError(
            "Cannot start a job with invalid configuration:\n- "
            + "\n- ".join(errors)
        )
    if profile_name not in config["profiles"]:
        raise ConfigurationError(f"Unknown Fusion Drive profile: {profile_name}")

    job_id = _job_id(operation, idempotency_key)
    request = {
        "schema_version": 1,
        "job_id": job_id,
        "operation": operation,
        "profile": profile_name,
        "arguments": json_copy(dict(arguments)),
    }
    request_sha256 = canonical_hash(request)
    config_sha256 = canonical_hash(config)

    with exclusive_lock(_lock_path(job_id)):
        manifest_path = _manifest_path(job_id)
        if manifest_path.exists():
            manifest = _load_manifest(job_id)
            if (
                manifest.get("request_sha256") != request_sha256
                or manifest.get("config_sha256") != config_sha256
            ):
                raise ConfigurationError(
                    "Job claim/idempotency key already belongs to a different request or configuration"
                )
            return _public_manifest(manifest, reused=True)

        atomic_write_json(_request_path(job_id), request)
        created_at = now_utc()
        manifest = {
            "schema_version": 1,
            "job_id": job_id,
            "run_id": job_id,
            "operation": operation,
            "profile": profile_name,
            "status": "queued",
            "abort_requested": False,
            "request_sha256": request_sha256,
            "config_sha256": config_sha256,
            "plugin_version": __version__,
            "worker_pid": None,
            "worker_state": "launching",
            "created_at": created_at,
            "started_at": None,
            "finished_at": None,
            "updated_at": created_at,
            "result_sha256": None,
            "error": None,
        }
        _save_manifest(manifest)
        command, environment, plugin_root = _worker_command(job_id)
        try:
            worker = subprocess.Popen(
                command,
                cwd=plugin_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            manifest["status"] = "failed"
            manifest["worker_state"] = "launch_failed"
            manifest["finished_at"] = now_utc()
            manifest["error"] = {
                "type": type(exc).__name__,
                "message": _safe_error(str(exc)),
            }
            return _public_manifest(_save_manifest(manifest), reused=False)
        manifest["worker_pid"] = worker.pid
        manifest["worker_started_at"] = _process_started_at(worker.pid)
        manifest["worker_state"] = "spawned"
        return _public_manifest(_save_manifest(manifest), reused=False)


def start_fuse_job(
    *,
    task: str,
    idempotency_key: str,
    confirmed_external_costs: bool,
    context: str = "",
    mechanical_evidence: str = "",
    profile_name: str | None = None,
) -> dict[str, Any]:
    if not confirmed_external_costs:
        raise ExternalActionRequired(
            "Asynchronous fusion consumes provider or subscription usage and requires confirmation"
        )
    if not task.strip():
        raise ConfigurationError("Fusion task cannot be empty")
    config = load_config()
    selected_profile = profile_name or str(config["active_profile"])
    return _start_job(
        "fuse",
        idempotency_key=idempotency_key,
        profile_name=selected_profile,
        arguments={
            "task": task,
            "context": context,
            "mechanical_evidence": mechanical_evidence,
        },
    )


def _required_sha256(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{64}", value) is None
    ):
        raise ConfigurationError(
            f"{field_name} must be a lowercase SHA-256 digest"
        )
    return value


def _source_job_documents(
    source_job_id: str,
    *,
    expected_request_sha256: str,
    expected_manifest_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read and validate one terminal source job without creating artifacts."""

    validate_identifier(source_job_id, "source_job_id")
    expected_request_sha256 = _required_sha256(
        expected_request_sha256,
        "expected_source_request_sha256",
    )
    expected_manifest_sha256 = _required_sha256(
        expected_manifest_sha256,
        "expected_source_manifest_sha256",
    )
    jobs_root = (runtime_dir() / "jobs").resolve()
    source_directory = runtime_dir() / "jobs" / source_job_id
    if source_directory.is_symlink():
        raise ConfigurationError(
            "Fusion continuation rejects a symlinked source job directory"
        )
    if not source_directory.is_dir():
        raise ConfigurationError(
            f"Unknown Fusion Drive source job: {source_job_id}"
        )
    try:
        source_directory.resolve().relative_to(jobs_root)
    except ValueError as exc:
        raise ConfigurationError(
            "Fusion continuation source job escapes the runtime jobs directory"
        ) from exc
    manifest_path = source_directory / "job.json"
    request_path = source_directory / "request.json"
    lock_path = source_directory / ".job.lock"
    source_files = (manifest_path, request_path, lock_path)
    if any(path.is_symlink() for path in source_files):
        raise ConfigurationError(
            "Fusion continuation source job files must not be symlinks"
        )
    if any(not path.is_file() for path in source_files):
        raise ConfigurationError(
            "Fusion continuation source job files must be existing regular files"
        )

    with read_only_existing_lock(lock_path):
        manifest = read_json(manifest_path)
        request = read_json(request_path)
    manifest_sha256 = _manifest_hash(manifest)
    request_sha256 = canonical_hash(request)
    if manifest.get("manifest_sha256") != manifest_sha256:
        raise ConfigurationError("Fusion continuation source job manifest hash mismatch")
    if manifest_sha256 != expected_manifest_sha256:
        raise ConfigurationError(
            "Fusion continuation source manifest does not match the expected hash"
        )
    if request_sha256 != manifest.get("request_sha256"):
        raise ConfigurationError("Fusion continuation source request hash mismatch")
    if request_sha256 != expected_request_sha256:
        raise ConfigurationError(
            "Fusion continuation source request does not match the expected hash"
        )
    if (
        manifest.get("job_id") != source_job_id
        or manifest.get("run_id") != source_job_id
        or manifest.get("operation") != "fuse"
        or manifest.get("status") != "failed"
        or manifest.get("result_sha256") is not None
    ):
        raise ConfigurationError(
            "Fusion continuation requires a terminal failed source fuse job without a result"
        )
    if (
        request.get("schema_version") != 1
        or request.get("job_id") != source_job_id
        or request.get("operation") != "fuse"
        or request.get("profile") != manifest.get("profile")
        or not isinstance(request.get("arguments"), Mapping)
    ):
        raise ConfigurationError(
            "Fusion continuation source request identity is invalid"
        )
    return manifest, request


def _inspect_continuation_source(
    *,
    config: Mapping[str, Any],
    source_job_id: str,
    expected_source_request_sha256: str,
    expected_source_manifest_sha256: str,
    expected_source_tree_sha256: str,
    expected_source_engine_manifest_file_sha256: str,
    expected_source_ledger_file_sha256: str,
    expected_failed_judge_response_sha256: str,
    expected_panel_response_hashes: Mapping[str, Any],
    selected_profile: str,
    expected_binding: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Any]:
    os.environ["RELENTLESS_INCEPTION_DATA_DIR"] = str(runtime_dir() / "engine")
    source_manifest, source_request = _source_job_documents(
        source_job_id,
        expected_request_sha256=expected_source_request_sha256,
        expected_manifest_sha256=expected_source_manifest_sha256,
    )
    if source_manifest.get("profile") != selected_profile:
        raise ConfigurationError(
            "Fusion continuation profile must match the source job profile"
        )
    source_arguments = source_request["arguments"]
    task = source_arguments.get("task")
    context = source_arguments.get("context", "")
    mechanical_evidence = source_arguments.get("mechanical_evidence", "")
    if not isinstance(task, str) or not task.strip():
        raise ConfigurationError("Fusion continuation source task is invalid")
    if not isinstance(context, str) or not isinstance(mechanical_evidence, str):
        raise ConfigurationError(
            "Fusion continuation source context and evidence must be strings"
        )
    translated_config, translated_profile = translate_config(
        config,
        profile_name=selected_profile,
    )
    inspection_arguments = {
        "source_run_id": source_job_id,
        "task": task,
        "context": context,
        "mechanical_evidence": mechanical_evidence,
        "profile_name": selected_profile,
        "translated_config": translated_config,
        "translated_profile_name": translated_profile,
        "expected_panel_response_hashes": expected_panel_response_hashes,
        "expected_source_tree_sha256": _required_sha256(
            expected_source_tree_sha256,
            "expected_source_tree_sha256",
        ),
        "expected_source_engine_manifest_file_sha256": _required_sha256(
            expected_source_engine_manifest_file_sha256,
            "expected_source_engine_manifest_file_sha256",
        ),
        "expected_source_ledger_file_sha256": _required_sha256(
            expected_source_ledger_file_sha256,
            "expected_source_ledger_file_sha256",
        ),
        "expected_failed_judge_response_sha256": _required_sha256(
            expected_failed_judge_response_sha256,
            "expected_failed_judge_response_sha256",
        ),
        "source_schema_v2_sha256": str(source_manifest["config_sha256"]),
        "current_schema_v2_sha256": canonical_hash(config),
        "source_job_manifest": source_manifest,
        "source_request": source_request,
    }
    source_binding = bind_source_run(
        **inspection_arguments,
        expected_binding=expected_binding,
    )
    snapshot = validated_source_snapshot(source_binding, reverify=False)
    return snapshot, source_manifest, source_request, source_binding


def start_fuse_continuation_job(
    *,
    source_job_id: str,
    expected_source_request_sha256: str,
    expected_source_manifest_sha256: str,
    expected_source_tree_sha256: str,
    expected_source_engine_manifest_file_sha256: str,
    expected_source_ledger_file_sha256: str,
    expected_failed_judge_response_sha256: str,
    expected_panel_response_hashes: Mapping[str, Any],
    idempotency_key: str,
    confirmed_external_costs: bool,
    profile_name: str | None = None,
) -> dict[str, Any]:
    """Start a child job that reuses only validated source panel artifacts."""

    if confirmed_external_costs is not True:
        raise ExternalActionRequired(
            "Fusion continuation consumes provider or subscription usage and requires confirmation"
        )
    validate_identifier(idempotency_key, "idempotency_key")
    if not isinstance(expected_panel_response_hashes, Mapping):
        raise ConfigurationError(
            "expected_panel_response_hashes must be an object keyed by seat name"
        )
    config = load_config()
    selected_profile = profile_name or str(config["active_profile"])
    source_snapshot, source_manifest, source_request, _source_binding = _inspect_continuation_source(
        config=config,
        source_job_id=source_job_id,
        expected_source_request_sha256=expected_source_request_sha256,
        expected_source_manifest_sha256=expected_source_manifest_sha256,
        expected_source_tree_sha256=expected_source_tree_sha256,
        expected_source_engine_manifest_file_sha256=(
            expected_source_engine_manifest_file_sha256
        ),
        expected_source_ledger_file_sha256=expected_source_ledger_file_sha256,
        expected_failed_judge_response_sha256=(
            expected_failed_judge_response_sha256
        ),
        expected_panel_response_hashes=expected_panel_response_hashes,
        selected_profile=selected_profile,
    )
    source_arguments = source_request["arguments"]
    # A source checkpoint owns at most one continuation child. Deriving the
    # durable claim from the source id prevents a different caller key, profile
    # revision, or retry wrapper from spawning duplicate judge/fuser/gate work.
    source_claim_key = "source-" + canonical_hash(
        {"schema_version": 1, "source_job_id": source_job_id}
    )[:48]
    return _start_job(
        "fuse_continue",
        idempotency_key=source_claim_key,
        profile_name=selected_profile,
        verified_config=config,
        arguments={
            "source_job_id": source_job_id,
            "expected_source_request_sha256": expected_source_request_sha256,
            "expected_source_manifest_sha256": expected_source_manifest_sha256,
            "expected_source_tree_sha256": expected_source_tree_sha256,
            "expected_source_engine_manifest_file_sha256": (
                expected_source_engine_manifest_file_sha256
            ),
            "expected_source_ledger_file_sha256": (
                expected_source_ledger_file_sha256
            ),
            "expected_failed_judge_response_sha256": (
                expected_failed_judge_response_sha256
            ),
            "expected_panel_response_hashes": json_copy(
                dict(expected_panel_response_hashes)
            ),
            "source_snapshot": source_snapshot,
            "source_plugin_version": source_manifest.get("plugin_version"),
            "task": str(source_arguments["task"]),
            "context": str(source_arguments.get("context", "")),
            "mechanical_evidence": str(
                source_arguments.get("mechanical_evidence", "")
            ),
        },
    )


def start_approval_gate_job(
    *,
    task: str,
    artifact: str,
    stage: str,
    idempotency_key: str,
    confirmed_external_costs: bool,
    mechanical_evidence: str = "",
    profile_name: str | None = None,
    workflow_id: str | None = None,
    expected_lifecycle_sha256: str | None = None,
) -> dict[str, Any]:
    if not confirmed_external_costs:
        raise ExternalActionRequired(
            "Asynchronous approval review consumes provider or subscription usage and requires confirmation"
        )
    if not task.strip() or not artifact.strip():
        raise ConfigurationError("Approval task and artifact cannot be empty")
    if workflow_id and not expected_lifecycle_sha256:
        raise ConfigurationError(
            "expected_lifecycle_sha256 is required when recording a workflow gate"
        )
    config = load_config()
    selected_profile = profile_name or str(config["active_profile"])
    profile = config.get("profiles", {}).get(selected_profile)
    if not isinstance(profile, Mapping):
        raise ConfigurationError(
            f"Unknown Fusion Drive profile: {selected_profile}"
        )
    gate_set = config["gate_sets"][profile["gate_set"]]
    if stage not in gate_set["stages"]:
        raise ConfigurationError(
            f"Unknown approval stage for profile {selected_profile}: {stage}"
        )
    return _start_job(
        "approval_gate",
        idempotency_key=idempotency_key,
        profile_name=selected_profile,
        arguments={
            "task": task,
            "artifact": artifact,
            "stage": stage,
            "mechanical_evidence": mechanical_evidence,
            "workflow_id": workflow_id,
            "expected_lifecycle_sha256": expected_lifecycle_sha256,
        },
    )


def _process_started_at(pid: int) -> str | None:
    """Return the OS-reported start time of a pid, or None if unavailable.

    Used to tell a live worker apart from an unrelated process that happens to
    have inherited its recycled pid.
    """

    # Deliberately total: this is a diagnostic probe layered onto job dispatch,
    # so no failure mode of it — missing `ps`, a sandbox, a patched subprocess —
    # may propagate into starting or reaping a job. Returning None just means
    # falling back to the plain pid check.
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        started = str(completed.stdout).strip()
    except Exception:
        return None
    return started or None


def _pid_is_alive(pid: Any, started_at: str | None = None) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if started_at is None:
        return True
    # The pid is live, but a recycled pid belongs to some other process. Without
    # this check a crashed worker looks alive forever and is never reclaimed.
    observed = _process_started_at(pid)
    return observed is None or observed == started_at


def job_status(job_id: str) -> dict[str, Any]:
    validate_identifier(job_id, "job_id")
    with exclusive_lock(_lock_path(job_id)):
        manifest = _load_manifest(job_id)
        completion_path = _completion_path(job_id)
        if completion_path.is_symlink():
            raise ConfigurationError(
                "Fusion Drive completion receipt path must not be a symlink"
            )
        if completion_path.exists():
            manifest = _promote_completion_receipt(
                manifest,
                _read_completion_receipt(job_id),
            )
        manifest = _validated_completed_job_manifest(manifest)
        queued_without_worker = (
            manifest["status"] == "queued"
            and manifest.get("worker_pid") is None
        )
        exited_without_receipt = (
            manifest["status"] not in TERMINAL_STATUSES
            and manifest.get("worker_pid") is not None
            and not _pid_is_alive(manifest.get("worker_pid"), manifest.get("worker_started_at"))
        )
        if queued_without_worker or exited_without_receipt:
            manifest["status"] = (
                "aborted" if manifest.get("abort_requested") else "failed"
            )
            manifest["worker_state"] = (
                "aborted"
                if manifest["status"] == "aborted"
                else "exited_without_receipt"
            )
            manifest["finished_at"] = now_utc()
            if manifest["status"] == "failed":
                manifest["error"] = {
                    "type": "WorkerExitedWithoutReceipt",
                    "message": (
                        "The detached worker exited without a terminal receipt; "
                        "the job will not be redispatched automatically"
                    ),
                }
            manifest = _save_manifest(manifest)
        return _public_manifest(manifest)


def _continuation_synthesis_receipt(
    base_result: Mapping[str, Any],
) -> dict[str, Any]:
    synthesis = base_result.get("synthesis")
    gate = base_result.get("gate")
    lineage = base_result.get("continuation_lineage")
    if (
        not isinstance(synthesis, str)
        or not synthesis
        or not isinstance(gate, Mapping)
        or gate.get("passed") is not True
        or not isinstance(lineage, Mapping)
    ):
        raise ConfigurationError(
            "Fusion continuation base result has no exact passed synthesis receipt"
        )
    return {
        "stage": "synthesis",
        "verdict": "PASS",
        "artifact_sha256": text_hash(synthesis),
        "engine_gate": json_copy(gate),
        "continuation": json_copy(lineage),
    }


def _validated_reconciliation_initial_lifecycle(
    *,
    job_id: str,
    manifest: Mapping[str, Any],
    base_result: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
) -> dict[str, Any]:
    workflow_report_value = base_result.get("workflow_report")
    lifecycle_report = (
        workflow_report_value.get("lifecycle")
        if isinstance(workflow_report_value, Mapping)
        else None
    )
    host_goal_creation_tool = (
        lifecycle_report.get("host_goal_creation_tool")
        if isinstance(lifecycle_report, Mapping)
        else None
    )
    profile_name = base_result.get("profile")
    engine_name = base_result.get("engine")
    synthesis = base_result.get("synthesis")
    if (
        not isinstance(host_goal_creation_tool, str)
        or not host_goal_creation_tool
        or not isinstance(profile_name, str)
        or not isinstance(engine_name, str)
        or not isinstance(synthesis, str)
    ):
        raise ConfigurationError(
            "Fusion continuation base result lacks lifecycle identity"
        )
    return validate_initialized_lifecycle(
        lifecycle,
        workflow_id=job_id,
        run_id=job_id,
        plan_sha256=text_hash(synthesis),
        config_sha256=str(manifest.get("config_sha256")),
        profile_name=profile_name,
        engine_name=engine_name,
        host_goal_creation_tool=host_goal_creation_tool,
        synthesis_gate_receipt=_continuation_synthesis_receipt(base_result),
    )


def _reconciled_continuation_result(
    base_result: Mapping[str, Any],
    initialized_lifecycle: Mapping[str, Any],
) -> dict[str, Any]:
    result = json_copy(dict(base_result))
    result["host_lifecycle"] = json_copy(initialized_lifecycle)
    result["lifecycle_status"] = "created"
    result["lifecycle_reconciliation"] = {
        "required": False,
        "reconciled": True,
    }
    result["next_action"] = (
        "Run the plan gate, then show the fused plan, Mermaid graph, and complete effective configuration. "
        "Do not execute until the user explicitly confirms the exact plan."
    )
    return result


def job_result(job_id: str) -> dict[str, Any]:
    validate_identifier(job_id, "job_id")
    with exclusive_lock(_lock_path(job_id)):
        manifest = _load_manifest(job_id)
        manifest = _validated_completed_job_manifest(manifest)
        if manifest["status"] != "completed":
            raise ConfigurationError(
                f"Fusion Drive job {job_id} is {manifest['status']}; no completed result is available"
            )
        base_result = read_json(_result_path(job_id))
        base_result_sha256 = canonical_hash(base_result)
        if base_result_sha256 != manifest.get("result_sha256"):
            raise ConfigurationError("Fusion Drive job result hash mismatch")
        reconciliation_path = _reconciliation_path(job_id)
        if reconciliation_path.is_symlink():
            raise ConfigurationError(
                "Fusion continuation reconciliation receipt must not be a symlink"
            )
        receipt = (
            _read_reconciliation_receipt(job_id)
            if reconciliation_path.exists()
            else None
        )
        if (
            receipt is None
            and base_result.get("lifecycle_status")
            == "reconciliation_required"
        ):
            live_lifecycle_path = lifecycle_path(job_id)
            if live_lifecycle_path.is_symlink() or live_lifecycle_path.exists():
                raise ConfigurationError(
                    "Fusion continuation lifecycle exists without its reconciliation receipt"
                )

    result = base_result
    effective_result_sha256 = base_result_sha256
    reconciliation = None
    if receipt is not None:
        if (
            manifest.get("operation") != "fuse_continue"
            or receipt.get("base_result_sha256") != base_result_sha256
        ):
            raise ConfigurationError(
                "Fusion continuation reconciliation does not bind the base result"
            )
        effective_result = receipt["result"]
        effective_result_sha256 = str(receipt["effective_result_sha256"])
        if (
            base_result.get("lifecycle_status") != "reconciliation_required"
            or base_result.get("host_lifecycle") is not None
            or effective_result.get("lifecycle_status") != "created"
            or effective_result.get("lifecycle_reconciliation")
            != {"required": False, "reconciled": True}
        ):
            raise ConfigurationError(
                "Fusion continuation reconciliation has invalid lifecycle states"
            )
        effective_lifecycle = effective_result.get("host_lifecycle")
        if not isinstance(effective_lifecycle, Mapping):
            raise ConfigurationError(
                "Fusion continuation reconciliation has no lifecycle creation receipt"
            )
        initial_lifecycle = _validated_reconciliation_initial_lifecycle(
            job_id=job_id,
            manifest=manifest,
            base_result=base_result,
            lifecycle=effective_lifecycle,
        )
        expected_effective_result = _reconciled_continuation_result(
            base_result,
            initial_lifecycle,
        )
        if effective_result != expected_effective_result:
            raise ConfigurationError(
                "Fusion continuation reconciliation altered immutable result fields"
            )
        validate_lifecycle_descendant(
            load_lifecycle(job_id),
            initial_lifecycle,
        )
        result = json_copy(effective_result)
        reconciliation = {
            key: json_copy(value)
            for key, value in receipt.items()
            if key != "result"
        }
    public_job = _public_manifest(manifest)
    public_job["result_sha256_scope"] = "base_result"
    public_job["base_result_sha256"] = base_result_sha256
    public_job["effective_result_sha256"] = effective_result_sha256
    return {
        "job": public_job,
        "result": result,
        "base_result_sha256": base_result_sha256,
        "effective_result_sha256": effective_result_sha256,
        "reconciliation": reconciliation,
    }


def _validated_completed_continuation_child(
    job_id: str,
    job_result_value: Mapping[str, Any],
    *,
    expected_task_hash: str,
    expected_config_hash: str,
    expected_input_hash: str,
    child_lock_held: bool = False,
) -> dict[str, Any]:
    child_run_directory = runtime_dir() / "engine" / "runs" / job_id
    if (
        child_run_directory.is_symlink()
        or not child_run_directory.is_dir()
        or Path(str(job_result_value.get("artifacts_dir", ""))).resolve()
        != child_run_directory.resolve()
    ):
        raise ConfigurationError(
            "Fusion continuation child artifact directory is missing or mismatched"
        )
    if not child_lock_held:
        with read_only_existing_lock(child_run_directory / ".run.lock"):
            return _validated_completed_continuation_child(
                job_id,
                job_result_value,
                expected_task_hash=expected_task_hash,
                expected_config_hash=expected_config_hash,
                expected_input_hash=expected_input_hash,
                child_lock_held=True,
            )
    if (child_run_directory / "KILL").exists():
        raise ConfigurationError(
            "Aborted Fusion continuation child cannot create lifecycle authority"
        )
    child_tree_sha256 = hash_source_run_tree(child_run_directory)
    child_manifest = read_json(child_run_directory / "manifest.json")
    child_result = read_json(child_run_directory / "result.json")
    child_accounting = read_json(
        child_run_directory / "continuation-accounting.json"
    )
    child_ledger = read_json(child_run_directory / "ledger.json")
    panel_import = read_json(child_run_directory / "panel-import.json")
    execution_handoff = read_json(
        child_run_directory / "execution-handoff.json"
    )
    expected_child_tree_sha256 = _required_sha256(
        job_result_value.get("continuation_child_tree_sha256"),
        "continuation_child_tree_sha256",
    )
    if child_tree_sha256 != expected_child_tree_sha256:
        raise ConfigurationError(
            "Fusion continuation child artifact tree changed after completion"
        )
    stages = child_manifest.get("stages")
    required_completed_stages = (
        "panel-import",
        "judge",
        "synthesis",
        "continuation-accounting",
    )
    if (
        child_manifest.get("run_id") != job_id
        or child_manifest.get("task_hash") != expected_task_hash
        or child_manifest.get("config_hash") != expected_config_hash
        or child_manifest.get("input_hash") != expected_input_hash
        or child_result.get("task_hash") != expected_task_hash
        or child_result.get("config_hash") != expected_config_hash
        or child_manifest.get("status") != "completed"
        or not isinstance(stages, Mapping)
        or any(
            not isinstance(stages.get(stage_name), Mapping)
            or stages[stage_name].get("status") != "completed"
            for stage_name in required_completed_stages
        )
    ):
        raise ConfigurationError(
            "Fusion continuation child run is not durably completed"
        )
    gate_rounds = sorted(
        int(match.group(1))
        for stage_name in stages
        for match in [re.fullmatch(r"gate-(\d+)", str(stage_name))]
        if match is not None
    )
    if not gate_rounds or gate_rounds != list(range(gate_rounds[-1] + 1)):
        raise ConfigurationError(
            "Fusion continuation child has an invalid gate-stage sequence"
        )
    final_round = gate_rounds[-1]
    for round_index in gate_rounds:
        gate_stage = stages[f"gate-{round_index}"]
        expected_status = "passed" if round_index == final_round else "rejected"
        if (
            not isinstance(gate_stage, Mapping)
            or gate_stage.get("status") != expected_status
            or gate_stage.get("artifact") != f"gate-{round_index}.json"
        ):
            raise ConfigurationError(
                "Fusion continuation child gate stages do not prove the final pass"
            )
        if round_index > 0:
            amendment_stage = stages.get(f"amendment-{round_index}")
            if (
                not isinstance(amendment_stage, Mapping)
                or amendment_stage.get("status") != "completed"
                or amendment_stage.get("artifact")
                != f"synthesis-amendment-{round_index}.json"
            ):
                raise ConfigurationError(
                    "Fusion continuation child amendment sequence is incomplete"
                )
    final_gate = read_json(child_run_directory / f"gate-{final_round}.json")
    final_synthesis_name = (
        "synthesis.json"
        if final_round == 0
        else f"synthesis-amendment-{final_round}.json"
    )
    final_synthesis = read_json(child_run_directory / final_synthesis_name)
    if (
        final_gate != child_result.get("gate")
        or final_synthesis.get("text") != child_result.get("synthesis")
        or final_synthesis.get("sha256")
        != text_hash(str(child_result.get("synthesis", "")))
    ):
        raise ConfigurationError(
            "Fusion continuation child final synthesis or gate artifact is mismatched"
        )
    for field_name in (
        "run_id",
        "task_hash",
        "config_hash",
        "status",
        "synthesis",
        "gate",
        "panel",
        "judge",
        "ledger",
        "execution_handoff",
    ):
        if child_result.get(field_name) != job_result_value.get(field_name):
            raise ConfigurationError(
                "Fusion continuation job result does not match its durable child result"
            )
    if (
        child_accounting.get("accounting_status") != "completed"
        or child_accounting.get("child_outcome") != "completed"
        or child_accounting.get("aggregate_ledger") != job_result_value.get("ledger")
        or child_accounting.get("child_ledger") != child_ledger
        or execution_handoff != job_result_value.get("execution_handoff")
        or child_result.get("panel") != panel_import.get("reports")
        or child_result.get("gate", {}).get("artifact_sha256")
        != text_hash(str(child_result.get("synthesis", "")))
    ):
        raise ConfigurationError(
            "Fusion continuation child accounting, handoff, or artifact binding is invalid"
        )
    try:
        persisted_execution_contract(execution_handoff)
    except LegacyConfigError as exc:
        raise ConfigurationError(
            "Fusion continuation child execution handoff is invalid"
        ) from exc
    return {
        "manifest": child_manifest,
        "result": child_result,
        "accounting": child_accounting,
        "ledger": child_ledger,
        "panel_import": panel_import,
        "execution_handoff": execution_handoff,
    }


def reconcile_fuse_continuation_job(
    *,
    job_id: str,
    expected_result_sha256: str,
) -> dict[str, Any]:
    """Create a missing host lifecycle without invoking any provider runtime."""

    validate_identifier(job_id, "job_id")
    provided_result_sha256 = _required_sha256(
        expected_result_sha256,
        "expected_result_sha256",
    )
    with exclusive_lock(_lock_path(job_id)):
        manifest = _load_manifest(job_id)
        if (
            manifest.get("status") == "completed"
            and (
                manifest.get("operation") != "fuse_continue"
                or manifest.get("abort_requested") is not False
            )
        ):
            raise ConfigurationError(
                "Only an exact completed continuation result can be reconciled"
            )
        manifest = _validated_completed_job_manifest(manifest)
        base_result_sha256 = str(manifest.get("result_sha256", ""))
        reconciliation_path = _reconciliation_path(job_id)
        if reconciliation_path.is_symlink():
            raise ConfigurationError(
                "Fusion continuation reconciliation receipt path is unsafe"
            )
        existing_receipt = (
            _read_reconciliation_receipt(job_id)
            if reconciliation_path.exists()
            else None
        )
        matches_effective_receipt = (
            existing_receipt is not None
            and existing_receipt.get("base_result_sha256")
            == base_result_sha256
            and existing_receipt.get("effective_result_sha256")
            == provided_result_sha256
        )
        if (
            manifest.get("operation") != "fuse_continue"
            or manifest.get("status") != "completed"
            or manifest.get("abort_requested") is not False
            or (
                provided_result_sha256 != base_result_sha256
                and not matches_effective_receipt
            )
        ):
            raise ConfigurationError(
                "Only an exact completed continuation result can be reconciled"
            )
        request = _read_request(job_id, manifest)
        result = read_json(_result_path(job_id))
        if canonical_hash(result) != base_result_sha256:
            raise ConfigurationError("Fusion continuation result hash mismatch")
        lifecycle_reconciliation = result.get("lifecycle_reconciliation")
        if (
            result.get("run_id") != job_id
            or result.get("workflow_id") != job_id
            or result.get("status") != "completed"
            or result.get("profile") != manifest.get("profile")
            or result.get("host_lifecycle") is not None
            or result.get("lifecycle_status") != "reconciliation_required"
            or not isinstance(lifecycle_reconciliation, Mapping)
            or lifecycle_reconciliation.get("required") is not True
        ):
            raise ConfigurationError(
                "Fusion continuation result is not eligible for lifecycle reconciliation"
            )
        gate = result.get("gate")
        if not isinstance(gate, Mapping) or gate.get("passed") is not True:
            raise ConfigurationError(
                "Fusion continuation lifecycle requires an exact passed synthesis gate"
            )
        synthesis = result.get("synthesis")
        if not isinstance(synthesis, str) or not synthesis:
            raise ConfigurationError(
                "Fusion continuation result has no synthesis artifact"
            )

        config = load_config()
        config_errors = validate_config(config)
        if config_errors:
            raise ConfigurationError(
                "Cannot reconcile with invalid configuration:\n- "
                + "\n- ".join(config_errors)
            )
        config_sha256 = canonical_hash(config)
        if (
            config_sha256 != manifest.get("config_sha256")
            or manifest.get("plugin_version") != __version__
        ):
            raise ConfigurationError(
                "Configuration or plugin version changed before lifecycle reconciliation"
            )
        arguments = request.get("arguments")
        if not isinstance(arguments, Mapping):
            raise ConfigurationError(
                "Fusion continuation request has no immutable arguments"
            )
        expected_binding = arguments.get("source_snapshot")
        if not isinstance(expected_binding, Mapping):
            raise ConfigurationError(
                "Fusion continuation request has no immutable source binding"
            )
        source_snapshot, _source_manifest, _source_request, source_binding = (
            _inspect_continuation_source(
                config=config,
                source_job_id=str(arguments["source_job_id"]),
                expected_source_request_sha256=str(
                    arguments["expected_source_request_sha256"]
                ),
                expected_source_manifest_sha256=str(
                    arguments["expected_source_manifest_sha256"]
                ),
                expected_source_tree_sha256=str(
                    arguments["expected_source_tree_sha256"]
                ),
                expected_source_engine_manifest_file_sha256=str(
                    arguments[
                        "expected_source_engine_manifest_file_sha256"
                    ]
                ),
                expected_source_ledger_file_sha256=str(
                    arguments["expected_source_ledger_file_sha256"]
                ),
                expected_failed_judge_response_sha256=str(
                    arguments["expected_failed_judge_response_sha256"]
                ),
                expected_panel_response_hashes=arguments[
                    "expected_panel_response_hashes"
                ],
                selected_profile=str(request["profile"]),
                expected_binding=expected_binding,
            )
        )
        if result.get("continuation_lineage") != source_snapshot.get("lineage"):
            raise ConfigurationError(
                "Fusion continuation result lineage does not match its live source binding"
            )

        synthesis_receipt = {
            "stage": "synthesis",
            "verdict": "PASS",
            "artifact_sha256": text_hash(synthesis),
            "engine_gate": json_copy(gate),
            "continuation": json_copy(source_snapshot["lineage"]),
        }
        selected_profile = str(request["profile"])
        translated_config, translated_profile = translate_config(
            config,
            profile_name=selected_profile,
        )
        child_run_directory = runtime_dir() / "engine" / "runs" / job_id
        expected_child_input_hash = canonical_hash(
            {
                "operation": "fuse_continue",
                "task": str(arguments["task"]),
                "context": str(arguments.get("context", "")),
                "mechanical_evidence": str(
                    arguments.get("mechanical_evidence", "")
                ),
                "profile_name": translated_profile,
                "source_snapshot_sha256": canonical_hash(source_snapshot),
            }
        )
        with read_only_existing_lock(child_run_directory / ".run.lock"):
            child_artifacts = _validated_completed_continuation_child(
                job_id,
                result,
                expected_task_hash=text_hash(str(arguments["task"])),
                expected_config_hash=canonical_hash(translated_config),
                expected_input_hash=expected_child_input_hash,
                child_lock_held=True,
            )
            if (
                child_artifacts["panel_import"] != source_snapshot
                or child_artifacts["accounting"].get("source_tree_sha256")
                != source_snapshot.get("source_tree_sha256")
                or child_artifacts["accounting"].get("source_usage")
                != source_snapshot.get("source_usage")
            ):
                raise ConfigurationError(
                    "Fusion continuation child does not match its revalidated source snapshot"
                )
            validated_execution_source_snapshot(
                source_binding,
                reverify=True,
                task=str(arguments["task"]),
                context=str(arguments.get("context", "")),
                mechanical_evidence=str(
                    arguments.get("mechanical_evidence", "")
                ),
                source_profile_name=selected_profile,
                current_schema_v2_sha256=config_sha256,
                translated_profile_name=translated_profile,
                translated_engine_sha256=canonical_hash(translated_config),
                child_run_id=job_id,
            )
            # Re-read every immutable job input immediately before creating
            # lifecycle authority. The job claim and child evidence leases stay
            # held until the append-only reconciliation receipt is committed.
            current_manifest = _load_manifest(job_id)
            current_request = _read_request(job_id, current_manifest)
            current_result = read_json(_result_path(job_id))
            if (
                current_manifest != manifest
                or current_request != request
                or canonical_hash(current_result) != base_result_sha256
                or current_result != result
            ):
                raise ConfigurationError(
                    "Fusion continuation result changed during lifecycle reconciliation"
                )
            reconciliation_path = _reconciliation_path(job_id)
            if reconciliation_path.is_symlink():
                raise ConfigurationError(
                    "Fusion continuation reconciliation receipt path is unsafe"
                )
            reused = reconciliation_path.exists()
            persisted_receipt = (
                _read_reconciliation_receipt(job_id) if reused else None
            )
            if persisted_receipt is not None:
                if (
                    persisted_receipt.get("base_result_sha256")
                    != base_result_sha256
                    or not isinstance(
                        persisted_receipt.get("result"), Mapping
                    )
                    or not isinstance(
                        persisted_receipt["result"].get("host_lifecycle"),
                        Mapping,
                    )
                ):
                    raise ConfigurationError(
                        "Fusion continuation reconciliation receipt does not bind this result"
                    )
                initial_lifecycle = (
                    _validated_reconciliation_initial_lifecycle(
                        job_id=job_id,
                        manifest=manifest,
                        base_result=result,
                        lifecycle=persisted_receipt["result"][
                            "host_lifecycle"
                        ],
                    )
                )
                validate_lifecycle_descendant(
                    load_lifecycle(job_id),
                    initial_lifecycle,
                )
            else:
                live_lifecycle = initialize_lifecycle(
                    job_id,
                    run_id=job_id,
                    plan_sha256=text_hash(synthesis),
                    config_sha256=config_sha256,
                    profile_name=selected_profile,
                    engine_name=str(
                        config["profiles"][selected_profile]["engine"]
                    ),
                    host_goal_creation_tool=str(
                        config["lifecycle"]["host_goal_creation_tool"]
                    ),
                    synthesis_gate_receipt=synthesis_receipt,
                )
                initial_lifecycle = initialized_lifecycle_receipt(
                    live_lifecycle
                )
                initial_lifecycle = (
                    _validated_reconciliation_initial_lifecycle(
                        job_id=job_id,
                        manifest=manifest,
                        base_result=result,
                        lifecycle=initial_lifecycle,
                    )
                )
                validate_lifecycle_descendant(
                    live_lifecycle,
                    initial_lifecycle,
                )
            reconciled_result = _reconciled_continuation_result(
                result,
                initial_lifecycle,
            )
            effective_result_sha256 = canonical_hash(reconciled_result)
            receipt = {
                "schema_version": 1,
                "kind": "continuation_lifecycle_reconciliation",
                "job_id": job_id,
                "base_result_sha256": base_result_sha256,
                "effective_result_sha256": effective_result_sha256,
                "lifecycle_sha256": initial_lifecycle[
                    "lifecycle_sha256"
                ],
                "reconciled_at": initial_lifecycle["updated_at"],
                "result": reconciled_result,
            }
            receipt["reconciliation_sha256"] = _reconciliation_hash(receipt)
            if reused:
                if persisted_receipt != receipt:
                    raise ConfigurationError(
                        "Fusion continuation reconciliation receipt conflicts with the verified lifecycle"
                    )
                receipt = persisted_receipt
            else:
                atomic_write_json(reconciliation_path, receipt)

        reconciliation = {
            key: json_copy(value)
            for key, value in receipt.items()
            if key != "result"
        }
    return {
        "job": _public_manifest(manifest),
        "result": json_copy(receipt["result"]),
        "base_result_sha256": base_result_sha256,
        "effective_result_sha256": receipt["effective_result_sha256"],
        "reconciliation": reconciliation,
        "reconciled": True,
        "reused": reused,
    }


def job_wait(
    job_id: str,
    *,
    timeout_seconds: float = 55.0,
    poll_interval_seconds: float = 1.0,
) -> dict[str, Any]:
    """Wait boundedly for one durable job and return its terminal receipt.

    This collapses repeated host-side job_status calls without changing job
    durability, cancellation, or result hash verification.
    """

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds < 0
        or timeout_seconds > 300
    ):
        raise ConfigurationError("timeout_seconds must be between 0 and 300")
    if (
        isinstance(poll_interval_seconds, bool)
        or not isinstance(poll_interval_seconds, (int, float))
        or poll_interval_seconds < 0.1
        or poll_interval_seconds > 10
    ):
        raise ConfigurationError(
            "poll_interval_seconds must be between 0.1 and 10"
        )

    deadline = time.monotonic() + float(timeout_seconds)
    while True:
        status = job_status(job_id)
        if status["status"] == "completed":
            return {
                **job_result(job_id),
                "wait_timed_out": False,
            }
        if status["status"] in TERMINAL_STATUSES:
            return {
                "job": status,
                "result": None,
                "wait_timed_out": False,
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "job": status,
                "result": None,
                "wait_timed_out": True,
            }
        time.sleep(min(float(poll_interval_seconds), remaining))


def job_abort(job_id: str) -> dict[str, Any]:
    validate_identifier(job_id, "job_id")
    with exclusive_lock(_lock_path(job_id)):
        manifest = _load_manifest(job_id)
        if manifest["status"] in TERMINAL_STATUSES:
            return _public_manifest(manifest)
        manifest["abort_requested"] = True
        manifest["status"] = "abort_requested"
        manifest["worker_state"] = "abort_requested"
        run_directory = runtime_dir() / "engine" / "runs" / str(manifest["run_id"])
        run_directory.mkdir(parents=True, exist_ok=True)
        os.chmod(run_directory, 0o700)
        kill_path = run_directory / "KILL"
        kill_path.touch(exist_ok=True)
        os.chmod(kill_path, 0o600)
        return _public_manifest(_save_manifest(manifest))


def _read_request(job_id: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    request = read_json(_request_path(job_id))
    if canonical_hash(request) != manifest.get("request_sha256"):
        raise ConfigurationError("Fusion Drive job request hash mismatch")
    return request


def _finish_job(
    job_id: str,
    *,
    status: str,
    result: Mapping[str, Any] | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    with exclusive_lock(_lock_path(job_id)):
        manifest = _load_manifest(job_id)
        if result is not None and status == "completed":
            if manifest.get("abort_requested") is not False:
                manifest["status"] = "aborted"
                manifest["worker_state"] = "aborted"
                manifest["finished_at"] = now_utc()
                return _save_manifest(manifest)
            completion_path = _completion_path(job_id)
            if completion_path.is_symlink():
                raise ConfigurationError(
                    "Fusion Drive completion receipt path must not be a symlink"
                )
            if completion_path.exists():
                completion_receipt = _read_completion_receipt(job_id)
                if completion_receipt.get("result") != result:
                    raise ConfigurationError(
                        "Fusion Drive completion receipt conflicts with the completed result"
                    )
            else:
                completion_receipt = _build_completion_receipt(
                    manifest,
                    result,
                    finished_at=now_utc(),
                )
                atomic_write_json(completion_path, completion_receipt)
            return _promote_completion_receipt(
                manifest,
                completion_receipt,
            )
        manifest["status"] = status
        manifest["worker_state"] = status
        manifest["finished_at"] = now_utc()
        if error is not None:
            error_receipt = {
                "type": type(error).__name__,
                "message": _safe_error(str(error)),
            }
            notes = _safe_error_notes(error)
            if notes:
                error_receipt["notes"] = notes
            manifest["error"] = error_receipt
        return _save_manifest(manifest)


def _run_job_with_execution_lease(
    job_id: str,
    *,
    engine_factory: Callable[[], FusionDriveEngine] = FusionDriveEngine,
) -> dict[str, Any]:
    validate_identifier(job_id, "job_id")
    try:
        with exclusive_lock(_lock_path(job_id)):
            manifest = _load_manifest(job_id)
            completion_path = _completion_path(job_id)
            if completion_path.is_symlink():
                raise ConfigurationError(
                    "Fusion Drive completion receipt path must not be a symlink"
                )
            if completion_path.exists():
                manifest = _promote_completion_receipt(
                    manifest,
                    _read_completion_receipt(job_id),
                )
            manifest = _validated_completed_job_manifest(manifest)
            if manifest["status"] in TERMINAL_STATUSES:
                return manifest
            if manifest.get("abort_requested"):
                manifest["status"] = "aborted"
                manifest["worker_state"] = "aborted"
                manifest["finished_at"] = now_utc()
                return _save_manifest(manifest)
            if manifest.get("status") != "queued":
                raise ConfigurationError(
                    "Fusion Drive job already has an execution claim; automatic redispatch is disabled"
                )
            if manifest.get("plugin_version") != __version__:
                raise ConfigurationError(
                    "Plugin version changed after job creation; provider work was not dispatched"
                )
            config = load_config()
            if canonical_hash(config) != manifest.get("config_sha256"):
                raise ConfigurationError(
                    "Configuration changed after job creation; provider work was not dispatched"
                )
            request = _read_request(job_id, manifest)
            manifest["status"] = "running"
            manifest["worker_state"] = "running"
            manifest["started_at"] = now_utc()
            manifest["worker_pid"] = os.getpid()
            manifest["worker_started_at"] = _process_started_at(os.getpid())
            _save_manifest(manifest)

        arguments = request["arguments"]
        engine = (
            FusionDriveEngine(config)
            if engine_factory is FusionDriveEngine
            else engine_factory()
        )
        if request["operation"] == "fuse_continue":
            engine_config = getattr(engine, "config", None)
            if (
                not isinstance(engine_config, Mapping)
                or canonical_hash(engine_config) != canonical_hash(config)
            ):
                raise ConfigurationError(
                    "Fusion continuation engine configuration does not match the verified job configuration"
                )
        if request["operation"] == "fuse":
            result = engine.fuse(
                str(arguments["task"]),
                context=str(arguments.get("context", "")),
                mechanical_evidence=str(
                    arguments.get("mechanical_evidence", "")
                ),
                profile_name=str(request["profile"]),
                resume_run_id=job_id,
            )
        elif request["operation"] == "fuse_continue":
            expected_binding = arguments.get("source_snapshot")
            if not isinstance(expected_binding, Mapping):
                raise ConfigurationError(
                    "Fusion continuation job is missing its immutable source snapshot"
                )
            (
                _fresh_snapshot,
                _source_manifest,
                _source_request,
                source_binding,
            ) = (
                _inspect_continuation_source(
                    config=config,
                    source_job_id=str(arguments["source_job_id"]),
                    expected_source_request_sha256=str(
                        arguments["expected_source_request_sha256"]
                    ),
                    expected_source_manifest_sha256=str(
                        arguments["expected_source_manifest_sha256"]
                    ),
                    expected_source_tree_sha256=str(
                        arguments["expected_source_tree_sha256"]
                    ),
                    expected_source_engine_manifest_file_sha256=str(
                        arguments[
                            "expected_source_engine_manifest_file_sha256"
                        ]
                    ),
                    expected_source_ledger_file_sha256=str(
                        arguments["expected_source_ledger_file_sha256"]
                    ),
                    expected_failed_judge_response_sha256=str(
                        arguments["expected_failed_judge_response_sha256"]
                    ),
                    expected_panel_response_hashes=arguments[
                        "expected_panel_response_hashes"
                    ],
                    selected_profile=str(request["profile"]),
                    expected_binding=expected_binding,
                )
            )
            result = engine._fuse_continue_validated(
                str(arguments["task"]),
                source_binding=source_binding,
                context=str(arguments.get("context", "")),
                mechanical_evidence=str(
                    arguments.get("mechanical_evidence", "")
                ),
                profile_name=str(request["profile"]),
                resume_run_id=job_id,
            )
        elif request["operation"] == "approval_gate":
            result = engine.approval_gate(
                str(arguments["task"]),
                str(arguments["artifact"]),
                stage=str(arguments["stage"]),
                mechanical_evidence=str(
                    arguments.get("mechanical_evidence", "")
                ),
                profile_name=str(request["profile"]),
                workflow_id=arguments.get("workflow_id"),
                expected_lifecycle_sha256=arguments.get(
                    "expected_lifecycle_sha256"
                ),
                resume_run_id=job_id,
            )
        else:
            raise ConfigurationError(
                f"Unsupported asynchronous operation: {request['operation']}"
            )
        return _finish_job(job_id, status="completed", result=result)
    except BaseException as exc:
        try:
            with exclusive_lock(_lock_path(job_id)):
                recovery_manifest = _load_manifest(job_id)
                completion_path = _completion_path(job_id)
                if completion_path.is_symlink():
                    raise ConfigurationError(
                        "Fusion Drive completion receipt path must not be a symlink"
                    )
                if completion_path.exists():
                    return _promote_completion_receipt(
                        recovery_manifest,
                        _read_completion_receipt(job_id),
                    )
        except OSError:
            # A valid completion journal must never be demoted to failed just
            # because materialization or manifest promotion was transiently
            # unavailable. Leave the preterminal claim for job_status/run_job
            # to promote without another provider dispatch.
            raise exc
        except ConfigurationError:
            pass
        try:
            manifest = _load_manifest(job_id)
            status = "aborted" if manifest.get("abort_requested") else "failed"
            return _finish_job(job_id, status=status, error=exc)
        except BaseException:
            raise exc


def run_job(
    job_id: str,
    *,
    engine_factory: Callable[[], FusionDriveEngine] = FusionDriveEngine,
) -> dict[str, Any]:
    """Execute one job under a process-wide durable dispatch lease."""

    validate_identifier(job_id, "job_id")
    with exclusive_lock(_execution_lock_path(job_id), timeout=0.1):
        return _run_job_with_execution_lease(
            job_id,
            engine_factory=engine_factory,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-fusion-drive-job")
    parser.add_argument("--worker", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    manifest = run_job(str(arguments.worker))
    return 0 if manifest.get("status") in {"completed", "aborted"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
