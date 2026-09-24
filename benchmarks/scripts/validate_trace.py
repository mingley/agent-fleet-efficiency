#!/usr/bin/env python3
"""Validate a workload-D trace file (stdlib only).

Checks structural rules the replay harness depends on: exactly one
leading header, known event types, required fields per type, monotonic
sequence numbers, and paired session start/end events. This is a
structural pre-check, not a full JSON Schema validator; the normative
field types live in benchmarks/schemas/trace.schema.json.

Usage:
    python3 benchmarks/scripts/validate_trace.py benchmarks/fixtures/sample-trace.jsonl
"""

import json
import sys

REQUIRED = {
    "trace_header": {"trace_version", "source"},
    "session_start": {"session"},
    "session_end": {"session"},
    "command": {"session", "command", "duration_ms", "exit_code",
                "stdout_bytes", "stderr_bytes"},
    "interrupt": {"session"},
    "file_op": {"op", "path", "size_bytes", "duration_ms"},
}

FILE_OPS = {"read", "write", "upload"}


def validate(path):
    errors = []
    open_sessions = set()
    expected_seq = 0
    n_events = 0
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"line {lineno}: invalid JSON: {e}")
                continue
            if not isinstance(ev, dict):
                errors.append(f"line {lineno}: event must be an object")
                continue
            n_events += 1
            etype = ev.get("type")
            if etype not in REQUIRED:
                errors.append(f"line {lineno}: unknown type {etype!r}")
                continue
            if lineno == 1 and etype != "trace_header":
                errors.append("line 1: first event must be trace_header")
            if etype == "trace_header" and lineno != 1:
                errors.append(f"line {lineno}: trace_header only valid first")
            missing = [k for k in ({"seq", "t_ms"} | REQUIRED[etype]) if k not in ev]
            if missing:
                errors.append(f"line {lineno}: missing fields {sorted(missing)}")
                continue
            if ev["seq"] != expected_seq:
                errors.append(
                    f"line {lineno}: seq {ev['seq']} != expected {expected_seq}")
            expected_seq += 1
            if etype == "session_start":
                if ev["session"] in open_sessions:
                    errors.append(
                        f"line {lineno}: session {ev['session']!r} already open")
                open_sessions.add(ev["session"])
            elif etype == "session_end":
                if ev["session"] not in open_sessions:
                    errors.append(
                        f"line {lineno}: session {ev['session']!r} not open")
                else:
                    open_sessions.discard(ev["session"])
            elif etype in ("command", "interrupt"):
                if ev["session"] not in open_sessions:
                    errors.append(
                        f"line {lineno}: session {ev['session']!r} not open")
            elif etype == "file_op":
                if ev["op"] not in FILE_OPS:
                    errors.append(f"line {lineno}: bad file op {ev['op']!r}")
    for session in sorted(open_sessions):
        errors.append(f"session {session!r} never closed")
    return n_events, errors


def main():
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <trace.jsonl>", file=sys.stderr)
        return 2
    n_events, errors = validate(sys.argv[1])
    if errors:
        print(f"{sys.argv[1]}: INVALID ({n_events} events, "
              f"{len(errors)} errors)")
        for err in errors[:20]:
            print(f"  {err}")
        return 1
    print(f"{sys.argv[1]}: valid ({n_events} events)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
