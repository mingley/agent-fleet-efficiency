# Roadmap

## Prioritization model

Rank work by four questions:

- Fleet impact: is the cost paid for every agent/tool call?
- Adoption path: can existing OSS use the improvement without a rewrite?
- OSS gap: does a strong native implementation already exist?
- Tractability: can useful evidence be produced in weeks rather than by building a new cloud platform?

## P0 — Build the benchmark before the rewrite

Deliver a reproducible harness that replays representative coding-agent execution traces without LLM inference.

Workloads:

1. Tiny commands: true, pwd, git status, small file reads.
2. Persistent shell: cd, environment mutation, background process, interrupt, reconnect.
3. Repository operations: clone/fetch/worktree/diff/file transfer.
4. Build/test workloads for Rust, Python, Node, and Java.
5. SWE-agent trace replay on public repositories at fixed commits.
6. Concurrency sweep at 1, 10, 100, and host saturation.

Metrics:

- user + system CPU-seconds per trace
- child-process CPU separated from executor CPU
- p50/p95/p99 dispatch latency
- idle, steady-state, and peak RSS
- process/thread counts
- context switches and syscalls where practical
- filesystem/network bytes
- sandbox startup/reset latency
- cache hit ratio
- completed traces per host-hour

Baselines:

- current upstream SWE-ReX Python runtime
- optimized SWE-ReX Python runtime
- direct local subprocess/PTY baseline
- Rust agent-execd prototype
- Docker sandbox
- at least one snapshot/warm-pool-oriented sandbox path if practical

Exit criterion: do not pursue a broad rewrite if profiling shows the execution layer is a small fraction of total host cost. If builds/tests/setup dominate, move effort to P2/P3.

## P0.5 — Optimize SWE-ReX before crediting Rust

Current source inspection found several language-neutral costs to isolate:

- reuse a persistent aiohttp client session instead of forcing a fresh connection per remote request
- benchmark making the per-command bash syntax-check subprocess optional
- reduce the extra PTY round trip used to retrieve command exit status
- replace fixed shell-startup sleeps with readiness synchronization
- measure unconditional response-body buffering in request-id middleware
- stream uploads rather than reading complete files into memory before writing

The benchmark must compare:

1. Current upstream Python.
2. Optimized Python with safe architectural fixes.
3. Rust agent-execd.

Only the delta from 2 to 3 is evidence for Rust.

Exit criterion: if optimized Python removes most of the execution-plane tax, contribute those fixes upstream and move to cache/workspace reuse.

## P1 — agent-execd: Rust execution core

Why SWE-ReX first:

- Python implementation in the repeated command path
- explicitly intended for massively parallel sandboxed execution
- already used by SWE-agent
- MIT license
- backend-neutral placement above Docker/VM/Kubernetes/cloud sandbox providers

Compatibility surface:

- health/version
- create/close persistent shell session
- run command in session
- interrupt/cancel command
- timeout and exit-status semantics
- read/write/upload file primitives needed by callers
- auth token handling

Proposed stack:

- Tokio
- Axum/Hyper for compatibility HTTP
- portable-pty initially, with direct Unix PTY only if profiling justifies it
- rustix/nix for process/session controls
- process groups and optionally cgroups for kill-tree correctness
- bounded buffers and explicit backpressure
- optional Unix domain socket transport for same-host use
- per-command resource accounting

Experiment gates:

- materially lower idle RSS per worker than optimized Python
- lower executor CPU on 10,000 tiny command dispatches
- semantic parity on persistent/interactive session tests
- less than 2% regression on long commands where executor overhead should disappear
- measurable host-density improvement at at least 100 concurrent short/tool-heavy workers before making fleet-efficiency claims

Upstream strategy: preserve SWE-ReX Python deployment abstractions. Prefer an optional native executor over an incompatible fork.

## P2 — Shared cache/bootstrap injection

This may save more CPU than the language rewrite.

Disposable agents repeatedly redo deterministic setup:

