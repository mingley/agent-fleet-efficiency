# Decision Log

## 2026-09-24 — Measurement first

Do not claim that Rust materially reduces fleet cost until trace replay measures executor share of total CPU/RSS.

## 2026-09-24 — Optimize the incumbent first

Compare Rust against an optimized Python SWE-ReX baseline, not only current upstream behavior.

Reason: connection reuse, redundant process launches, PTY round trips, fixed sleeps, buffering, and streaming choices can otherwise be falsely credited to the language rewrite.

## 2026-09-24 — First native target: SWE-ReX-compatible executor

Build a native execution daemon behind an existing API rather than a new agent framework or sandbox cloud.

Reasons:

- repeated per-tool-call placement
- explicit massive-parallelism use case
- existing SWE-agent adoption
- MIT license
- backend-neutral position

## 2026-09-24 — Reuse beats rewrite

Use existing VM/sandbox lifecycle systems and existing compiler/package caches. Focus new code on measured execution overhead and eliminating repeated setup/build work.

## 2026-09-24 — Negative results are useful

If optimized Python, shared caching, or workspace reuse dominates the Rust delta, document that and change direction. The project is an efficiency investigation, not a mandate to produce a Rust rewrite.

## 2026-09-24 — P0.5 exit review (matrix-20260924T211248Z, linux/arm64 container)

Workload A tiny_true, n=1000: upstream 113.21ms, opt-plain 112.43ms
(parity), opt-all 55.16ms (-51%), 0001-only 109.52ms, 0002-only
60.03ms, 0003-only 113.36ms, direct subprocess 0.28ms. Session
create/close 466.80ms -> 162.25ms under 0003. Child CPU 5.56s ->
1.16s, executor CPU 3.44s -> 2.45s. All arms 0 failures.

Attribution: the extra PTY exit-status round trip (0002) dominates
per-command latency (-53ms); the bash -n subprocess (0001) dominates
child CPU but costs only ~4ms wall; fixed sleeps (0003) dominate
session creation only. ~55ms/command of prompt-sync/read-loop cost
remains in optimized Python, still ~200x above direct subprocess.

Server workload (n=50, localhost): remote tiny 120.72ms -> 119.22ms,
upload ~33ms flat. No latency win from connection reuse, bounded
idempotency, or streaming upload at this shape; their CPU/RSS
benefits are unmeasured (server-side RSS is not yet collected).

Decision: proceed to P1. The 2->3 delta (optimized Python vs Rust)
can only be measured once agent-execd exists, and ~55ms/command of
executor tax remains. In parallel, workload C/D (build/test share of
task, trace replay) is still required: the execution layer's share of
total task cost is unknown, and the P0 exit rule turns on it.

Limitations: single run, Docker Desktop linux/arm64 (not fleet
amd64 bare metal), no CPU pinning, no server-side RSS, no variance
analysis. Numbers are comparison evidence, not fleet claims.
