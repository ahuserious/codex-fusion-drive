#!/usr/bin/env python3
"""Stdlib curses list TUI for hash-bound e2e_policy. No secrets.

CLI: show | doctor | interview | apply --hash <sha256> --confirmed

Attended TTY: ↑↓ select, ←→ or y/n cycle, s print hash-bound propose, q quit
without write. Non-TTY or FUSION_E2E_UNATTENDED=1: dump JSON + hash, skip
interview. Persist overlay e2e_policy only. Never write default.json. Never
flip profiles.*.plan_stop_required. Nested unattended_e2e.plan_stop_required
stays as shipped.
"""

from __future__ import annotations

import curses
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
PLUGIN_ROOT = HERE.parents[1] if HERE.parent.name == "scripts" else HERE.parent

STABLY_DEPTHS = ("ci_shots_logs", "jig_video_logs", "full_sim_ui")
LOOP_ON_CI = ("n", "y", "heavy")
VALIDATION_GATING = ("on", "off")
AUTHOR_STYLE = "verification-author-first"

YN_KEYS = (
    "unattended",
    "require_screenshots_per_feature",
    "require_video_evidence",
    "cursor_cloud_video",
    "stably_authoring",
    "auto_review_and_merge",
)

DEFAULT_E2E_POLICY: dict[str, Any] = {
    "unattended": False,
    "require_screenshots_per_feature": True,
    "require_video_evidence": True,
    "cursor_cloud_video": True,
    "stably_authoring": True,
    "stably_depth": "full_sim_ui",
    "loop_on_ci": "n",
    "auto_review_and_merge": False,
    "fusion_preset": "maximum-intelligence",
    "engine": "in_harness",
    "validation_gating": "on",
    "test_flow_author_style": AUTHOR_STYLE,
    "models": "maximum-intelligence",
    "codex_oauth_required_for_sol_subscription": True,
    "spawn_isolation": "none",
    "spawn_surface": "side_terminal_teammates",
    "api_jig_required_before_live": True,
    "abc_contract": True,
    "side_terminal_teammates": True,
    "evidence_isolation": "none",
}

SECRET_SUBSTR = ("key", "token", "secret", "password", "cookie", "credential")


def canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def unattended_env() -> bool:
    token = os.environ.get("FUSION_E2E_UNATTENDED") or os.environ.get("FUSION_UNATTENDED") or ""
    return token.strip().lower() in {"1", "true", "yes", "y"}


def default_json_path() -> Path:
    for name in ("default.json", "fusion-drive.default.json"):
        path = PLUGIN_ROOT / "config" / name
        if path.is_file():
            return path
    return PLUGIN_ROOT / "config" / "default.json"


def overlay_path() -> Path:
    for env_name in ("GROK_PLUGIN_DATA", "CLAUDE_PLUGIN_DATA", "CODEX_PLUGIN_DATA", "PI_PLUGIN_DATA"):
        raw = os.environ.get(env_name)
        if raw:
            return Path(raw) / "config" / "e2e_policy.json"
    home = Path.home()
    text = str(PLUGIN_ROOT)
    if "grok-fusion-drive" in text or "/.grok/" in text:
        return home / ".grok" / "grok-fusion-drive" / "config" / "e2e_policy.json"
    if "codex-fusion-drive" in text or "/.codex/" in text:
        return home / ".codex" / "codex-fusion-drive" / "config" / "e2e_policy.json"
    if "/.pi/" in text:
        return home / ".pi" / "agent" / "config" / "e2e_policy.json"
    return home / ".claude" / "claude-fusion-drive" / "config" / "e2e_policy.json"


def pending_path() -> Path:
    return overlay_path().with_name("e2e_policy.pending.json")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def load_shipped_config() -> dict[str, Any]:
    data = _read_json(default_json_path())
    return data if isinstance(data, dict) else {}


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"y", "yes", "true", "on", "1"}:
        return True
    if text in {"n", "no", "false", "off", "0"}:
        return False
    return default


