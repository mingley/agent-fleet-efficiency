# Proposed Architecture: agent-execd

## Scope

A small Rust daemon for the execution plane inside an existing sandbox.

It is not:

- a VM manager
- a Kubernetes operator
- an agent framework
- an LLM client
- a build system
- a distributed scheduler

## Placement

Python/TypeScript agent framework
        |
   SWE-ReX/OpenHands/RL adapter
        |
   HTTP or Unix socket
        |
   agent-execd (Rust)
        |
   PTY/process/files/resource accounting
        |
   existing sandbox OS
        |
   Docker / E2B / Kubernetes / VM / Firecracker

Start with the protocol required for compatibility. Add a more efficient transport only after benchmarks justify it.

## Responsibilities

- session manager
- PTY manager
- process supervisor
- command timeout and cancellation
- process-tree cleanup/reaping
- file service
- bounded stdout/stderr handling
- resource accounting
- health/version/auth compatibility

## Constraints

### Compatibility before cleverness

The first version should emulate the SWE-ReX remote behaviors needed by SWE-agent closely enough that the deployment/runtime layer does not need to know the executor is Rust.

### One small binary

Avoid requiring a Python runtime solely to host the remote executor. Make Linux x86_64/aarch64 packaging straightforward.

### Persistent sessions

Do not spawn a new shell for every command. Preserve shell state such as cwd, exports, and shell functions.

### Correct cancellation

Agent commands spawn descendants. Cancellation must terminate and reap the process tree reliably. Correctness outranks lower p99 latency.

### Bounded backpressure

Commands can emit unbounded output. Buffers and streams need explicit limits, truncation/streaming semantics, and cancellation behavior.

### Resource accounting

Expose wall time, CPU time, max RSS where practical, exit code/signal, and output counts. The executor should make the benchmark easier to run.

## Proposed crate layout

- crates/execd-core — sessions, commands, process trees, PTY abstraction
- crates/execd-protocol — compatibility types
- crates/execd-server — HTTP/UDS service
- crates/execd-metrics — proc/cgroup accounting
- crates/trace-replay — benchmark trace schema and replay client
- benchmarks/fixtures — deterministic workloads
- benchmarks/scripts — orchestration/collection

## Milestones

### M1 — Compatibility spike

Implement health/version, create session, run command, timeout, interrupt, and teardown.

### M2 — File APIs and parity

Add the file primitives actually used by the integration and build a compatibility test suite that runs identical vectors against Python and Rust servers.

### M3 — Benchmark

Publish idle RSS, 10k tiny-command cost, 100 concurrent sessions, several trace replays, and one build-heavy workload.

### M4 — Upstreamable packaging

Produce prebuilt Linux binaries and an integration path that can be proposed upstream without requiring agent-code changes.
