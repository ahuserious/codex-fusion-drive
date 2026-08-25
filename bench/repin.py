"""Intentionally regenerate the artifact hashes in bench/pins.json.

The bench and evidence validator fail closed on any plugin-tree or runtime
drift, so every deliberate plugin edit must be followed by an explicit repin.
This helper recomputes only the ``artifacts`` block using the exact hash
functions the runner verifies with, prints the before/after values, and leaves
every other pin (task lists, harness pins) untouched.

Usage: python3 bench/repin.py [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

BENCH_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = BENCH_ROOT.parent
sys.path.insert(0, str(BENCH_ROOT))

from run_bench import file_sha256, tree_sha256  # noqa: E402

PINS_PATH = BENCH_ROOT / "pins.json"


def observed_artifacts() -> dict[str, str]:
    observed = {
        "plugin_tree_sha256": tree_sha256(REPOSITORY_ROOT / "plugins" / "codex-fusion-drive"),
        "runner_sha256": file_sha256(BENCH_ROOT / "run_bench.py"),
        "validator_sha256": file_sha256(BENCH_ROOT / "validate_evidence.py"),
    }
    runtime_digest = hashlib.sha256()
    for directory_name in ("harbor", "pier", "support"):
        runtime_digest.update(directory_name.encode("utf-8"))
        runtime_digest.update(b"\0")
        runtime_digest.update(tree_sha256(BENCH_ROOT / directory_name).encode("ascii"))
        runtime_digest.update(b"\n")
    observed["benchmark_runtime_tree_sha256"] = runtime_digest.hexdigest()
    return observed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print drift without writing")
    args = parser.parse_args()

    pins = json.loads(PINS_PATH.read_text(encoding="utf-8"))
    expected = pins.get("artifacts", {})
    observed = observed_artifacts()

    drifted = {
        key: {"pinned": expected.get(key), "observed": value}
        for key, value in observed.items()
        if expected.get(key) != value
    }
    if not drifted:
        print("Pins already match the current tree; nothing to do.")
        return 0

    print(json.dumps(drifted, indent=2, sort_keys=True))
    if args.dry_run:
        print("Dry run: pins.json left unchanged.")
        return 1

    pins["artifacts"] = observed
    PINS_PATH.write_text(json.dumps(pins, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"Repinned {len(drifted)} artifact hash(es) in {PINS_PATH}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
