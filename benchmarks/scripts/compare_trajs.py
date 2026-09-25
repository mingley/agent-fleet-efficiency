"""Compare two replay output trajs step-by-step (control vs treatment).

Compares the fields that must match for runtime parity:

* ``trajectory[i]``: action, observation, thought, response, state, query
  (``execution_time`` excluded — it measures the runtimes' speed)
* ``history`` assistant items: replayed actions (user items in replay output
  are unrendered ``{observation}`` templates by design)
* ``info``: exit_status, submission, edited_files*

Usage:
    python3 compare_trajs.py <control.traj> <treatment.traj>
    python3 compare_trajs.py --pair <name> <control-dir> <treatment-dir>

Exit 0 on full parity, 1 on any divergence (divergences printed).
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

TRAJ_KEYS = ("action", "observation", "thought", "response", "state", "query")
INFO_KEYS = ("exit_status", "submission",
             "edited_files30", "edited_files50", "edited_files70")


def find_traj(d: Path) -> Path:
    gs = sorted(glob.glob(str(d / "*" / "*.traj")))
    assert len(gs) == 1, f"expected 1 traj under {d}, found {len(gs)}"
    return Path(gs[0])


def compare(control: dict, treatment: dict) -> list[str]:
    divs: list[str] = []
    ct, tt = control.get("trajectory", []), treatment.get("trajectory", [])
    if len(ct) != len(tt):
        divs.append(f"trajectory length {len(ct)} != {len(tt)}")
    for i, (a, b) in enumerate(zip(ct, tt)):
        for k in TRAJ_KEYS:
            if a.get(k) != b.get(k):
                divs.append(f"trajectory[{i}].{k} differs")
    ch, th = control.get("history", []), treatment.get("history", [])
    ca = [h for h in ch if h.get("role") == "assistant"]
    ta = [h for h in th if h.get("role") == "assistant"]
    if len(ca) != len(ta):
        divs.append(f"assistant-action count {len(ca)} != {len(ta)}")
    for i, (a, b) in enumerate(zip(ca, ta)):
        if a != b:
            divs.append(f"history assistant[{i}] differs")
    ci, ti = control.get("info", {}), treatment.get("info", {})
    for k in INFO_KEYS:
        if ci.get(k) != ti.get(k):
            divs.append(f"info.{k} differs")
    return divs


def show_first_div(control: dict, treatment: dict, div: str, width: int = 500) -> None:
    if div.startswith("trajectory["):
        idx = int(div.split("[")[1].split("]")[0])
        key = div.split(".", 1)[1].split()[0]
        a = control["trajectory"][idx].get(key)
        b = treatment["trajectory"][idx].get(key)
        print(f"--- {div} ---")
        print("CONTROL:  ", json.dumps(a)[:width])
        print("TREATMENT:", json.dumps(b)[:width])
    else:
        print(f"--- {div} --- (see trajs)")


def main(argv: list[str]) -> int:
    if len(argv) == 5 and argv[1] == "--pair":
        _, _, name, cdir, tdir = argv
        cp, tp = find_traj(Path(cdir)), find_traj(Path(tdir))
        label = name
    elif len(argv) == 3:
        cp, tp, label = Path(argv[1]), Path(argv[2]), f"{argv[1]} vs {argv[2]}"
    else:
        print(__doc__, file=sys.stderr)
        return 2
    control = json.loads(cp.read_text())
    treatment = json.loads(tp.read_text())
    divs = compare(control, treatment)
    if not divs:
        n = len(control.get("trajectory", []))
        print(f"{label}: PARITY ({n} steps, actions+observations+info identical)")
        return 0
    print(f"{label}: {len(divs)} DIVERGENCES")
    for d in divs[:10]:
        show_first_div(control, treatment, d)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
