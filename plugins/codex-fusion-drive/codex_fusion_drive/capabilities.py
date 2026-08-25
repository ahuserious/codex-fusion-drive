"""Non-invasive local capability and advanced-workflow probes."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import load_config
from .e2e_policy import named_codex_oauth_blocker
from .errors import ConfigurationError
from .util import canonical_hash, json_copy


def _skill_status(path: str) -> dict[str, Any]:
    skill = Path(path).expanduser()
    return {"path": str(skill), "available": skill.is_file(), "readable": skill.is_file()}


def _required_seats(
    config: Mapping[str, Any],
    profile_name: str,
) -> list[str]:
    profile = config["profiles"][profile_name]
    engine = config["engines"][profile["engine"]]
    gate_set = config["gate_sets"][profile["gate_set"]]
    seats = list(engine.get("panel", []))
    for role in ("judge", "fuser", "seat"):
        seat_name = engine.get(role)
        if seat_name:
            seats.append(str(seat_name))
    seats.extend(str(item) for item in gate_set.get("reviewers", []))
    return list(dict.fromkeys(seats))


def capability_probe(
    *,
    host_mcp_tools: Iterable[str] = (),
    config: Mapping[str, Any] | None = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    config = dict(config or load_config())
    profile_name = profile_name or str(config["active_profile"])
    if profile_name not in config.get("profiles", {}):
        raise ConfigurationError(f"Unknown Fusion Drive profile: {profile_name}")
    tools = sorted(set(str(item) for item in host_mcp_tools))
    host_tool_probe_supplied = bool(tools)
    gitnexus = config["integrations"]["gitnexus"]
    repo_merge = config["integrations"]["repo_merge"]
    gitnexus_mcp = any("gitnexus" in item.lower() for item in tools)
    required_seats = _required_seats(config, profile_name)
    required_providers = {
        str(config["seats"][seat_name]["provider"])
        for seat_name in required_seats
    }
    readiness_issues: list[str] = []
    providers = {}
    for name, provider in sorted(config["providers"].items()):
        command = provider.get("command")
        binary_path = shutil.which(str(command)) if command else None
        required_for_profile = name in required_providers
        auth = provider.get("auth", {})
        auth = auth if isinstance(auth, Mapping) else {}
        auth_mode = auth.get("mode")
        api_key_env = auth.get("api_key_env")
        api_key_env_present = (
            isinstance(api_key_env, str)
            and bool(api_key_env)
            and api_key_env in os.environ
        )
        providers[name] = {
            "enabled": bool(provider.get("enabled")),
            "transport": provider.get("transport"),
            "billing": provider.get("billing"),
            "binary_available": binary_path,
            "required_for_profile": required_for_profile,
            "auth_mode": auth_mode,
            "auth_checked": False,
            "api_key_env_present": api_key_env_present,
            "auth_value_accessed": False,
            "async_batch": json_copy(provider.get("async_batch", {})),
        }
        if required_for_profile and not provider.get("enabled"):
            readiness_issues.append(f"Required provider {name} is disabled")
        if required_for_profile and command and not binary_path:
            readiness_issues.append(
                f"Required provider {name} cannot resolve command {command!r}"
            )
        if required_for_profile and auth_mode == "api_key_env":
            if not isinstance(api_key_env, str) or not api_key_env:
                readiness_issues.append(
                    f"Required provider {name} has no API environment reference"
                )
            elif not api_key_env_present:
                readiness_issues.append(
                    f"Required provider {name} environment reference {api_key_env!r} is absent"
                )
    configured_host_tool = str(
        config["lifecycle"]["host_goal_creation_tool"]
    )
    host_tool_available = (
        configured_host_tool in tools if host_tool_probe_supplied else None
    )
    if host_tool_probe_supplied and not host_tool_available:
        readiness_issues.append(
            f"Configured host goal tool {configured_host_tool!r} is not exposed"
        )
    report = {
        "selected_profile": profile_name,
        "required_seats": required_seats,
        "required_providers": sorted(required_providers),
        "host_goal": {
            "configured_tool": configured_host_tool,
            "probe_supplied": host_tool_probe_supplied,
            "available": host_tool_available,
            "probe_basis": tools,
        },
        "repo_merge": {
            **_skill_status(repo_merge["skill_path"]),
            "external_writes_require_approval": repo_merge["external_writes_require_approval"],
            "destructive_actions_require_approval": repo_merge["destructive_actions_require_approval"],
        },
        "gitnexus": {
            **_skill_status(gitnexus["skill_path"]),
            "cli_path": shutil.which(gitnexus["cli_command"]),
            "mcp_exposed_by_host": gitnexus_mcp,
            "mcp_probe_basis": tools,
            "auto_install": False,
        },
        "providers": providers,
        "credential_policy": (
            "This probe does not read, refresh, print, or store OAuth tokens or API-key values. "
            "CLI OAuth and API-key billing remain separate. missing_codex_oauth is a named "
            "blocker for Sol-via-subscription seats, not a silent fallback."
        ),
        "readiness": {
            "ok": not readiness_issues,
            "issues": readiness_issues,
        },
        "named_blockers": [],
    }
    profile_blocker = named_codex_oauth_blocker(config, required_seats=required_seats)
    if profile_blocker:
        report["named_blockers"].append(profile_blocker)
        readiness_issues.append(profile_blocker)
        report["readiness"] = {"ok": False, "issues": readiness_issues}
    report["capability_sha256"] = canonical_hash(report)
    return report


def advanced_workflow_plan(
    task: str,
    *,
    repository_count: int = 1,
    requires_merge: bool = False,
    host_mcp_tools: Iterable[str] = (),
) -> dict[str, Any]:
    capabilities = capability_probe(host_mcp_tools=host_mcp_tools)
    steps: list[dict[str, Any]] = [
        {
            "step": "gitnexus_context",
            "enabled": bool(capabilities["gitnexus"]["cli_path"] or capabilities["gitnexus"]["mcp_exposed_by_host"]),
            "action": "Map symbols, dependencies, callers, and impact before edits.",
            "approval_required": False,
        }
    ]
    if repository_count > 1 or requires_merge:
        steps.append(
            {
                "step": "repo_merge",
                "enabled": capabilities["repo_merge"]["available"],
                "action": "Build a non-destructive merge plan with source/target mapping and conflict evidence.",
                "approval_required": True,
            }
        )
    steps.extend(
        [
            {
                "step": "fusion_plan",
                "enabled": True,
                "action": "Fuse independent plans and return the workflow report for confirmation.",
                "approval_required": False,
            },
            {
                "step": "external_changes",
                "enabled": True,
                "action": "Perform pushes, PRs, remote writes, or destructive operations only after explicit approval.",
                "approval_required": True,
            },
        ]
    )
    result = {
        "task": task,
        "repository_count": repository_count,
        "requires_merge": requires_merge,
        "steps": steps,
        "capabilities": capabilities,
    }
    result["plan_sha256"] = canonical_hash(result)
    return result