def normalize_policy(raw: dict[str, Any] | None) -> dict[str, Any]:
    policy = dict(DEFAULT_E2E_POLICY)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in DEFAULT_E2E_POLICY:
                policy[key] = value
    for key in YN_KEYS:
        policy[key] = _coerce_bool(policy.get(key), bool(DEFAULT_E2E_POLICY[key]))
    for key in (
        "codex_oauth_required_for_sol_subscription",
        "api_jig_required_before_live",
        "abc_contract",
        "side_terminal_teammates",
    ):
        policy[key] = _coerce_bool(policy.get(key), bool(DEFAULT_E2E_POLICY[key]))
    if policy.get("stably_depth") not in STABLY_DEPTHS:
        policy["stably_depth"] = "full_sim_ui" if policy.get("unattended") else "ci_shots_logs"
    if policy.get("loop_on_ci") not in LOOP_ON_CI:
        policy["loop_on_ci"] = "n"
    gating = policy.get("validation_gating")
    if gating is True:
        gating = "on"
    if gating is False:
        gating = "off"
    policy["validation_gating"] = gating if gating in VALIDATION_GATING else "on"
    policy["test_flow_author_style"] = AUTHOR_STYLE
    policy["spawn_isolation"] = "none"
    policy["spawn_surface"] = "side_terminal_teammates"
    policy["evidence_isolation"] = "none"
    if not isinstance(policy.get("fusion_preset"), str) or not str(policy["fusion_preset"]).strip():
        policy["fusion_preset"] = "maximum-intelligence"
    if not isinstance(policy.get("engine"), str) or not str(policy["engine"]).strip():
        policy["engine"] = "in_harness"
    if not isinstance(policy.get("models"), str) or not str(policy["models"]).strip():
        policy["models"] = str(policy["fusion_preset"])
    return policy


def _merge_policy_dict(target: dict[str, Any], source: dict[str, Any] | None) -> None:
    if not isinstance(source, dict):
        return
    inner = source.get("e2e_policy") if isinstance(source.get("e2e_policy"), dict) else source
    if not isinstance(inner, dict):
        return
    for key in DEFAULT_E2E_POLICY:
        if key in inner:
            target[key] = inner[key]


def load_policy(*, include_pending: bool = False) -> dict[str, Any]:
    shipped = load_shipped_config()
    merged: dict[str, Any] = {}
    raw = shipped.get("e2e_policy")
    if isinstance(raw, dict):
        merged.update({k: raw[k] for k in DEFAULT_E2E_POLICY if k in raw})
    _merge_policy_dict(merged, _read_json(overlay_path()))
    if include_pending:
        _merge_policy_dict(merged, _read_json(pending_path()))
    return normalize_policy(merged)


def display_value(key: str, policy: dict[str, Any]) -> str:
    value = policy.get(key)
    if key in YN_KEYS or isinstance(value, bool):
        return "y" if value else "n"
    return str(value)


def cycle_choice(current: str, choices: tuple[str, ...], *, reverse: bool = False) -> str:
    if current not in choices:
        return choices[0]
    delta = -1 if reverse else 1
    return choices[(choices.index(current) + delta) % len(choices)]


def available_profiles(config: dict[str, Any]) -> tuple[str, ...]:
    profiles = config.get("profiles")
    if isinstance(profiles, dict) and profiles:
        names = tuple(str(name) for name in profiles if isinstance(name, str))
        if names:
            return names
    return ("maximum-intelligence",)


def available_engines(config: dict[str, Any]) -> tuple[str, ...]:
    engines = config.get("engines")
    if isinstance(engines, dict) and engines:
        names = tuple(str(name) for name in engines if isinstance(name, str))
        if names:
            return names
    return ("in_harness",)


def _seat_model(config: dict[str, Any], seat_name: str) -> str:
    seats = config.get("seats")
    if not isinstance(seats, dict):
        return seat_name
    seat = seats.get(seat_name)
    if not isinstance(seat, dict):
        return seat_name
    model = seat.get("model")
    provider = seat.get("provider")
    bits = [seat_name]
    if isinstance(model, str) and model.strip():
        bits.append(model.strip())
    if isinstance(provider, str) and provider.strip():
        bits.append(provider.strip())
    return "=".join(bits)