- compiler work
- package installation
- dependency downloads
- repository cloning
- toolchain bootstrap
- indexing/test discovery

Initial adapters:

- sccache for Rust/C/C++/CUDA
- Cargo registry and git caches
- uv/pip wheel/package caches
- npm/pnpm/yarn caches
- Maven/Gradle caches
- Git object/reference cache

Use known cache semantics first. Do not start with arbitrary command-result memoization.

Measure cold versus warm CPU-seconds and wall time, especially when many agents attack the same repository/version concurrently.

## P3 — Copy-on-write workspaces and warm workers

Evaluate in order:

1. Git worktrees.
2. Filesystem reflinks where supported.
3. overlayfs lowerdir plus per-agent upperdir.
4. Existing backend snapshots and warm pools.

Do not rebuild E2B snapshot management or Kubernetes Agent Sandbox warm pools. Integrate them.

Measure workspace creation CPU, I/O, bytes copied, reset latency, and agent density.

## P4 — OpenHands integration

If P1 proves the process/session layer is significant, reuse agent-execd behind the OpenHands command/workspace execution layer.

Do not rewrite the OpenHands SDK or agent logic.

Goal: one native execution primitive reusable by multiple agent frameworks.

## P5 — RL/eval integration

Target tool-heavy rollout environments such as Prime Intellect Verifiers.

Keep Python as the environment authoring layer. Use the native worker only for repeated process/session/file/sandbox communication where measurements show value.

Measure rollout-host CPU separately from model inference.

## Explicitly deprioritized

### Generic Rust agent framework

Already crowded. Codex is Rust, Roder exists, and multiple Rust agent runtimes/sandbox projects exist.

### New VM/Firecracker runtime

E2B Runtime, Kubernetes Agent Sandbox, ArcBox, and other systems already attack lifecycle/startup costs.

### Generic MCP rewrite

The Rust MCP ecosystem already exists. Protocol microseconds are likely secondary to shell/build/test time until profiling proves otherwise.

### New GitHub Actions runner

Rust alternatives such as Chimera already exist. Benchmark or contribute rather than start another implementation.

### New build system

Buck2 and sccache already attack build efficiency at a more fundamental level.

## Definition of success

A useful result is one of:

- an upstream Python fix that reduces CPU/RSS materially
- a native executor that reduces core-seconds/RSS beyond optimized Python
- shared caches or CoW workspaces that remove more repeated work than the runtime rewrite
- evidence that a proposed rewrite is not worth doing

Negative results are valid output.

## Execution record (2026-09-24)

- P0 benchmark harness: DONE. Workloads A/B/C, server workload,
  trace schema + validator + replay harness, dual-env Docker image,
  resumable 13-arm matrix, summarizer. Full matrix green (0 failures).
- P0.5 optimized Python: DONE, exit reviewed (DECISIONS.md). Six
  flag-gated patches; local flags halve per-command latency
  (113ms -> 55ms Linux N=1000); server flags latency-neutral at
  sequential localhost shape.
- P1 agent-execd: DONE, gate PASSED (DECISIONS.md). Wire-compatible
  Rust server (10 routes, parity 43/43), N=200 gate: rust tiny
  0.57ms vs opt-Python 58ms vs upstream 141ms; Linux rerun confirms
  (0.48ms vs 117.73ms); 20-way concurrency 6ms/7.7MB vs 2.9s/74MB.
- P2 build caches: harness DONE, initial numbers captured
  (cold/warm/touch for Rust/Python/C fixtures; sccache/ccache arms
  record skipped when absent). Real-repo measurements remain.
- P3 provisioning: harness DONE, initial numbers captured (10k-file
  tree macOS: clone 5.5s, worktree 1.9s, cp 9.6s, tar 10.4s,
  reflink 7.1s). Snapshot-mount/NFS comparisons remain.
- P4/P5 integrations: DEFERRED. No evidence yet that a second
  framework or RL rollout needs the primitive; revisit when a
  concrete consumer with measurements appears. The compat surface
  (docs/p1-compat.md) and parity suite keep the option cheap.
