"""Repair a stale SWE-agent demo trajectory for `sweagent run-replay`.

Applies the known upstream renames to ``replay_config.agent`` only; ``history``,
``trajectory``, ``info`` and ``environment`` are left byte-identical:

* bundle paths: ``tools/defaults`` -> ``tools/windowed``,
  ``tools/edit_linting`` -> ``tools/windowed_edit_linting``,
  ``tools/edit_replace`` -> ``tools/windowed_edit_replace`` (PR #1147 renames)
* prepend ``{"path": "tools/registry", "hidden_tools": []}`` if absent
  (now holds ``_write_env``, required first on PATH)
* ``agent.history_processor`` (dict) -> ``agent.history_processors`` (list)

Usage:
    python3 repair_traj.py <input.traj> <output.traj>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BUNDLE_RENAMES = {
    "tools/defaults": "tools/windowed",
    "tools/edit_linting": "tools/windowed_edit_linting",
    "tools/edit_replace": "tools/windowed_edit_replace",
}

REGISTRY_BUNDLE = {"path": "tools/registry", "hidden_tools": []}


def repair(data: dict) -> list[str]:
    """Mutate ``data`` in place; return a human-readable list of fixes applied."""
    try:
        replay_config = data["replay_config"]
    except KeyError:
        raise ValueError(
            "no replay_config in trajectory (pre-replay format); "
            "cannot repair without a stored agent config"
        ) from None
    if isinstance(replay_config, str):
        replay_config = json.loads(replay_config)
        data["replay_config"] = replay_config
    agent = replay_config["agent"]
    fixes: list[str] = []

    bundles = agent["tools"]["bundles"]
    for bundle in bundles:
        old = bundle["path"]
        if old in BUNDLE_RENAMES:
            bundle["path"] = BUNDLE_RENAMES[old]
            fixes.append(f"bundle {old} -> {bundle['path']}")
    if not any(b["path"] == "tools/registry" for b in bundles):
        bundles.insert(0, dict(REGISTRY_BUNDLE))
        fixes.append("prepend tools/registry")

    if "history_processor" in agent:
        legacy = agent.pop("history_processor")
        if "history_processors" not in agent:
            agent["history_processors"] = legacy if isinstance(legacy, list) else [legacy]
            fixes.append("history_processor dict -> history_processors list")
        else:
            fixes.append("drop legacy history_processor (history_processors already present)")

    return fixes


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {Path(argv[0]).name} <input.traj> <output.traj>", file=sys.stderr)
        return 2
    src, dst = Path(argv[1]), Path(argv[2])
    data = json.loads(src.read_text())
    try:
        fixes = repair(data)
    except ValueError as e:
        print(f"{src}: {e}", file=sys.stderr)
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(data))
    print(f"{src.name}: {', '.join(fixes) if fixes else 'already current'} -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
