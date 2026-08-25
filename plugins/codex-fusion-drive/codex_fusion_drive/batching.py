"""Batch planning, immutable bundles, and opt-in provider submission."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import load_config, runtime_dir
from .errors import CapabilityError, ExternalActionRequired
from .util import (
    atomic_write_json,
    atomic_write_text,
    canonical_hash,
    canonical_json,
    now_utc,
    read_json,
    validate_identifier,
)


def batch_capabilities(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    config = dict(config or load_config())
    rows: dict[str, Any] = {}
    for name, provider in sorted(config["providers"].items()):
        rows[name] = {
            "transport": provider["transport"],
            "billing": provider["billing"],
            "provider_async": provider["async_batch"],
            "bounded_microbatch": provider["transport"] != "codex_host",
            "max_concurrency": provider["max_concurrency"],
            "discount_verified_for_selected_transport": bool(provider["async_batch"].get("supported")),
            "subscription_is_not_api_auth": provider["billing"] == "subscription",
        }
    return rows


def plan_batch(
    tasks: Sequence[Mapping[str, Any]],
    *,
    provider_name: str,
    model: str,
    requested_mode: str = "bounded_microbatch",
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = dict(config or load_config())
    provider = config.get("providers", {}).get(provider_name)
    if not isinstance(provider, Mapping):
        raise CapabilityError(f"Unknown batch provider: {provider_name}")
    if not tasks:
        raise CapabilityError("Batch must contain at least one task")
    max_items = int(config["batching"]["max_batch_items"])
    if len(tasks) > max_items:
        raise CapabilityError(f"Batch contains {len(tasks)} items; configured maximum is {max_items}")
    async_supported = bool(provider["async_batch"].get("supported"))
    selected_mode = requested_mode
    reason = "requested mode is supported"
    if requested_mode == "provider_async" and not async_supported:
        selected_mode = "bounded_microbatch"
        reason = str(provider["async_batch"].get("reason", "provider async batch is unsupported"))
    if provider["billing"] == "subscription":
        selected_mode = "bounded_microbatch"
        reason = (
            "OAuth subscription CLI work is isolated and serialized; this is not an API batch discount"
        )
    normalized_tasks = []
    for index, task in enumerate(tasks):
        if not isinstance(task, Mapping) or not str(task.get("task", "")).strip():
            raise CapabilityError(f"Batch task {index} must contain nonempty task text")
        normalized_tasks.append(
            {
                "custom_id": str(task.get("custom_id") or f"task-{index:04d}"),
                "task_sha256": canonical_hash({"task": task["task"], "context": task.get("context", "")}),
                "task": str(task["task"]),
                "context": str(task.get("context", "")),
            }
        )
    plan = {
        "provider": provider_name,
        "model": model,
        "requested_mode": requested_mode,
        "selected_mode": selected_mode,
        "selection_reason": reason,
        "task_count": len(normalized_tasks),
        "tasks": normalized_tasks,
        "max_concurrency": 1 if provider["billing"] == "subscription" else int(provider["max_concurrency"]),
        "shared_prompt_prefix": bool(config["batching"]["shared_prompt_prefix"]),
        "prompt_cache_keys": bool(config["batching"]["prompt_cache_keys"]),
        "cost_statement": (
            "A discount is reported only for a provider-documented async Batch API. "
            "Concurrency alone is not called a discount and subscription marginal cost remains unknown."
        ),
    }
    plan["batch_plan_sha256"] = canonical_hash(plan)
    return plan


def _batch_dir(batch_id: str) -> Path:
    return runtime_dir() / "batches" / validate_identifier(batch_id, "batch_id")


def prepare_provider_batch(
    tasks: Sequence[Mapping[str, Any]],
    *,
    provider_name: str,
    model: str,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = dict(config or load_config())
    plan = plan_batch(tasks, provider_name=provider_name, model=model, requested_mode="provider_async", config=config)
    if plan["selected_mode"] != "provider_async":
        raise CapabilityError(f"Provider async batch is unavailable: {plan['selection_reason']}")
    provider = config["providers"][provider_name]
    transport = provider["transport"]
    requests: list[dict[str, Any]] = []
    for item in plan["tasks"]:
        if transport == "openai_responses":
            requests.append(
                {
                    "custom_id": item["custom_id"],
                    "method": "POST",
                    "url": "/v1/responses",
                    "body": {"model": model, "input": item["task"], "reasoning": {"effort": "xhigh"}},
                }
            )
        elif transport == "anthropic_messages":
            requests.append(
                {
                    "custom_id": item["custom_id"],
                    "params": {
                        "model": model,
                        "max_tokens": 32768,
                        "messages": [{"role": "user", "content": item["task"]}],
                    },
                }
            )
        else:
            raise CapabilityError(f"Provider transport {transport} has no async batch serializer")
    batch_id = canonical_hash({"provider": provider_name, "model": model, "requests": requests})[:24]
    directory = _batch_dir(batch_id)
    directory.mkdir(parents=True, exist_ok=True)
    if transport == "openai_responses":
        bundle_text = "".join(canonical_json(item) + "\n" for item in requests)
        bundle_name = "requests.jsonl"
    else:
        bundle_text = canonical_json({"requests": requests}) + "\n"
        bundle_name = "requests.json"
    bundle_path = directory / bundle_name
    if bundle_path.exists() and bundle_path.read_text(encoding="utf-8") != bundle_text:
        raise CapabilityError("Immutable batch bundle collision")
    atomic_write_text(bundle_path, bundle_text)
    manifest = {
        "schema_version": 1,
        "batch_id": batch_id,
        "provider": provider_name,
        "transport": transport,
        "model": model,
        "status": "prepared",
        "bundle": bundle_name,
        "bundle_sha256": canonical_hash(requests),
        "task_count": len(requests),
        "created_at": now_utc(),
        "remote": None,
        "plan": plan,
    }
    atomic_write_json(directory / "manifest.json", manifest)
    return {**manifest, "directory": str(directory), "requires_submission_confirmation": True}


def _request_json(
    request: urllib.request.Request,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 60,
) -> dict[str, Any]:
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
        raise CapabilityError(f"Provider batch request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise CapabilityError("Provider batch response was not a JSON object")
    return payload


def _multipart_file(path: Path, *, purpose: str) -> tuple[bytes, str]:
    boundary = f"fusion-drive-{uuid.uuid4().hex}"
    data = path.read_bytes()
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\n{purpose}\r\n".encode(),
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
            "Content-Type: application/jsonl\r\n\r\n"
        ).encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), boundary


def submit_provider_batch(
    batch_id: str,
    *,
    confirmed: bool,
    config: Mapping[str, Any] | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    if not confirmed:
        raise ExternalActionRequired("Provider batch submission is billable and requires confirmed=true")
    config = dict(config or load_config())
    directory = _batch_dir(batch_id)
    manifest = read_json(directory / "manifest.json")
    if manifest["status"] not in {"prepared", "submitted"}:
        raise CapabilityError(f"Batch {batch_id} cannot be submitted from status {manifest['status']}")
    if manifest["status"] == "submitted":
        return manifest
    provider = config["providers"][manifest["provider"]]
    env_name = provider["auth"].get("api_key_env")
    api_key = os.environ.get(str(env_name), "")
    if not api_key:
        raise CapabilityError(f"Required API-key environment variable is unavailable: {env_name}")
    base_url = str(provider["base_url"]).rstrip("/")
    transport = manifest["transport"]
    bundle_path = directory / manifest["bundle"]
    if transport == "openai_responses":
        body, boundary = _multipart_file(bundle_path, purpose="batch")
        upload = _request_json(
            urllib.request.Request(
                f"{base_url}/files",
                data=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            ),
            opener=opener,
        )
        file_id = upload.get("id")
        if not isinstance(file_id, str):
            raise CapabilityError("OpenAI file upload returned no file id")
        create_body = json.dumps(
            {"input_file_id": file_id, "endpoint": "/v1/responses", "completion_window": "24h"}
        ).encode()
        remote = _request_json(
            urllib.request.Request(
                f"{base_url}/batches",
                data=create_body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            ),
            opener=opener,
        )
    elif transport == "anthropic_messages":
        remote = _request_json(
            urllib.request.Request(
                f"{base_url}/messages/batches",
                data=bundle_path.read_bytes(),
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                method="POST",
            ),
            opener=opener,
        )
    else:
        raise CapabilityError(f"Unsupported provider batch transport: {transport}")
    remote_id = remote.get("id")
    if not isinstance(remote_id, str):
        raise CapabilityError("Provider batch response returned no id")
    manifest["status"] = "submitted"
    manifest["submitted_at"] = now_utc()
    manifest["remote"] = {"id": remote_id, "status": remote.get("status") or remote.get("processing_status")}
    atomic_write_json(directory / "manifest.json", manifest)
    return manifest


def provider_batch_status(
    batch_id: str,
    *,
    online: bool = False,
    config: Mapping[str, Any] | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    config = dict(config or load_config())
    directory = _batch_dir(batch_id)
    manifest = read_json(directory / "manifest.json")
    if not online or manifest.get("status") != "submitted":
        return manifest
    provider = config["providers"][manifest["provider"]]
    env_name = provider["auth"].get("api_key_env")
    api_key = os.environ.get(str(env_name), "")
    if not api_key:
        raise CapabilityError(f"Required API-key environment variable is unavailable: {env_name}")
    remote_id = manifest["remote"]["id"]
    base_url = str(provider["base_url"]).rstrip("/")
    if manifest["transport"] == "openai_responses":
        url = f"{base_url}/batches/{remote_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
    else:
        url = f"{base_url}/messages/batches/{remote_id}"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    remote = _request_json(urllib.request.Request(url, headers=headers), opener=opener)
    manifest["remote"] = {
        "id": remote_id,
        "status": remote.get("status") or remote.get("processing_status"),
        "request_counts": remote.get("request_counts"),
    }
    terminal = {"completed", "ended", "expired", "cancelled", "failed"}
    if str(manifest["remote"]["status"]).lower() in terminal:
        manifest["status"] = str(manifest["remote"]["status"]).lower()
    atomic_write_json(directory / "manifest.json", manifest)
    return manifest


def execute_microbatch(
    items: Sequence[Any],
    worker: Callable[[Any], Any],
    *,
    max_concurrency: int,
) -> list[dict[str, Any]]:
    if max_concurrency < 1:
        raise CapabilityError("max_concurrency must be at least one")
    results: list[dict[str, Any] | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        futures = {executor.submit(worker, item): index for index, item in enumerate(items)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = {"ok": True, "value": future.result()}
            except Exception as exc:
                results[index] = {"ok": False, "error": str(exc)}
    return [item for item in results if item is not None]