def models_fallbacks_line(config: dict[str, Any], policy: dict[str, Any]) -> str:
    profiles = config.get("profiles") if isinstance(config.get("profiles"), dict) else {}
    active = config.get("active_profile")
    engine_name = policy.get("engine")
    if isinstance(profiles, dict):
        wanted = policy.get("fusion_preset") if policy.get("fusion_preset") in profiles else active
        prof = profiles.get(wanted) if isinstance(wanted, str) else None
        if isinstance(prof, dict) and isinstance(prof.get("engine"), str):
            engine_name = prof.get("engine") or engine_name
    engines = config.get("engines") if isinstance(config.get("engines"), dict) else {}
    engine = engines.get(engine_name) if isinstance(engine_name, str) else None
    parts: list[str] = []
    if isinstance(engine, dict):
        panel = engine.get("panel")
        if isinstance(panel, list):
            parts.append("panel:" + ",".join(_seat_model(config, str(item)) for item in panel[:4]))
        for role in ("judge", "fuser"):
            raw = engine.get(role)
            if isinstance(raw, str):
                parts.append(f"{role}:{_seat_model(config, raw)}")
        judge_model = engine.get("judge_model")
        if isinstance(judge_model, str) and judge_model.strip():
            parts.append("judge_model:" + judge_model.strip())
        analysis = engine.get("analysis_models")
        if isinstance(analysis, list):
            parts.append("analysis:" + ",".join(str(item) for item in analysis[:4]))
    fallbacks = config.get("model_fallbacks")
    if isinstance(fallbacks, dict) and fallbacks:
        shown = []
        for src, dst in list(fallbacks.items())[:4]:
            sl = str(src).lower()
            if any(token in sl for token in SECRET_SUBSTR):
                continue
            shown.append(f"{src}->{dst}")
        if shown:
            parts.append("fallbacks:" + ",".join(shown))
    if not parts:
        return f"active={active} engine={engine_name} (display-only)"
    return f"active={active} engine={engine_name} " + " | ".join(parts)


def missing_codex_oauth() -> bool:
    return shutil.which("codex") is None


def doctor_line() -> str:
    binary = shutil.which("codex")
    if binary is None:
        return (
            "NAMED BLOCKER missing_codex_oauth: Codex CLI OAuth is a prerequisite "
            "for Sol-via-subscription seats; not a silent OPENAI_API_KEY / OpenRouter "
            "openai/gpt-5.6-sol (1.05M) fallback. Tokens are not read."
        )
    return f"ok missing_codex_oauth=false command=codex binary={binary} silent_fallback=false"


