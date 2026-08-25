"""Small deterministic and atomic utilities."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, MutableMapping

from .errors import ConfigurationError, LockTimeout


SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SECRET_KEY_PARTS = ("api_key", "access_token", "refresh_token", "password", "private_key", "credential")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def json_copy(value: Any) -> Any:
    return copy.deepcopy(value)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_identifier(value: str, label: str = "identifier") -> str:
    if not SAFE_IDENTIFIER.fullmatch(value):
        raise ConfigurationError(f"Invalid {label}: {value!r}")
    return value


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigurationError(f"Missing JSON file: {path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"Expected a JSON object in {path}")
    return value


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, mode)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Mapping[str, Any], *, mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", mode=mode)


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = json_copy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = json_copy(value)
    return merged


def deep_get(value: Mapping[str, Any], dotted_path: str) -> Any:
    cursor: Any = value
    for part in dotted_path.split("."):
        if not isinstance(cursor, Mapping) or part not in cursor:
            raise ConfigurationError(f"Unknown configuration path: {dotted_path}")
        cursor = cursor[part]
    return json_copy(cursor)


def deep_set(value: MutableMapping[str, Any], dotted_path: str, new_value: Any) -> None:
    parts = dotted_path.split(".")
    if not all(parts):
        raise ConfigurationError("Configuration path cannot be empty")
    cursor: MutableMapping[str, Any] = value
    for part in parts[:-1]:
        child = cursor.get(part)
        if child is None:
            child = {}
            cursor[part] = child
        if not isinstance(child, MutableMapping):
            raise ConfigurationError(f"Configuration path crosses a scalar: {dotted_path}")
        cursor = child
    cursor[parts[-1]] = json_copy(new_value)


def redact(value: Any, key: str = "") -> Any:
    lowered = key.lower()
    if any(part in lowered for part in SECRET_KEY_PARTS) and not lowered.endswith("_env"):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(item_key): redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    return value


def reject_plaintext_secrets(value: Any, path: str = "") -> list[str]:
    errors: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            lowered = str(key).lower()
            if any(part in lowered for part in SECRET_KEY_PARTS) and not lowered.endswith("_env"):
                errors.append(f"{child_path} may not store a credential; use an environment-variable name or CLI OAuth")
            errors.extend(reject_plaintext_secrets(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(reject_plaintext_secrets(child, f"{path}[{index}]"))
    return errors


LOCK_TIMEOUT_SECONDS = 120.0
LOCK_POLL_SECONDS = 0.05


@contextmanager
def exclusive_lock(path: Path, *, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Take an exclusive lock, giving up rather than blocking forever.

    A blocking flock let one wedged provider CLI (subprocess timeout is 1800s)
    stall every queued caller with nothing to point at. The holder writes its
    identity into the lock file so a timeout can name who is holding it.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"Timed out after {timeout:.0f}s waiting for {path.name}; "
                        f"held by {_lock_holder(handle)}"
                    ) from None
                time.sleep(LOCK_POLL_SECONDS)
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": os.getpid(), "acquired_at": now_utc()}))
            handle.flush()
            yield
        finally:
            try:
                handle.seek(0)
                handle.truncate()
                handle.flush()
            except OSError:
                pass
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def read_only_existing_lock(
    path: Path,
    *,
    timeout: float = LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Lease an existing lock file without creating, truncating, or chmodding it."""

    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"Read-only lock must be an existing file: {path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ConfigurationError(f"Unable to open read-only lock: {path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ConfigurationError(
                f"Read-only lock must remain a regular file: {path}"
            )
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"Timed out after {timeout:.0f}s waiting for {path.name}"
                    ) from None
                time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _lock_holder(handle: Any) -> str:
    """Describe the current lock holder for a timeout message, best effort."""

    try:
        handle.seek(0)
        record = json.loads(handle.read() or "{}")
    except (OSError, ValueError):
        return "an unknown process"
    pid = record.get("pid")
    if not pid:
        return "an unknown process"
    return f"pid {pid} since {record.get('acquired_at', 'an unknown time')}"
