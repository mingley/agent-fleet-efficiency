#!/usr/bin/env python3
"""Record host + toolchain facts for a benchmark run (stdlib only).

Usage:
    python3 benchmarks/scripts/record_host.py [--out benchmark-results/host.json]

Emits one JSON object describing the machine, OS, toolchains, and repo
revision so results stay comparable across hosts. Follows the host-recording
checklist in docs/benchmark-plan.md. Every probe is best-effort; a failed
probe records null rather than failing the run.
"""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone


def _run(cmd):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except Exception:
        return None


def _cpu_model():
    if sys.platform == "darwin":
        out = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        return out or None
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def _mem_bytes():
    if sys.platform == "darwin":
        out = _run(["sysctl", "-n", "hw.memsize"])
        return int(out) if out and out.isdigit() else None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _filesystem():
    if sys.platform == "darwin":
        out = _run(["stat", "-f", "%T", "."])
    else:
        out = _run(["stat", "-f", "-c", "%T", "."])
    return out or None


def _governor():
    try:
        with open(
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
        ) as f:
            return f.read().strip()
    except OSError:
        return None


def _git(field_cmd):
    out = _run(["git"] + field_cmd)
    return out or None


def _tool_version(cmd, pattern=None):
    out = _run(cmd)
    if not out:
        return None
    if pattern:
        m = re.search(pattern, out)
        return m.group(1) if m else out.splitlines()[0]
    return out.splitlines()[0]


def collect():
    dirty = _run(["git", "status", "--porcelain"])
    try:
        loadavg = os.getloadavg()
    except OSError:
        loadavg = None
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "cpu_model": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "mem_bytes": _mem_bytes(),
        "kernel": platform.platform(),
        "arch": platform.machine(),
        "filesystem": _filesystem(),
        "python": platform.python_version(),
        "repo_sha": _git(["rev-parse", "HEAD"]),
        "repo_branch": _git(["branch", "--show-current"]),
        "repo_dirty": bool(dirty) if dirty is not None else None,
        "docker": _tool_version(["docker", "--version"]),
        "rustc": _tool_version(["rustc", "--version"]),
        "cargo": _tool_version(["cargo", "--version"]),
        "cpu_governor": _governor(),
        "loadavg_1_5_15": list(loadavg) if loadavg else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="Write JSON here (default: stdout)")
    args = parser.parse_args()
    record = collect()
    text = json.dumps(record, indent=2) + "\n"
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            f.write(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
