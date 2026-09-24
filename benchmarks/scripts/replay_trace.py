#!/usr/bin/env python3
"""Replay a workload-D trace against an executor (P0 share-of-task track).

Reads a validated trace JSONL file (see benchmarks/schemas/trace.schema.json
and validate_trace.py), re-executes its session/command events, and checks
exit-code fidelity. Output byte counts are recorded, not asserted: replay
runs on a different machine/time than capture, so bytes legitimately differ.

Event handling:
- session_start/session_end -> create/close the named session
- command -> run it (cd first when cwd is set and differs; env_delta
  applied via a preceding export); exit code must match the trace
- interrupt -> interrupt the session (best effort, never fails the run)
- file_op write -> write size_bytes of filler; read/upload -> best effort,
  recorded as skipped when the path is absent

Usage:
    python3 benchmarks/scripts/replay_trace.py benchmarks/fixtures/replay-smoke.jsonl --variant swerex-local
    python3 benchmarks/scripts/replay_trace.py <trace> --variant direct --out-dir benchmark-results
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_workload_b as WB
from validate_trace import validate as validate_trace_file


class Replayer:
    def __init__(self, executor):
        self.ex = executor
        self.rec = WB.Recorder()
        self.cwd = {}
        self.trace_wall_ms = 0.0

    async def _run_fidelity(self, session, command, timeout):
        # Helper command (cd/export): run it, record, never fail the replay.
        t0 = time.perf_counter()
        try:
            code, _ = await self.ex.run(session, command)
            ok = True
            note = f"exit={code}"
        except Exception as e:  # noqa: BLE001 - fidelity helper, keep going
            code, ok, note = -1, True, f"{type(e).__name__}: {e}"
        self.rec.add("fidelity_setup", time.perf_counter() - t0,
                     exit_code=code, ok=ok, note=note)

    async def on_command(self, ev):
        session = ev["session"]
        cwd = ev.get("cwd") or ""
        if cwd and self.cwd.get(session) != cwd:
            await self._run_fidelity(session, f"cd {cwd}", ev.get("timeout_s"))
            self.cwd[session] = cwd
        env_delta = ev.get("env_delta") or {}
        if env_delta:
            exports = " ".join(f"{k}={v}" for k, v in env_delta.items())
            await self._run_fidelity(session, f"export {exports}", ev.get("timeout_s"))
        self.trace_wall_ms += ev.get("duration_ms", 0)
        t0 = time.perf_counter()
        try:
            code, output = await self.ex.run(session, ev["command"])
        except Exception as e:  # noqa: BLE001 - record mismatch, keep going
            # Session executors raise on nonzero exit (check="raise"); recover
            # the code from the error when the trace expects failure.
            wall = time.perf_counter() - t0
            m = re.search(r"exit code (\d+)", str(e))
            code = int(m.group(1)) if m else None
            ok = code is not None and code == ev.get("exit_code")
            self.rec.add("replay_command", wall, exit_code=code if code is not None else -1,
                         ok=ok, note=f"{type(e).__name__} :: {ev['command'][:80]}")
            return
        wall = time.perf_counter() - t0
        want = ev.get("exit_code")
        ok = (code == want)
        note = "" if ok else f"exit {code} != trace {want} :: {ev['command'][:80]}"
        self.rec.add("replay_command", wall, out_bytes=len(output or ""),
                     exit_code=code, ok=ok, note=note)

    async def on_file_op(self, ev):
        op, path, size = ev["op"], ev["path"], ev["size_bytes"]
        t0 = time.perf_counter()
        try:
            if op == "write":
                await self.ex.write_file(path, "x" * size)
                self.rec.add("replay_file_write", time.perf_counter() - t0,
                             out_bytes=size, note=path)
            elif op == "read":
                content = await self.ex.read_file(path)
                self.rec.add("replay_file_read", time.perf_counter() - t0,
                             out_bytes=len(content or ""), note=path)
            else:
                self.rec.add("replay_file_upload", 0.0, ok=True,
                             note=f"skipped (no source content): {path}")
        except Exception as e:  # noqa: BLE001 - best effort, keep going
            self.rec.add(f"replay_file_{op}", time.perf_counter() - t0, ok=True,
                         note=f"skipped: {type(e).__name__}: {e}")

    async def run(self, events):
        for ev in events:
            etype = ev["type"]
            if etype == "trace_header":
                continue
            elif etype == "session_start":
                t0 = time.perf_counter()
                try:
                    await self.ex.create_session(ev["session"])
                    self.rec.add("replay_session_create", time.perf_counter() - t0)
                except Exception as e:  # noqa: BLE001
                    self.rec.add("replay_session_create", time.perf_counter() - t0,
                                 ok=False, note=f"{type(e).__name__}: {e}")
            elif etype == "session_end":
                t0 = time.perf_counter()
                try:
                    await self.ex.close_session(ev["session"])
                    self.rec.add("replay_session_close", time.perf_counter() - t0)
                except Exception as e:  # noqa: BLE001
                    self.rec.add("replay_session_close", time.perf_counter() - t0,
                                 ok=False, note=f"{type(e).__name__}: {e}")
            elif etype == "command":
                await self.on_command(ev)
            elif etype == "interrupt":
                try:
                    from swerex.runtime.abstract import BashInterruptAction
                    await self.ex._rt.run_in_session(
                        BashInterruptAction(session=ev["session"]))
                except Exception:  # noqa: BLE001 - best effort only
                    pass
                self.rec.add("replay_interrupt", 0.0, ok=True, note="best effort")
            elif etype == "file_op":
                await self.on_file_op(ev)


async def main_async(args):
    n_events, errors = validate_trace_file(args.trace)
    if errors:
        print(f"{args.trace}: INVALID trace ({len(errors)} errors)")
        for err in errors[:10]:
            print(f"  {err}")
        return 2
    with open(args.trace) as f:
        events = [json.loads(line) for line in f if line.strip()]

    ex = WB.EXECUTORS[args.variant](timeout=args.timeout)
    rp = Replayer(ex)
    if not ex.supports_sessions:
        print("note: direct variant has no sessions; lifecycle events are no-ops")
    t0 = time.perf_counter()
    await rp.run(events)
    wall = time.perf_counter() - t0
    failures = rp.rec.failures()
    summary = {
        "type": "summary",
        "trace": os.path.basename(args.trace),
        "variant": ex.name,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "trace_events": n_events,
        "trace_wall_s": rp.trace_wall_ms / 1000,
        "replay_wall_s": wall,
        "failures": len(failures),
        "by_op": rp.rec.summarize(),
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(args.out_dir, f"replay-{ex.name}-{ts}.jsonl")
    os.makedirs(args.out_dir, exist_ok=True)
    with open(out_path, "w") as f:
        for op_rec in rp.rec.ops:
            f.write(json.dumps({"type": "op", **op_rec}) + "\n")
        f.write(json.dumps(summary) + "\n")
    print(f"variant={ex.name} events={n_events} replay_wall={wall:.1f}s "
          f"trace_wall={summary['trace_wall_s']:.1f}s failures={len(failures)}")
    print(f"wrote {out_path}")
    for fail in failures[:10]:
        print(f"  FAIL {fail}")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace")
    parser.add_argument("--variant", choices=sorted(WB.EXECUTORS), default="swerex-local")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--out-dir", default="benchmark-results")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