def propose_payload(policy: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_policy(policy)
    return {
        "e2e_policy": normalized,
        "hash": canonical_hash(normalized),
        "persist": "overlay e2e_policy only",
        "never_flip": "profiles.*.plan_stop_required",
        "unattended_e2e.plan_stop_required": "unchanged",
        "codex_oauth": doctor_line(),
    }


def dump_policy(policy: dict[str, Any], *, extra: dict[str, Any] | None = None) -> None:
    payload = propose_payload(policy)
    if extra:
        payload.update(extra)
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    print(f"e2e_policy_sha256 {payload['hash']}")
    print("codex_oauth:", doctor_line())
    print("Persist overlay e2e_policy only. Never write default.json.")
    print("Never flip profiles.*.plan_stop_required. Nested unattended_e2e.plan_stop_required stays.")
    print("apply: python3 scripts/settings_tui.py apply --hash <sha256> --confirmed")
    print("Unattended e2e/GOAL/Stably skips interview. auto_review_and_merge never silent prod merge.")


EDITABLE_ROWS: tuple[tuple[str, str], ...] = (
    ("unattended", "y/n"),
    ("require_screenshots_per_feature", "y/n"),
    ("require_video_evidence", "y/n"),
    ("cursor_cloud_video", "y/n"),
    ("stably_authoring", "y/n"),
    ("stably_depth", "ci_shots_logs|jig_video_logs|full_sim_ui"),
    ("loop_on_ci", "n|y|heavy"),
    ("auto_review_and_merge", "y/n (never silent prod merge)"),
    ("fusion_preset", "cycle profiles"),
    ("engine", "cycle engines"),
    ("validation_gating", "on|off"),
    ("test_flow_author_style", "verification-author-first (fixed)"),
    ("models_fallbacks", "display-only from active profile"),
)


def cycle_row(key: str, policy: dict[str, Any], config: dict[str, Any], *, reverse: bool = False) -> None:
    if key == "models_fallbacks" or key == "test_flow_author_style":
        return
    if key in YN_KEYS:
        policy[key] = not bool(policy.get(key))
        if key == "unattended" and policy[key] and policy.get("stably_depth") not in STABLY_DEPTHS:
            policy["stably_depth"] = "full_sim_ui"
        return
    if key == "stably_depth":
        policy[key] = cycle_choice(str(policy.get(key)), STABLY_DEPTHS, reverse=reverse)
        return
    if key == "loop_on_ci":
        policy[key] = cycle_choice(str(policy.get(key)), LOOP_ON_CI, reverse=reverse)
        return
    if key == "validation_gating":
        policy[key] = cycle_choice(str(policy.get(key)), VALIDATION_GATING, reverse=reverse)
        return
    if key == "fusion_preset":
        policy[key] = cycle_choice(str(policy.get(key)), available_profiles(config), reverse=reverse)
        policy["models"] = str(policy[key])
        return
    if key == "engine":
        policy[key] = cycle_choice(str(policy.get(key)), available_engines(config), reverse=reverse)


def set_yn(policy: dict[str, Any], key: str, value: bool) -> None:
    if key in YN_KEYS:
        policy[key] = value
        if key == "unattended" and value:
            if policy.get("stably_depth") not in STABLY_DEPTHS:
                policy["stably_depth"] = "full_sim_ui"


def run_curses(policy: dict[str, Any], config: dict[str, Any]) -> str:
    """Return 'propose', 'quit', or 'dump'."""
    state = {"action": "quit", "index": 0}

    def _draw(stdscr: curses.window) -> None:
        curses.curs_set(0)
        stdscr.keypad(True)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_RED, -1)
            curses.init_pair(4, curses.COLOR_GREEN, -1)
        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            digest = canonical_hash(normalize_policy(policy))
            header = f"Fusion Drive e2e_policy  hash={digest[:16]}…"
            blocker = missing_codex_oauth()
            doc = doctor_line()
            models = models_fallbacks_line(config, policy)
            lines = [
                header[: max(0, width - 1)],
                ("BLOCKER " if blocker else "doctor  ") + doc,
                "models/fallbacks " + models,
                "Persist overlay e2e_policy only. q quits without write. s prints propose payload.",
                "Never flip plan_stop_required. Never silent prod merge. Never read tokens.",
                "",
            ]
            for row_i, (key, hint) in enumerate(EDITABLE_ROWS):
                if key == "models_fallbacks":
                    value = "(display-only)"
                else:
                    value = display_value(key, policy)
                marker = ">" if row_i == state["index"] else " "
                row = f" {marker} {key:<34} {value:<22} {hint}"
                lines.append(row)
            lines.extend(
                [
                    "",
                    "↑↓ select   ←→ or y/n cycle   s propose-hash   q quit",
                ]
            )
            for y, line in enumerate(lines):
                if y >= height - 1:
                    break
                attr = curses.A_NORMAL
                if y == 1 and blocker and curses.has_colors():
                    attr = curses.color_pair(3) | curses.A_BOLD
                elif y == 1 and curses.has_colors():
                    attr = curses.color_pair(4)
                body_start = 6
                if body_start <= y < body_start + len(EDITABLE_ROWS):
                    if (y - body_start) == state["index"] and curses.has_colors():
                        attr = curses.color_pair(1) | curses.A_BOLD
                    elif (y - body_start) == state["index"]:
                        attr = curses.A_REVERSE
                try:
                    stdscr.addnstr(y, 0, line, max(0, width - 1), attr)
                except curses.error:
                    pass
            stdscr.refresh()
            try:
                keypress = stdscr.getch()
            except KeyboardInterrupt:
                state["action"] = "quit"
                return
            if keypress in (ord("q"), ord("Q"), 27):
                state["action"] = "quit"
                return
            if keypress in (ord("s"), ord("S")):
                state["action"] = "propose"
                return
            if keypress in (curses.KEY_UP, ord("k")):
                state["index"] = (state["index"] - 1) % len(EDITABLE_ROWS)
                continue
            if keypress in (curses.KEY_DOWN, ord("j")):
                state["index"] = (state["index"] + 1) % len(EDITABLE_ROWS)
                continue
            row_key = EDITABLE_ROWS[state["index"]][0]
            if keypress in (curses.KEY_RIGHT, ord("l"), ord(" ")):
                cycle_row(row_key, policy, config, reverse=False)
                continue
            if keypress in (curses.KEY_LEFT, ord("h")):
                cycle_row(row_key, policy, config, reverse=True)
                continue
            if keypress in (ord("y"), ord("Y")):
                set_yn(policy, row_key, True)
                continue
            if keypress in (ord("n"), ord("N")):
                set_yn(policy, row_key, False)
                continue

    try:
        curses.wrapper(_draw)
    except curses.error:
        state["action"] = "dump"
    return str(state["action"])


