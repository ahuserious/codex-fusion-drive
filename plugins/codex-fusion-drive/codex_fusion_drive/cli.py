"""Small local CLI for offline reports and deterministic evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from . import __version__
from .auto_eval import generate_auto_eval
from .batching import batch_capabilities
from .capabilities import capability_probe
from .config import effective_config_report, load_config, validate_config
from .lifecycle import lifecycle_summary
from .presets import resolve_preset
from .report import workflow_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cfd", description="Codex Fusion Drive control plane")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("config-show")
    subparsers.add_parser("workflow-report").add_argument("--profile")
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--profile")
    preset = subparsers.add_parser("preset")
    preset.add_argument("name")
    lifecycle = subparsers.add_parser("lifecycle")
    lifecycle.add_argument("workflow_id")
    auto_eval = subparsers.add_parser("auto-eval")
    auto_eval.add_argument("--evidence", required=True)
    auto_eval.add_argument("--output")
    settings = subparsers.add_parser("settings")
    settings.add_argument("action", nargs="?", default="show")
    settings.add_argument("rest", nargs="*")
    return parser


def dispatch(args: argparse.Namespace) -> object:
    if args.command == "config-show":
        return effective_config_report()
    if args.command == "workflow-report":
        return workflow_report(profile_name=args.profile)
    if args.command == "doctor":
        config = load_config(validate=False)
        errors = validate_config(config)
        capabilities = capability_probe(
            config=config,
            profile_name=args.profile,
        )
        return {
            "ok": not errors and capabilities["readiness"]["ok"],
            "version": __version__,
            "config": {"ok": not errors, "errors": errors},
            "capabilities": capabilities,
            "batching": batch_capabilities(config),
        }
    if args.command == "preset":
        return resolve_preset(args.name)
    if args.command == "lifecycle":
        return lifecycle_summary(args.workflow_id)
    if args.command == "auto-eval":
        evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
        if not isinstance(evidence, dict):
            raise ValueError("Evidence file must contain a JSON object")
        return generate_auto_eval(evidence, output_path=args.output)
    if args.command == "settings":
        from .e2e_policy import cmd_settings, format_e2e_policy, load_e2e_policy
        from .config import load_config, propose_config, approve_config
        config = load_config()
        action = args.action
        if action == "show":
            return {"e2e_policy": load_e2e_policy(config)}
        if action == "propose":
            policy = load_e2e_policy(config)
            proposal = propose_config({"e2e_policy": policy}, rationale="cfd settings propose")
            return {"proposal_hash": proposal["proposal_hash"], "e2e_policy": policy}
        if action == "apply" and args.rest:
            return approve_config(args.rest[0], confirmed=True)
        return {"e2e_policy": load_e2e_policy(config), "hint": "show|propose|apply HASH"}
    raise ValueError(f"Unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    result = dispatch(build_parser().parse_args(argv))
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0
