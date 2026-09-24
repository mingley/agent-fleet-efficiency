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