def tty_ok() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty()) and not unattended_env()


def write_pending(policy: dict[str, Any]) -> Path:
    path = pending_path()
    if path.name == "default.json" or path.resolve() == default_json_path().resolve():
        raise RuntimeError("refusing to write default.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"e2e_policy": normalize_policy(policy)}, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def cmd_show() -> int:
    policy = load_policy()
    dump_policy(policy, extra={"models_fallbacks": models_fallbacks_line(load_shipped_config(), policy)})
    return 0


def cmd_doctor() -> int:
    policy = load_policy()
    dump_policy(policy, extra={"models_fallbacks": models_fallbacks_line(load_shipped_config(), policy)})
    return 2 if missing_codex_oauth() else 0


def cmd_interview() -> int:
    policy = load_policy()
    config = load_shipped_config()
    if not tty_ok():
        print("interview skipped (non-TTY or FUSION_E2E_UNATTENDED=1)")
        dump_policy(policy)
        return 0
    action = run_curses(policy, config)
    if action == "propose":
        pending = write_pending(policy)
        dump_policy(policy)
        print(f"pending {pending} (not default.json; q would have written nothing)")
        print("overlay not applied until: settings_tui.py apply --hash <sha256> --confirmed")
        return 0
    if action == "dump":
        dump_policy(policy)
        return 0
    print("quit without write")
    return 0


def cmd_apply(argv: list[str]) -> int:
    expected = ""
    confirmed = False
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--hash" and i + 1 < len(argv):
            expected = argv[i + 1]
            i += 2
            continue
        if token.startswith("--hash="):
            expected = token.split("=", 1)[1]
            i += 1
            continue
        if token == "--confirmed":
            confirmed = True
            i += 1
            continue
        i += 1
    if not confirmed or not expected:
        print("apply requires --hash <sha256> --confirmed")
        return 1
    policy = normalize_policy(load_policy(include_pending=True))
    digest = canonical_hash(policy)
    if digest != expected:
        print(f"hash mismatch: computed {digest} != {expected}")
        print("Re-run interview, press s, then apply the printed hash. Overlay not written.")
        return 1
    path = overlay_path()
    if path.name == "default.json" or path.resolve() == default_json_path().resolve():
        print("refusing to write default.json")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"e2e_policy": policy}, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote overlay {path}")
    print(f"e2e_policy_sha256 {digest}")
    print("did not write default.json; did not flip plan_stop_required")
    print("Merge overlay e2e_policy via config_propose / grok_fusion_drive_config_propose on the current config SHA-256, then config_approve on the proposal hash.")
    return 0


def usage() -> int:
    print(__doc__)
    print("usage: settings_tui.py [show|doctor|interview|apply --hash <sha256> --confirmed]")
    return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"-h", "--help"}:
        return usage()
    action = args[0] if args else "interview"
    if action == "show" or action == "status":
        return cmd_show()
    if action == "doctor":
        return cmd_doctor()
    if action == "interview":
        return cmd_interview()
    if action == "apply":
        return cmd_apply(args[1:])
    return usage()


if __name__ == "__main__":
    raise SystemExit(main())
