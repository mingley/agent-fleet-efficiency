# Agent Fleet Efficiency

A measurement-first project to reduce CPU, memory, filesystem I/O, and setup work consumed by large fleets of AI coding agents, eval workers, and RL environments.

## Thesis

LLM inference is primarily GPU work, but agent tool use is ordinary systems work. At scale, agents repeatedly create sandboxes, spawn shells, move files, clone repositories, install dependencies, compile code, run tests, parse output, and tear environments down.

The opportunity is not "rewrite everything in Rust." The opportunity is to identify the execution paths multiplied by every agent or tool call, measure them under realistic concurrency, remove redundant work, and use Rust where runtime overhead remains material.

## Current priority order

1. Benchmark real agent execution traces.
2. Optimize obvious SWE-ReX Python hot paths before attributing gains to Rust.
3. Build a Rust execution core compatible with SWE-ReX if the execution plane remains material.
4. Add shared compiler/package/repository caches across disposable sandboxes.
5. Add copy-on-write workspace preparation and warm-worker reuse.
6. Reuse the same native execution core in OpenHands and RL/eval systems if measurements justify it.

Do not start by rewriting Firecracker, E2B, Kubernetes Agent Sandbox, ArcBox, Buck2, sccache, MCP protocol plumbing, or GitHub Actions runner infrastructure. Strong native implementations already exist, and much of their cost sits below the language layer.

## Primary metric

Optimize core-seconds per useful completed agent task and RSS per concurrent agent.

Microbenchmark throughput is secondary. A protocol layer can be 10x faster and still have negligible fleet value if the agent spends almost all of its time compiling or testing.

## First implementation candidate

agent-execd: a small Rust daemon that runs inside an existing Docker container, VM, Firecracker microVM, or Kubernetes sandbox.

Responsibilities:

- persistent shell and PTY sessions
- process execution and supervision
- process-tree cancellation
- timeouts
- file read/write/upload primitives
- bounded output streaming
- per-command resource accounting
- compatibility with the SWE-ReX remote execution surface needed by SWE-agent

The intent is to keep Python orchestration and cloud/sandbox providers intact and replace only the repeated in-sandbox execution path.

## Documents

- ROADMAP.md — prioritized build sequence and exit criteria
- docs/benchmark-plan.md — how to measure the work
- docs/oss-landscape.md — current OSS survey and where not to duplicate work
- docs/swerex-hotpath.md — concrete current SWE-ReX costs to isolate
- docs/architecture.md — proposed native execution-plane architecture
- DECISIONS.md — project decision log
- SOURCES.md — primary repositories and references

## Status

Research and implementation plan initialized September 24, 2026. No performance claims are made yet. Numeric targets are experiment gates, not expected results.
