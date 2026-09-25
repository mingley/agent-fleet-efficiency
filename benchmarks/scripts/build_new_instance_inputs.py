"""Build replay inputs + fixtures for the two config-less demo trajectories.

Neither ``function_calling_simple.traj`` nor ``humanevalfix-python-0.traj``
ships a ``replay_config``, so this script grafts a repaired sibling config
onto the original history (documented transplant) and reconstructs the
fixture repo from the ``open`` observations:

* history: byte-identical to the vendor demo
* replay_config.agent: copied from the repaired sweep sibling
  (window100 for humanevalfix/thought_action, function_calling for fc-simple)
* replay_config.problem_statement.text: ISSUE text from the demo's obs 1
* replay_config.env.repo.base_commit: fixture repo's initial commit
* fixture files: parsed from the ``[File: ...]`` display, committed to git

Usage:
    python3 build_new_instance_inputs.py <demos-dir> <out-dir> <fixtures-dir>

Writes ``<out-dir>/{humanevalfix-python-0,function_calling_simple}.input.traj``
and fixture repos ``<fixtures-dir>/{humanevalfix-testbed,fc-simple-testbed}``.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

FILE_LINE = re.compile(r"^(\d+):(.*)$")


def parse_file_display(obs: str) -> list[str]:
    """Extract file lines from an ``open`` observation's ``[File: ...]`` block."""
    lines = []
    in_block = False
    for raw in obs.replace("\r", "").split("\n"):
        if not in_block:
            if raw.startswith("[File:"):
                in_block = True
            continue
        m = FILE_LINE.match(raw)
        if not m:
            break
        lineno, content = int(m.group(1)), m.group(2)
        assert lineno == len(lines) + 1, f"gap in file display at {raw!r}"
        lines.append(content)
    assert lines, "no file lines parsed"
    return lines


def parse_issue(obs: str) -> str:
    """Extract the ISSUE text from the problem-statement observation."""
    start = obs.index("ISSUE:\n") + len("ISSUE:\n")
    end = obs.index("\n\nINSTRUCTIONS:")
    return obs[start:end].replace("\r", "")


def git_commit(repo: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=replay", "-c", "user.email=replay@local",
         "commit", "-qm", "fixture"],
        cwd=repo, check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    return sha


def build(demo: dict, sibling_input: Path, rel_path: str, fixture: Path,
          out_path: Path, executable: bool = False) -> None:
    history = demo["history"]
    obs1 = next(h["content"] for h in history if h.get("role") in ("user", "system")
                and "ISSUE:" in (h.get("content") or ""))
    file_obs = next(h["content"] for h in history
                    if (h.get("content") or "").replace("\r", "").find("[File:") >= 0)
    file_lines = parse_file_display(file_obs)
    target = fixture / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(file_lines) + "\n")
    if executable:
        target.chmod(0o755)
    sha = git_commit(fixture)

    base = json.loads(sibling_input.read_text())
    replay_config = copy.deepcopy(base["replay_config"])
    replay_config["problem_statement"] = {"text": parse_issue(obs1)}
    replay_config["env"]["repo"] = {
        "repo_name": "testbed", "base_commit": sha, "type": "preexisting",
    }
    out = {"history": history, "replay_config": replay_config,
           "trajectory": demo.get("trajectory", []),
           "info": demo.get("info", {}),
           "environment": demo.get("environment", {})}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out))
    acts = sum(1 for h in history if h.get("role") == "assistant")
    print(f"{out_path.name}: {len(history)} history items, {acts} actions, "
          f"fixture {rel_path} ({len(file_lines)} lines) @ {sha[:12]}")


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(f"usage: {Path(argv[0]).name} <demos-dir> <out-dir> <fixtures-dir>",
              file=sys.stderr)
        return 2
    demos, out_dir, fixtures = (Path(a) for a in argv[1:])
    repo_root = Path(__file__).resolve().parent.parent.parent
    window100 = repo_root / "trajectories/sweep/default_sys-env_window100__t-0.20__p-0.95__c-2.00__install-1.input.traj"
    fcall = repo_root / "trajectories/sweep/function_calling__install-1.input.traj"
    hefix = json.loads((demos / "human_thought__swe-bench-HumanEvalFix-python__lcb__t-0.00__p-0.95__c-4.00__install-0"
                        / "humanevalfix-python-0.traj").read_text())
    build(hefix, window100, "main.py", fixtures / "humanevalfix-testbed",
          out_dir / "humanevalfix-python-0.input.traj")
    fcs = json.loads((demos / "function_calling_simple.traj").read_text())
    build(fcs, fcall, "tests/missing_colon.py", fixtures / "fc-simple-testbed",
          out_dir / "function_calling_simple.input.traj", executable=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
