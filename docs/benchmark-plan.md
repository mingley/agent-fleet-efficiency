# Benchmark Plan

## Principle

Optimize core-seconds per useful completed task, not requests per second in isolation.

Every result must answer:

1. How much host CPU/RAM is saved at realistic concurrency?
2. What percentage of the full task did this component represent before optimization?

## Host recording

For every run record:

- CPU model and core count
- RAM
- kernel and OS
- filesystem and mount options
- container/VM backend versions
- runtime/language versions
- exact git SHAs
- CPU affinity and governor/power settings where relevant

Pin CPU sets for runtime comparisons. Avoid oversubscription until the concurrency sweep intentionally tests it.

## Workload A — Executor microbenchmarks

- health check
- create/delete session
- true
- pwd
- output sizes of 1 KiB, 64 KiB, and 1 MiB
- persistent environment mutation
- background process + interrupt
- 1,000 and 10,000 sequential tiny commands
- 100 concurrent persistent sessions

Purpose: isolate executor and protocol tax.

## Workload B — File/workspace operations

- read/write/upload 4 KiB, 1 MiB, and 100 MiB files
- unpack source tree
- clone from local mirror
- Git worktree creation
- reflink copy where supported
- overlayfs workspace creation

Purpose: identify copying/setup waste.

## Workload C — Build/test

Use fixed public repositories or deterministic fixtures for:

- Rust/Cargo
- Python/pytest
- Node
- Java/Gradle or Maven

Run:

1. cold sandbox + cold caches
2. warm sandbox + cold language caches
3. shared language/compiler caches
4. warm workspace snapshot/CoW

Purpose: determine whether avoided setup/build work dominates runtime-level improvements.

## Workload D — Real agent trace replay

Capture execution traces from coding-agent runs without model inference.

A trace should contain enough to replay the execution plane:

- command
- cwd
- environment deltas
- input/output byte counts
- duration
- exit code
- file transfer sizes
- session lifecycle events

Prefer public SWE-bench/SWE-agent-style workloads on open repositories at fixed commits.

## Primary metrics

- total executor CPU time
- total child-process CPU time
- completed traces per host-hour
- steady-state/peak RSS
- p50/p95/p99 command latency
- startup/reset latency

Secondary:

- context switches
- page faults
- filesystem bytes
- network bytes
- subprocess count
- overlayfs copy-up bytes
- cache hits/misses

Linux collection options: cgroup v2 accounting, procfs, pidstat, perf stat, and optional eBPF profiling. Keep expensive profiling off headline throughput runs if it changes results.

## Required comparison matrix

| Variant | Orchestration | Executor | Workspace | Caches |
| --- | --- | --- | --- | --- |
| Current | SWE-ReX Python | upstream Python | default | default |
| Optimized Python | SWE-ReX Python | optimized Python | same | same |
| Native executor | SWE-ReX Python | Rust agent-execd | same | same |
| Native + cache | SWE-ReX Python | Rust | same | shared |
| Native + CoW | SWE-ReX Python | Rust | CoW/worktree/overlay | shared |

## Reporting rules

- publish absolute CPU-seconds and RSS, not only percentages
- separate executor CPU from executed-command CPU
- publish cold and warm results
- include CPU consumed by supporting cache services
- report failures and semantic incompatibilities
- do not extrapolate fleet savings from a microbenchmark without trace-level evidence
- preserve raw benchmark artifacts under benchmark-results in releases/CI artifacts rather than committing large outputs to git
