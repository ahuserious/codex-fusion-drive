#!/usr/bin/env python3
"""Control-plane helper for the Fusion Drive statusline.

Subcommands:
  profile <name-or-slot>   Switch active_profile (validated propose+approve).
  mini-fuse on|off|status  Toggle the light-duty mini-fuse seats for subagent
                           and adversarial-review summarization.
  plan on|off              Fusion-plan toggle: planning runs use full fusion
                           at the configured preset level.
  preset up|down|<level>   Fusion preset ladder off→low→medium→high
                           (default high). `up`/`down` step the ladder —
                           repeated `down` reaches off.
  review up|down|<level>   Subagent review ladder off→light→exaflop
                           (default light). light = mini-fuse compression;
                           exaflop = grok45 xhigh + sol high mini panel with
                           a grok45 review judge reporting to the
                           orchestrator; auto-applies to dynamic workflows.
  config [full|open]       Show the configuration in the terminal (default:
                           colored summary; `full` = merged JSON; `open` =
                           GUI editor).
  slots [set <n> <profile>]  Show or edit the statusline hotkey slots.
  status                   One-line summary (same data the statusline shows).
  watch [job-id] [--once] [--interval <s>]
                           Live per-seat view of running fusion work. Run it as
                           a Claude Code background Bash task and it becomes an
                           openable entry in the agent view (left arrow), which
                           is the only way seat progress can surface there — an
                           MCP server cannot register agent-view entries itself.
  settings [show|doctor|interview|propose|apply-defaults|apply]
                           Claude-Code-interview-style e2e_policy TUI. Hash-bound
                           apply via propose+approve. Unattended e2e skips the
                           interview. Codex OAuth is a named prerequisite (no
                           silent fallback).

Profile and mini-fuse changes go through the plugin's own propose/approve
configuration flow, so they are schema-validated, secret-checked, and locked.
Run this yourself (or via a keybinding/`!` shell escape) — it is a deliberate
user action, which is why approval is auto-confirmed here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN_ROOT))


def settings_tui_script() -> Path:
    return PLUGIN_ROOT / "scripts" / "settings_tui.py"


def launch_settings_tui(argv: list[str]) -> int:
    script = settings_tui_script()
    if not script.is_file():
        print(f"missing {script}", file=sys.stderr)
        return 1
    return int(subprocess.call([sys.executable, str(script), *argv]))

from codex_fusion_drive.config import (  # noqa: E402
    approve_config,
    load_config,
    propose_config,
    runtime_dir,
)
from codex_fusion_drive.e2e_policy import (  # noqa: E402
    DEFAULT_E2E_POLICY,
    LOOP_ON_CI,
    STABLY_DEPTHS,
    codex_oauth_named_blocker,
    codex_oauth_status_line,
    format_e2e_policy,
    merge_e2e_policy,
    normalize_e2e_policy,
)

MINI_FUSE_SEATS = ("grok45-mini-panel", "grok45-mini-judge", "grok45-mini-fuser")
DEFAULT_SLOTS = {
    "1": "xai-claude-oauth",
    "2": "all-grok-4.5",
    "3": "maximum-intelligence",
    "4": "mini-fuse",
    "5": "exaflop-reactor",
}
PRESET_LADDER = ["off", "low", "medium", "high"]
REVIEW_LADDER = ["off", "light", "exaflop"]


def slots_path() -> Path:
    return runtime_dir() / "statusline.json"


def load_slots() -> dict[str, str]:
    try:
        data = json.loads(slots_path().read_text(encoding="utf-8"))
        slots = data.get("slots", {})
        if isinstance(slots, dict) and slots:
            return {str(k): str(v) for k, v in slots.items()}
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return dict(DEFAULT_SLOTS)


def load_statusline_config() -> dict:
    try:
        data = json.loads(slots_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_statusline_config(data: dict) -> None:
    slots_path().write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def save_slots(slots: dict[str, str]) -> None:
    data = load_statusline_config()
    data["slots"] = slots
    save_statusline_config(data)


DEFAULT_TOGGLES = {"fusion_plan": False, "preset": "high", "subagent_review": "off"}


def load_toggles() -> dict:
    merged = dict(DEFAULT_TOGGLES)
    stored = load_statusline_config().get("toggles")
    if isinstance(stored, dict):
        merged.update(stored)
    return merged


def save_toggle(key: str, value) -> None:
    data = load_statusline_config()
    stored = data.get("toggles") if isinstance(data.get("toggles"), dict) else {}
    stored[key] = value
    data["toggles"] = stored
    save_statusline_config(data)


def cmd_toggle(key: str, label: str, action: str) -> int:
    if action not in {"on", "off"}:
        print(f"usage: fusion_ctl.py {label} on|off")
        return 1
    save_toggle(key, action == "on")
    print(f"{label} → {action}")
    return 0


def _ladder_step(ladder: list[str], current: str, action: str) -> str:
    index = ladder.index(current) if current in ladder else len(ladder) - 1
    if action == "up":
        index = min(index + 1, len(ladder) - 1)
    else:
        index = max(index - 1, 0)
    return ladder[index]


def cmd_preset(action: str) -> int:
    if action in {"up", "down"}:
        level = _ladder_step(PRESET_LADDER, str(load_toggles().get("preset", "high")), action)
    elif action in PRESET_LADDER:
        level = action
    else:
        print("usage: fusion_ctl.py preset up|down|off|low|medium|high")
        return 1
    save_toggle("preset", level)
    print(f"preset → {level}")
    return 0


def _review_value(raw) -> str:
    if raw is True:
        return "light"
    if raw is False:
        return "off"
    return raw if raw in REVIEW_LADDER else "light"


def cmd_review(action: str) -> int:
    if action in {"on", "off"}:
        action = "light" if action == "on" else "off"
    if action in {"up", "down"}:
        level = _ladder_step(REVIEW_LADDER, _review_value(load_toggles().get("subagent_review")), action)
    elif action in REVIEW_LADDER:
        level = action
    else:
        print("usage: fusion_ctl.py review up|down|off|light|exaflop")
        return 1
    save_toggle("subagent_review", level)
    print(f"review → {level}")
    return 0


DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CYAN, GREEN, RED = "\033[36m", "\033[32m", "\033[31m"


def _seat_line(config: dict, seat_name: str) -> str:
    seat = config.get("seats", {}).get(seat_name, {})
    return f"{seat.get('model', '?')}@{seat.get('effective_reasoning', '?')}"


def cmd_config(mode: str = "") -> int:
    from codex_fusion_drive.config import load_config, redact, user_config_path

    default_path = PLUGIN_ROOT / "config" / "fusion-drive.default.json"
    user_path = user_config_path()
    config = load_config()

    if mode == "open":
        # GUI opener only — a terminal $EDITOR (vim/nano) would hang without a
        # tty, e.g. under the Claude Code `!` shell escape.
        import subprocess

        result = subprocess.run(["open", str(user_path)], capture_output=True, text=True)
        print(f"opened {user_path}" if result.returncode == 0
              else f"open failed: {result.stderr.strip() or result.returncode}")
        return 0
    if mode == "full":
        print(json.dumps(redact(config), indent=2, sort_keys=True))
        return 0

    active = str(config.get("active_profile"))
    print(f"{BOLD}⚛ Codex Fusion Drive config{RESET}  {DIM}(fusion config full | open){RESET}")
    print(f"{DIM}default{RESET} {default_path}")
    print(f"{DIM}user   {RESET} {user_path}")
    print()
    print(f"{BOLD}profiles{RESET}")
    for name, profile in sorted(config.get("profiles", {}).items()):
        engine = config.get("engines", {}).get(str(profile.get("engine")), {})
        marker = f"{CYAN}▶{RESET}" if name == active else " "
        if engine.get("kind") == "server_managed":
            topo = f"panel {'+'.join(engine.get('analysis_models', []))} · judge+fuse {engine.get('judge_model')}"
        else:
            panel = " + ".join(_seat_line(config, s) for s in engine.get("panel", []))
            topo = (f"panel {panel} · judge {_seat_line(config, str(engine.get('judge')))}"
                    f" · fuse {_seat_line(config, str(engine.get('fuser')))}")
        print(f" {marker} {BOLD}{name}{RESET} {DIM}[{profile.get('engine')}]{RESET}")
        print(f"      {topo}")
    print()
    print(f"{BOLD}providers{RESET}")
    for name, provider in sorted(config.get("providers", {}).items()):
        state = f"{GREEN}enabled{RESET}" if provider.get("enabled") else f"{DIM}disabled{RESET}"
        print(f"   {name:<24}{state}  {DIM}{provider.get('transport')}{RESET}")
    print()
    state = load_toggles()
    review = _review_value(state.get("subagent_review"))
    print(f"{BOLD}toggles{RESET}   plan {'on' if state.get('fusion_plan') else 'off'}"
          f" · preset {state.get('preset', 'high')} · review {review}"
          f" · mini-fuse {'on' if mini_fuse_enabled(config) else 'off'}")
    slots = load_slots()
    print(f"{BOLD}slots{RESET}     " + "  ".join(f"{k}:{v}" for k, v in sorted(slots.items())))
    policy = merge_e2e_policy(config.get("e2e_policy") if isinstance(config.get("e2e_policy"), dict) else None)
    print(
        f"{BOLD}e2e_policy{RESET} unattended {'y' if policy.get('unattended') else 'n'}"
        f" · shots {'y' if policy.get('require_screenshots_per_feature') else 'n'}"
        f" · video {'y' if policy.get('require_video_evidence') else 'n'}"
        f" · stably {policy.get('stably_depth')}"
        f" · loop_on_ci {policy.get('loop_on_ci')}"
        f" · author {policy.get('test_flow_author_style')}"
    )
    print(f"{BOLD}codex_oauth{RESET} {codex_oauth_status_line(config)}")
    return 0


def apply_change(changes: dict, rationale: str) -> None:
    proposal = propose_config(changes, rationale=rationale)
    approve_config(proposal["proposal_hash"], confirmed=True)


def _print_codex_oauth_blocker(config: dict) -> None:
    blocker = codex_oauth_named_blocker(config)
    flag = f"{RED}BLOCKER{RESET}" if blocker["named_blocker"] else f"{GREEN}ok{RESET}"
    print(f"{BOLD}codex_oauth{RESET}  {flag}  {DIM}{blocker['message']}{RESET}")
    print(f"{DIM}silent_fallback=false · required for Sol-via-subscription seats{RESET}")


def _policy_from_flags(rest: list[str]) -> dict:
    policy = dict(DEFAULT_E2E_POLICY)
    index = 0
    while index < len(rest):
        token = rest[index]
        if token in {"--json", "--apply"}:
            break
        if token.startswith("--") and index + 1 < len(rest):
            key = token[2:].replace("-", "_")
            value = rest[index + 1]
            if key in {"unattended", "require_screenshots_per_feature", "require_video_evidence",
                       "cursor_cloud_video", "stably_authoring", "auto_review_and_merge",
                       "codex_oauth_required_for_sol_subscription", "side_terminal_teammates"}:
                policy[key] = value.lower() in {"y", "yes", "true", "1", "on"}
            elif key == "loop_on_ci" and value in LOOP_ON_CI:
                policy[key] = value
            elif key == "stably_depth" and value in STABLY_DEPTHS:
                policy[key] = value
            elif key in policy:
                policy[key] = value
            index += 2
            continue
        index += 1
    if policy["unattended"]:
        policy["stably_depth"] = policy.get("stably_depth") or "full_sim_ui"
    return normalize_e2e_policy(policy)


def cmd_settings(rest: list[str]) -> int:
    action = rest[0] if rest else "show"
    if settings_tui_script().is_file() and (
        action in {"show", "status", "doctor", "interview"}
        or (action == "apply" and "--hash" in rest)
    ):
        return launch_settings_tui(rest if rest else ["show"])
    config = load_config()
    current = normalize_e2e_policy(config.get("e2e_policy") if isinstance(config.get("e2e_policy"), dict) else {})
    if action == "show":
        print(f"{BOLD}e2e_policy{RESET}  {DIM}hash-bound via fusion_ctl settings propose|apply{RESET}")
        print(json.dumps(current, indent=2, sort_keys=True))
        _print_codex_oauth_blocker(config)
        print(f"{DIM}unattended fusion/e2e overrides interview; attended still plan-stops{RESET}")
        return 0
    if action == "interview":
        if not sys.stdin.isatty():
            print("settings interview requires a TTY; use: fusion_ctl.py settings propose --unattended n ...")
            print("Unattended e2e class: do not interview; hash original request into plan_confirm instead.")
            return 1
        print(f"{BOLD}Fusion Drive e2e_policy interview{RESET} (Claude-Code style; no secrets)")
        answers = dict(current)
        prompts = [
            ("unattended", "unattended y/n", "n"),
            ("require_screenshots_per_feature", "require_screenshots_per_feature y/n", "y"),
            ("require_video_evidence", "require_video_evidence y/n", "y"),
            ("cursor_cloud_video", "cursor_cloud_video y/n", "y"),
            ("stably_authoring", "stably_authoring y/n", "y"),
            ("stably_depth", "stably_depth ci_shots_logs|jig_video_logs|full_sim_ui", "full_sim_ui"),
            ("loop_on_ci", "loop_on_ci n|y|heavy", "n"),
            ("auto_review_and_merge", "auto_review_and_merge y/n (never silent prod merge)", "n"),
            ("fusion_preset", "fusion_preset / profile", "maximum-intelligence"),
            ("engine", "engine", "in_harness"),
            ("validation_gating", "validation_gating", "on"),
            ("test_flow_author_style", "test_flow_author_style", "verification-author-first"),
        ]
        for key, label, default in prompts:
            raw = input(f"{label} [{answers.get(key, default)}]: ").strip()
            if raw:
                if key in {"unattended", "require_screenshots_per_feature", "require_video_evidence",
                           "cursor_cloud_video", "stably_authoring", "auto_review_and_merge"}:
                    answers[key] = raw.lower() in {"y", "yes", "true", "1", "on"}
                else:
                    answers[key] = raw
        policy = normalize_e2e_policy(answers)
        proposal = propose_config({"e2e_policy": policy}, rationale="fusion_ctl settings interview")
        print(f"proposal_hash {proposal['proposal_hash']}")
        print("approve with: fusion_ctl.py settings apply " + str(proposal["proposal_hash"]))
        _print_codex_oauth_blocker(config)
        return 0
    if action == "propose":
        policy = _policy_from_flags(rest[1:])
        proposal = propose_config({"e2e_policy": policy}, rationale="fusion_ctl settings propose")
        print(json.dumps({"proposal_hash": proposal["proposal_hash"], "e2e_policy": policy}, indent=2, sort_keys=True))
        print("apply: fusion_ctl.py settings apply " + str(proposal["proposal_hash"]))
        _print_codex_oauth_blocker(config)
        return 0
    if action == "apply" and len(rest) >= 2 and not rest[1].startswith("-"):
        result = approve_config(rest[1], confirmed=True)
        print(json.dumps({"approved": True, "proposal_hash": rest[1], "config_path": result.get("config_path")}, indent=2))
        print("active_profile", load_config().get("active_profile"))
        return 0
    if action in {"apply-defaults", "apply"} or "--noninteractive" in rest:
        policy = _policy_from_flags(rest[1:] if action != "apply" else rest)
        apply_change({"e2e_policy": policy}, "fusion_ctl settings e2e_policy")
        print(format_e2e_policy(policy))
        print("e2e_policy applied via propose+approve")
        print("active_profile", load_config().get("active_profile"))
        return 0
    print("usage: fusion_ctl.py settings [show|interview|propose [--unattended y] ...|apply <proposal_hash>|apply-defaults|--noninteractive]")
    return 1


def mini_fuse_enabled(config: dict) -> bool:
    seats = config.get("seats", {})
    return all(seats.get(name, {}).get("enabled") for name in MINI_FUSE_SEATS)


def cmd_profile(target: str) -> int:
    config = load_config()
    slots = load_slots()
    profile = slots.get(target, target)
    if profile not in config.get("profiles", {}):
        known = ", ".join(sorted(config.get("profiles", {})))
        print(f"Unknown profile or slot {target!r}. Profiles: {known}")
        return 1
    if config.get("active_profile") == profile:
        print(f"active_profile already {profile}")
        return 0
    apply_change({"active_profile": profile}, f"fusion_ctl profile switch to {profile}")
    print(f"active_profile → {profile}")
    return 0


def cmd_mini_fuse(action: str) -> int:
    config = load_config()
    if action == "status":
        print("mini-fuse:", "on" if mini_fuse_enabled(config) else "off")
        return 0
    if action not in {"on", "off"}:
        print("usage: fusion_ctl.py mini-fuse on|off|status")
        return 1
    desired = action == "on"
    if mini_fuse_enabled(config) == desired:
        print(f"mini-fuse already {action}")
        return 0
    changes = {"seats": {name: {"enabled": desired} for name in MINI_FUSE_SEATS}}
    apply_change(changes, f"fusion_ctl mini-fuse {action}")
    print(f"mini-fuse → {action}")
    return 0


def cmd_slots(args: list[str]) -> int:
    slots = load_slots()
    if args[:1] == ["set"] and len(args) == 3:
        slot, profile = args[1], args[2]
        config = load_config()
        if profile not in config.get("profiles", {}):
            print(f"Unknown profile {profile!r}")
            return 1
        slots[slot] = profile
        save_slots(slots)
    for slot in sorted(slots):
        print(f"  {slot}: {slots[slot]}")
    print(f"(edit {slots_path()} or `fusion_ctl.py slots set <n> <profile>`)")
    return 0


def cmd_status() -> int:
    config = load_config()
    engine = config["engines"][config["profiles"][config["active_profile"]]["engine"]]
    print(f"profile: {config['active_profile']}")
    print(f"panel: {engine.get('panel')}  judge: {engine.get('judge')}  fuser: {engine.get('fuser')}")
    print("mini-fuse:", "on" if mini_fuse_enabled(config) else "off")
    state = load_toggles()
    print(f"fusion-plan: {'on' if state.get('fusion_plan') else 'off'}  "
          f"preset: {state.get('preset', 'high')}  "
          f"subagent-review: {_review_value(state.get('subagent_review'))}")
    policy = merge_e2e_policy(config.get("e2e_policy") if isinstance(config.get("e2e_policy"), dict) else None)
    print(f"e2e_policy: unattended={'y' if policy.get('unattended') else 'n'} "
          f"stably_depth={policy.get('stably_depth')} "
          f"validation_gating={policy.get('validation_gating')}")
    print("codex_oauth:", codex_oauth_status_line(config))
    return 0


TERMINAL_JOB_STATUSES = {"completed", "failed", "aborted"}


def _read_json_or_none(path: Path) -> dict | None:
    """Read a run-store file, tolerating the writer being mid-rewrite."""

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _active_jobs() -> list[dict]:
    jobs_dir = runtime_dir() / "jobs"
    found = []
    for manifest_path in sorted(jobs_dir.glob("job-*/job.json")):
        manifest = _read_json_or_none(manifest_path)
        if manifest and manifest.get("status") not in TERMINAL_JOB_STATUSES:
            found.append(manifest)
    return found


def _seat_rows(job_id: str) -> tuple[list[tuple[str, str, str, str]], dict]:
    """Build (seat, role, status, detail) rows for a job, newest state first.

    Reads what the orchestrator already persists: panel.json is rewritten after
    every seat completes, and ledger.json reserves an attempt row before a seat's
    transport even starts, which is what makes in-flight seats visible.
    """

    run_dir = runtime_dir() / "engine" / "runs" / job_id
    panel = _read_json_or_none(run_dir / "panel.json") or {}
    ledger = _read_json_or_none(run_dir / "ledger.json") or {}
    manifest = _read_json_or_none(run_dir / "manifest.json") or {}

    rows: list[tuple[str, str, str, str]] = []
    finished = set()
    for result in panel.get("results", []):
        seat = str(result.get("seat_name", "?"))
        finished.add(seat)
        response = result.get("response") or {}
        if result.get("error"):
            detail = str(result["error"])[:60]
        else:
            latency = response.get("latency_seconds")
            model = response.get("actual_model", "")
            detail = f"{model} {latency:.0f}s" if isinstance(latency, (int, float)) else str(model)
        rows.append((seat, str(result.get("role", "")), str(result.get("status", "?")), detail))

    # Only panel seats land in panel.json, so judge/fuser/gate seats are known
    # solely from their reserved ledger attempt. Their stage record is what says
    # whether they are actually still running or already done.
    stages = manifest.get("stages") or {}
    for entry in ledger.get("attempt_entries", []):
        seat = str(entry.get("seat", "?"))
        if seat in finished:
            continue
        stage = str(entry.get("stage", ""))
        record = stages.get(stage) or next(
            (value for key, value in stages.items() if key.startswith(stage) and stage), None
        )
        status = str((record or {}).get("status") or "in-flight")
        detail = "awaiting response" if status == "in-flight" else str((record or {}).get("updated_at", ""))
        rows.append((seat, stage, status, detail))
        finished.add(seat)
    return rows, {"panel": panel, "manifest": manifest}


def _render_watch(job: dict) -> None:
    job_id = str(job.get("job_id", "?"))
    rows, extra = _seat_rows(job_id)
    panel = extra["panel"]
    stages = extra["manifest"].get("stages") or {}
    stage_summary = " ".join(
        f"{name}:{(value or {}).get('status', '?')}" for name, value in sorted(stages.items())
    )
    print(f"[{job_id}] {job.get('operation', 'fuse')} · {job.get('status', '?')} · {job.get('profile', '')}")
    if stage_summary:
        print(f"  stages  {stage_summary}")
    if panel:
        print(
            f"  panel   live={panel.get('live_count', 0)} failed={panel.get('failed_count', 0)}"
            f" degraded={bool(panel.get('degraded'))}"
        )
    if not rows:
        print("  seats   (none reported yet)")
    for seat, role, status, detail in rows:
        print(f"  {status:<10} {seat:<28} {role:<10} {detail}")
    sys.stdout.flush()


def cmd_watch(rest: list[str]) -> int:
    interval = 5.0
    once = False
    job_id = None
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--once":
            once = True
        elif token == "--interval" and index + 1 < len(rest):
            index += 1
            try:
                interval = max(1.0, float(rest[index]))
            except ValueError:
                print(f"fusion watch: bad --interval value {rest[index]!r}")
                return 1
        elif not token.startswith("-"):
            job_id = token
        index += 1

    while True:
        jobs = _active_jobs()
        if job_id is not None:
            manifest = _read_json_or_none(runtime_dir() / "jobs" / job_id / "job.json")
            jobs = [manifest] if manifest else []
        if not jobs:
            print("no active fusion jobs" if job_id is None else f"job {job_id} not found or finished")
            return 0
        for job in jobs:
            _render_watch(job)
        if once:
            return 0
        if all(job.get("status") in TERMINAL_JOB_STATUSES for job in jobs):
            return 0
        print("")
        time.sleep(interval)


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    command, rest = args[0], args[1:]
    if command == "profile" and len(rest) == 1:
        return cmd_profile(rest[0])
    if command == "mini-fuse" and len(rest) == 1:
        return cmd_mini_fuse(rest[0])
    if command == "plan" and len(rest) == 1:
        return cmd_toggle("fusion_plan", "plan", rest[0])
    if command == "preset" and len(rest) == 1:
        return cmd_preset(rest[0])
    if command == "review" and len(rest) == 1:
        return cmd_review(rest[0])
    if command == "config":
        return cmd_config(rest[0] if rest else "")
    if command == "settings":
        return cmd_settings(rest)
    if command == "slots":
        return cmd_slots(rest)
    if command == "status":
        return cmd_status()
    if command == "watch":
        return cmd_watch(rest)
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
