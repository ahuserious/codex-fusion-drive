"""Safe Claude Code, Grok, and Codex CLI OAuth adapters.

The adapters intentionally delegate token ownership to each CLI/keychain. API
environment variables are removed from child processes so an API key cannot
silently override the requested subscription OAuth path. Stripping
OPENAI_API_KEY matters most for Codex, which would otherwise bill a metered
API account instead of the ChatGPT subscription the seat asked for.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from relentless_inception.types import ModelResponse, Usage

from .config import load_config, runtime_dir
from .errors import CapabilityError
from .fallback import resolve_model
from .reasoning import normalize_reasoning
from .util import atomic_write_text, canonical_json, exclusive_lock, text_hash


EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
TOKEN_PATTERN = re.compile(r"\b(?:sk|xai|oauth|bearer)[-_A-Za-z0-9.]{12,}\b", re.IGNORECASE)
# The Grok CLI names two of these in camelCase. Omitting its spellings made
# _is_result_envelope reject the envelope entirely, so the whole telemetry
# payload — cost, session ids, the model's private reasoning — was canonicalised
# and returned as if it were the seat's answer.
ENVELOPE_FIELDS = {
    "content",
    "error",
    "is_error",
    "message",
    "response",
    "result",
    "structured_output",
    "structuredOutput",
    "subtype",
    "text",
}
STRUCTURED_OUTPUT_FIELDS = ("structured_output", "structuredOutput")
OAUTH_API_KEY_ENVIRONMENTS = ("ANTHROPIC_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY")
CLI_OAUTH_TRANSPORTS = frozenset({"claude_cli_oauth", "grok_cli_oauth", "codex_cli_oauth"})


def _safe_error(value: str, limit: int = 500) -> str:
    value = EMAIL_PATTERN.sub("<redacted-email>", value)
    value = TOKEN_PATTERN.sub("<redacted-token>", value)
    return value.strip()[:limit]


def oauth_instructions(provider_name: str, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    config = dict(config or load_config())
    provider = config.get("providers", {}).get(provider_name)
    if not isinstance(provider, Mapping):
        raise CapabilityError(f"Unknown provider: {provider_name}")
    transport = provider.get("transport")
    api_env = provider.get("auth", {}).get("api_key_env")
    unset_flags = " ".join(
        f"-u {environment_name}"
        for environment_name in OAUTH_API_KEY_ENVIRONMENTS
    )
    if transport == "claude_cli_oauth":
        command = f"env {unset_flags} claude"
        login = "Run /login in the interactive Claude Code session and choose the subscription account."
    elif transport == "grok_cli_oauth":
        command = f"env {unset_flags} grok"
        login = "Run /login in the interactive Grok session if the CLI reports that OAuth is not active."
    elif transport == "codex_cli_oauth":
        command = f"env {unset_flags} codex"
        login = "Run `codex login` and choose Sign in with ChatGPT so the seat bills the subscription."
    else:
        raise CapabilityError(f"Provider {provider_name} is not a CLI OAuth provider")
    return {
        "provider": provider_name,
        "command": command,
        "interactive_step": login,
        "identity_hint_policy": "The plugin does not store an email address, X handle, token, cookie, or keychain path.",
        "api_override_removed": api_env,
        "api_overrides_removed": list(OAUTH_API_KEY_ENVIRONMENTS),
        "verification": "Use oauth_status with online=true after completing the interactive login.",
    }


def oauth_status(
    provider_name: str,
    *,
    online: bool = False,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = dict(config or load_config())
    provider = config.get("providers", {}).get(provider_name)
    if not isinstance(provider, Mapping):
        raise CapabilityError(f"Unknown provider: {provider_name}")
    transport = provider.get("transport")
    command = str(provider.get("command", ""))
    binary = shutil.which(command)
    result: dict[str, Any] = {
        "provider": provider_name,
        "transport": transport,
        "binary_path": binary,
        "online_checked": online,
        "authenticated": None,
        "identity_disclosed": False,
        "token_accessed": False,
        "billing": provider.get("billing"),
    }
    if not binary or not online:
        return result
    env = os.environ.copy()
    for environment_name in OAUTH_API_KEY_ENVIRONMENTS:
        env.pop(environment_name, None)
    if transport == "claude_cli_oauth":
        args = [binary, "auth", "status", "--json"]
    elif transport == "grok_cli_oauth":
        args = [binary, "models"]
    elif transport == "codex_cli_oauth":
        args = [binary, "login", "status"]
    else:
        raise CapabilityError(f"Provider {provider_name} is not a CLI OAuth provider")
    try:
        completed = subprocess.run(
            args,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["error"] = _safe_error(str(exc))
        return result
    result["authenticated"] = completed.returncode == 0
    result["exit_code"] = completed.returncode
    if completed.returncode:
        result["error"] = _safe_error(completed.stderr or completed.stdout)
    return result


class _CliOutputFailure(CapabilityError):
    def __init__(self, diagnostics: Mapping[str, Any]):
        self.diagnostics = dict(diagnostics)
        super().__init__(f"CLI output rejected: {canonical_json(self.diagnostics)}")


def _output_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, bytes):
        return "bytes"
    return "unknown"


def _diagnostics(
    raw_output: str,
    *,
    value: Any,
    category: str,
    exit_status: int | str,
) -> dict[str, Any]:
    return {
        "category": category,
        "type": _output_type(value),
        "length": len(raw_output),
        "sha256": text_hash(raw_output),
        "exit_status": exit_status,
    }


def _decode_json_output(raw_output: str) -> tuple[Any, bool]:
    if not raw_output.strip():
        raise _CliOutputFailure(
            _diagnostics(
                raw_output,
                value=raw_output,
                category="empty_output",
                exit_status=0,
            )
        )
    try:
        return json.loads(raw_output), False
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        values: list[Any] = []
        cursor = 0
        while cursor < len(raw_output):
            while cursor < len(raw_output) and raw_output[cursor].isspace():
                cursor += 1
            if cursor >= len(raw_output):
                break
            try:
                value, cursor = decoder.raw_decode(raw_output, cursor)
            except json.JSONDecodeError as exc:
                raise _CliOutputFailure(
                    _diagnostics(
                        raw_output,
                        value=raw_output,
                        category="malformed_json",
                        exit_status=0,
                    )
                ) from exc
            values.append(value)
        if len(values) < 2:
            raise _CliOutputFailure(
                _diagnostics(
                    raw_output,
                    value=raw_output,
                    category="malformed_json",
                    exit_status=0,
                )
            )
        return values, True


def _is_result_envelope(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("type") in {"result", "error"}:
        return True
    return bool(ENVELOPE_FIELDS.intersection(value))


def _envelope_metadata(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, list) and value and isinstance(value[-1], Mapping):
        return value[-1]
    return {}


def _canonical_model_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    return canonical_json(value)


def _extract_text(
    payload: Any,
    *,
    raw_output: str,
    response_schema: Mapping[str, Any] | None,
    is_json_sequence: bool,
) -> tuple[str, Mapping[str, Any]]:
    selected = payload
    if is_json_sequence:
        if not isinstance(payload, list) or not payload or not _is_result_envelope(payload[-1]):
            raise _CliOutputFailure(
                _diagnostics(
                    raw_output,
                    value=payload,
                    category="malformed_sequence",
                    exit_status=0,
                )
            )
        selected = payload[-1]
    elif (
        isinstance(payload, list)
        and payload
        and isinstance(payload[-1], Mapping)
        and payload[-1].get("type") in {"result", "error"}
    ):
        selected = payload[-1]

    if selected is None:
        raise _CliOutputFailure(
            _diagnostics(
                raw_output,
                value=selected,
                category="null_output",
                exit_status=0,
            )
        )

    if not isinstance(selected, Mapping) or not _is_result_envelope(selected):
        text = _canonical_model_text(selected)
        if text is None:
            raise _CliOutputFailure(
                _diagnostics(
                    raw_output,
                    value=selected,
                    category="empty_output",
                    exit_status=0,
                )
            )
        return text, {}

    envelope = selected
    envelope_type = str(envelope.get("type", "")).lower()
    envelope_subtype = str(envelope.get("subtype", "")).lower()
    if (
        envelope.get("is_error") is True
        or envelope_type == "error"
        or "error" in envelope_subtype
        or ("error" in envelope and envelope.get("error") not in (None, False, ""))
    ):
        raise _CliOutputFailure(
            _diagnostics(
                raw_output,
                value=envelope,
                category="error_envelope",
                exit_status=0,
            )
        )

    structured_field = next(
        (field for field in STRUCTURED_OUTPUT_FIELDS if field in envelope), None
    )
    if response_schema is not None and structured_field is not None:
        structured_text = _canonical_model_text(envelope.get(structured_field))
        if structured_text is None:
            raise _CliOutputFailure(
                _diagnostics(
                    raw_output,
                    value=envelope.get(structured_field),
                    category="null_or_empty_structured_output",
                    exit_status=0,
                )
            )
        return structured_text, envelope

    output_field_present = False
    for key in ("result", "response", "text", "content", "message"):
        if key not in envelope:
            continue
        output_field_present = True
        value = envelope.get(key)
        text = _canonical_model_text(value)
        if text is not None:
            return text, envelope

    raise _CliOutputFailure(
        _diagnostics(
            raw_output,
            value=envelope,
            category=(
                "null_or_empty_result"
                if output_field_present
                else "malformed_envelope"
            ),
            exit_status=0,
        )
    )


def _codex_usage_fields(usage: Mapping[str, Any]) -> dict[str, Any]:
    """Rename Codex's two divergent token counters onto the envelope names.

    Only keys Codex actually sent are copied: `_usage` treats a present-but-
    unparseable counter as invalid metadata, so absent stays absent.
    """
    mapped: dict[str, Any] = {}
    for source, target in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("reasoning_output_tokens", "reasoning_tokens"),
        ("cached_input_tokens", "cached_tokens"),
    ):
        if source in usage:
            mapped[target] = usage[source]
    return mapped


def _codex_result(raw_output: str, last_message: str) -> tuple[str, dict[str, Any]]:
    """Assemble a result envelope from a `codex exec --json` run.

    Codex does not print one JSON document like the Claude and Grok CLIs. It
    streams JSONL events to stdout and writes only the final assistant message
    to `--output-last-message`, so the text and the usage/error metadata have
    to be recombined from two places before the shared failure handling can
    treat it like any other CLI envelope.
    """
    events: list[Mapping[str, Any]] = []
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Codex interleaves human-readable banner lines with the JSONL
            # stream; a non-JSON line is not itself a failure.
            continue
        if isinstance(event, Mapping):
            events.append(event)

    for event in events:
        if event.get("type") in {"error", "turn.failed"}:
            raise _CliOutputFailure(
                _diagnostics(raw_output, value=event, category="error_envelope", exit_status=0)
            )

    metadata: dict[str, Any] = {}
    for event in events:
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), Mapping):
            metadata["usage"] = _codex_usage_fields(event["usage"])
        elif event.get("type") == "thread.started" and event.get("thread_id"):
            metadata["request_id"] = event["thread_id"]

    text = last_message.strip()
    if not text:
        raise _CliOutputFailure(
            _diagnostics(raw_output, value=last_message, category="empty_output", exit_status=0)
        )
    return text, metadata


def _usage(payload: Mapping[str, Any], *, cached_is_subset: bool = True) -> Usage:
    """Normalise one CLI's usage block.

    Most providers report cached tokens as a detail *inside* input_tokens. The
    Grok CLI reports them alongside: measured on a live call, input 64561 +
    cache_read 128 + output 19 equals its own total of 64708. Leaving that
    unnormalised breaks the cached <= input invariant every budget check relies
    on, so a disjoint counter is folded into input here.
    """

    raw = payload.get("usage", {})
    if not isinstance(raw, Mapping):
        raw = {}

    invalid_usage = False

    def integer(*names: str) -> tuple[int, bool]:
        nonlocal invalid_usage
        for name in names:
            if name not in raw:
                continue
            value = raw.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value, True
            invalid_usage = True
            return 0, False
        return 0, False

    input_tokens, input_complete = integer("input_tokens", "inputTokens")
    output_tokens, output_complete = integer("output_tokens", "outputTokens")
    reasoning_tokens, _ = integer("reasoning_tokens", "reasoningTokens")
    cached_tokens, _ = integer("cached_tokens", "cache_read_input_tokens")
    input_output_usage_complete = input_complete and output_complete
    usage_error = None
    if invalid_usage:
        usage_error = "CLI returned invalid usage metadata"
    elif raw and not input_output_usage_complete:
        usage_error = "CLI returned incomplete usage metadata"

    if not cached_is_subset and cached_tokens:
        input_tokens += cached_tokens

    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        tool_calls=0,
        cost_usd=None,
        unknown_cost_fail_closed=False,
        input_output_usage_complete=input_output_usage_complete,
        raw_usage_invalid=invalid_usage,
        accounting_error=usage_error,
    )


class SubscriptionCliAdapter:
    """Execute one isolated, tool-free CLI completion under a principal lock."""

    def __init__(self, config: Mapping[str, Any] | None = None):
        self.config = dict(config or load_config())

    def complete(
        self,
        seat_name: str,
        *,
        system: str,
        prompt: str,
        response_schema: Mapping[str, Any] | None = None,
        schema_name: str = "structured_response",
        before_attempt: Callable[[], None] | None = None,
        on_semantic_failure_response: Callable[[ModelResponse], None] | None = None,
    ) -> ModelResponse:
        seat = self.config.get("seats", {}).get(seat_name)
        if not isinstance(seat, Mapping):
            raise CapabilityError(f"Unknown OAuth seat: {seat_name}")
        provider_name = str(seat.get("provider"))
        provider = self.config.get("providers", {}).get(provider_name)
        if not isinstance(provider, Mapping):
            raise CapabilityError(f"Seat {seat_name} references unknown provider")
        transport = provider.get("transport")
        if transport not in CLI_OAUTH_TRANSPORTS:
            raise CapabilityError(f"Seat {seat_name} is not a CLI OAuth seat")
        binary = shutil.which(str(provider.get("command", "")))
        if not binary:
            raise CapabilityError(f"CLI binary is unavailable for {provider_name}")

        configured_model = str(seat["model"])
        model = resolve_model(configured_model, self.config)
        reasoning = normalize_reasoning(provider, model, str(seat.get("reasoning", "xhigh")))
        timeout = float(seat.get("timeout_seconds", provider.get("request_timeout_seconds", 1800)))
        env = os.environ.copy()
        api_env = provider.get("auth", {}).get("api_key_env")
        for environment_name in OAUTH_API_KEY_ENVIRONMENTS:
            env.pop(environment_name, None)
        combined_prompt = f"{system.strip()}\n\nUSER TASK\n{prompt.strip()}\n"

        jobs_root = runtime_dir() / "oauth-jobs"
        jobs_root.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()

        def base_route() -> dict[str, Any]:
            route = {
                "transport": transport,
                "auth_mode": "cli_oauth_keychain",
                "billing": "subscription",
                "reasoning": reasoning,
                # Claude takes an explicit empty tool list and Grok takes a
                # deny-all rule plus disabled subagents/search/memory. Codex has
                # no equivalent switch, so it is confined by sandbox instead
                # and must not claim the stronger guarantee.
                "tools_disabled": transport != "codex_cli_oauth",
                "api_key_override_removed": bool(api_env),
                "api_key_overrides_removed": list(OAUTH_API_KEY_ENVIRONMENTS),
                "schema_name": schema_name,
            }
            if configured_model != model:
                # Keep the swap auditable: the receipt must not read as though
                # the configured model actually ran.
                route["model_fallback"] = {"from": configured_model, "to": model}
            if transport == "codex_cli_oauth":
                route["sandbox_policy"] = "read-only"
                route["web_search_disabled"] = True
                route["user_config_ignored"] = True
            return route

        def record_semantic_failure(
            diagnostics: Mapping[str, Any],
            *,
            metadata: Mapping[str, Any] | None = None,
        ) -> Exception | None:
            if on_semantic_failure_response is None:
                return None
            route = base_route()
            route["semantic_failure"] = dict(diagnostics)
            cached_is_subset = transport != "grok_cli_oauth"
            try:
                on_semantic_failure_response(
                    ModelResponse(
                        text="",
                        provider=provider_name,
                        requested_model=model,
                        actual_model=model,
                        usage=_usage(
                            metadata or {},
                            cached_is_subset=cached_is_subset,
                        ),
                        latency_seconds=time.monotonic() - started,
                        request_id=None,
                        route=route,
                        raw_status="failed",
                    )
                )
            except Exception as exc:
                # The callback persists already-billable evidence before it can
                # raise a budget/accounting error. Keep that secondary failure
                # diagnostic without replacing the provider's original semantic
                # failure, which is what callers need in order to retry safely.
                return exc
            return None

        def attach_receipt_failure(
            failure: _CliOutputFailure,
            receipt_failure: Exception | None,
        ) -> None:
            if receipt_failure is None:
                return
            note = (
                "Semantic-failure receipt callback also raised "
                f"{type(receipt_failure).__name__}: {_safe_error(str(receipt_failure))}"
            )
            failure.receipt_callback_note = note
            add_note = getattr(failure, "add_note", None)
            if callable(add_note):
                add_note(note)

        with exclusive_lock(runtime_dir() / "locks" / f"{provider_name}.lock"):
            with tempfile.TemporaryDirectory(prefix=f"{provider_name}-", dir=jobs_root) as temporary:
                temporary_path = Path(temporary)
                os.chmod(temporary_path, 0o700)
                prompt_path = temporary_path / "prompt.txt"
                atomic_write_text(prompt_path, combined_prompt, mode=0o600)
                codex_message_path = temporary_path / "last-message.txt"
                codex_schema_path = temporary_path / "output-schema.json"
                if transport == "claude_cli_oauth":
                    args = [
                        binary,
                        "--print",
                        "--model",
                        model,
                        "--effort",
                        reasoning["effective"],
                        "--output-format",
                        "json",
                        "--tools",
                        "",
                        "--safe-mode",
                        "--no-session-persistence",
                    ]
                    if response_schema:
                        args.extend(["--json-schema", json.dumps(response_schema, separators=(",", ":"), sort_keys=True)])
                    prompt_input = prompt_path.read_text(encoding="utf-8")
                    subprocess_stdin = None
                elif transport == "codex_cli_oauth":
                    # --ignore-user-config keeps the seat deterministic and
                    # immune to a broken or model-overriding ~/.codex/config.toml;
                    # auth still resolves from CODEX_HOME.
                    args = [
                        binary,
                        "exec",
                        "--ignore-user-config",
                        "--model",
                        model,
                        "-c",
                        f"model_reasoning_effort={json.dumps(reasoning['effective'])}",
                        "-c",
                        "tools.web_search=false",
                        "--sandbox",
                        "read-only",
                        "--skip-git-repo-check",
                        "--ephemeral",
                        "--json",
                        "--output-last-message",
                        str(codex_message_path),
                    ]
                    if response_schema:
                        atomic_write_text(
                            codex_schema_path,
                            json.dumps(response_schema, separators=(",", ":"), sort_keys=True),
                            mode=0o600,
                        )
                        args.extend(["--output-schema", str(codex_schema_path)])
                    # "-" makes Codex read the prompt from stdin instead of argv,
                    # keeping it out of the process table.
                    args.append("-")
                    prompt_input = prompt_path.read_text(encoding="utf-8")
                    subprocess_stdin = None
                else:
                    args = [
                        binary,
                        "--prompt-file",
                        str(prompt_path),
                        "--model",
                        model,
                        "--reasoning-effort",
                        reasoning["effective"],
                        "--output-format",
                        "json",
                        # `--tools` is an ALLOW list, so an empty value is not a
                        # deny — it simply sets no override and every built-in
                        # tool stays live. Verified against the Grok CLI: only a
                        # deny rule actually blocks execution.
                        "--deny",
                        "*",
                        "--no-subagents",
                        "--disable-web-search",
                        "--no-memory",
                        "--permission-mode",
                        "plan",
                        "--cwd",
                        str(temporary_path),
                    ]
                    if response_schema:
                        args.extend(["--json-schema", json.dumps(response_schema, separators=(",", ":"), sort_keys=True)])
                    prompt_input = None
                    subprocess_stdin = subprocess.DEVNULL
                if before_attempt is not None:
                    before_attempt()
                try:
                    completed = subprocess.run(
                        args,
                        input=prompt_input,
                        stdin=subprocess_stdin,
                        env=env,
                        cwd=temporary_path,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    timeout_output = exc.stdout or exc.stderr or ""
                    if isinstance(timeout_output, bytes):
                        timeout_output = timeout_output.decode("utf-8", errors="replace")
                    diagnostics = _diagnostics(
                        str(timeout_output),
                        value=timeout_output,
                        category="timeout",
                        exit_status="timeout",
                    )
                    failure = _CliOutputFailure(diagnostics)
                    attach_receipt_failure(
                        failure,
                        record_semantic_failure(diagnostics),
                    )
                    raise failure from exc
                if completed.returncode:
                    process_output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
                    diagnostics = _diagnostics(
                        process_output,
                        value=process_output,
                        category="nonzero_exit",
                        exit_status=int(completed.returncode),
                    )
                    failure = _CliOutputFailure(diagnostics)
                    attach_receipt_failure(
                        failure,
                        record_semantic_failure(diagnostics),
                    )
                    raise failure
                raw_output = completed.stdout or ""
                try:
                    if transport == "codex_cli_oauth":
                        last_message = (
                            codex_message_path.read_text(encoding="utf-8")
                            if codex_message_path.exists()
                            else ""
                        )
                        text, metadata = _codex_result(raw_output, last_message)
                    else:
                        payload, is_json_sequence = _decode_json_output(raw_output)
                        text, metadata = _extract_text(
                            payload,
                            raw_output=raw_output,
                            response_schema=response_schema,
                            is_json_sequence=is_json_sequence,
                        )
                except _CliOutputFailure as exc:
                    metadata = (
                        _envelope_metadata(payload)
                        if "payload" in locals()
                        else {}
                    )
                    attach_receipt_failure(
                        exc,
                        record_semantic_failure(
                            exc.diagnostics,
                            metadata=metadata,
                        ),
                    )
                    raise
                actual_model = str(metadata.get("model", model))
                cached_is_subset = transport != "grok_cli_oauth"
                request_id = metadata.get("request_id") or metadata.get("session_id")
                return ModelResponse(
                    text=text,
                    provider=provider_name,
                    requested_model=model,
                    actual_model=actual_model,
                    usage=_usage(metadata, cached_is_subset=cached_is_subset),
                    latency_seconds=time.monotonic() - started,
                    request_id=str(request_id) if request_id else None,
                    route=base_route(),
                )
