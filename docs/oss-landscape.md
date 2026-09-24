# OSS Landscape

Snapshot date: September 24, 2026.

This survey is intentionally scoped to software that affects CPU/RAM consumed by agent tool execution, sandboxes, workspaces, builds, and RL/eval environments.

## Tier A — High-value optimization/integration targets

### SWE-ReX — Python, MIT

Repository: https://github.com/SWE-agent/SWE-ReX

Remote execution abstraction for sandboxed shell environments, designed for massively parallel agent runs and used by SWE-agent.

Why it matters: session/process/file/remote execution sits directly on the repeated tool-call path.

Recommendation: first benchmark and native-executor compatibility target.

### OpenHands Software Agent SDK / Agent Server — Python

Repository: https://github.com/OpenHands/software-agent-sdk

Owns agent tools, command execution, workspaces, events, conversations, and parallel-agent behavior.

Recommendation: reuse the same native executor after SWE-ReX if measurements prove the process/session layer matters. Do not rewrite OpenHands wholesale.

### Prime Intellect Verifiers — Python, MIT

Repository: https://github.com/PrimeIntellect-ai/verifiers

RL environment and evaluation framework. Tool-heavy rollout fan-out can multiply environment execution overhead.

Recommendation: keep Python environment authoring; experiment with a native process/session worker only after executor benchmarks exist.

## Tier B — Integrate; do not replace first

### E2B Runtime — Go, Apache-2.0

Repository: https://github.com/e2b-dev/runtime

Firecracker-oriented sandbox infrastructure with snapshot restore and copy-on-write/lazy-loading techniques.

Why not rewrite: it already attacks dominant lifecycle costs architecturally.

Use as a backend/benchmark peer.

### Kubernetes SIG Agent Sandbox — Go

Repository: https://github.com/kubernetes-sigs/agent-sandbox

Kubernetes-native sandbox lifecycle with warm pools and stateful sandbox abstractions.

Why not rewrite: controller language is unlikely to dominate tool execution; warm-pool behavior is already the important architectural optimization.

### ArcBox — Rust

Repository: https://github.com/arcboxlabs/arcbox

Rust-native sandbox/container/VM work.

Meaning: low-level Rust sandbox infrastructure is not an empty OSS niche.

### Ephemeral Sandbox — Rust

Repository: https://github.com/Ephemeral-AI-Lab/ephemeral-sandbox

Rust core for isolated parallel coding-agent workspaces.

Meaning: study and benchmark rather than duplicate.

### Roder — Rust

Project: https://roder.sh/mission/

Rust runtime infrastructure for coding agents.

Meaning: a generic Rust agent runtime is not the high-yield gap.

## Tier C — Existing native building blocks

### sccache — Rust

Repository: https://github.com/mozilla/sccache

Compiler cache for Rust/C/C++/CUDA and remote storage/distributed scenarios.

Recommendation: inject into disposable agent environments and measure avoided compilation.

### Buck2 — Rust

Repository: https://github.com/facebook/buck2

Low-overhead incremental build system.

Recommendation: where usable, build-system architecture can dominate executor-language gains. Do not invent another build system.

### Model Context Protocol Rust SDK / chuk-mcp-rs

Official SDK: https://github.com/modelcontextprotocol/rust-sdk

IBM implementation: https://github.com/IBM/chuk-mcp-rs

Native Rust can substantially reduce protocol overhead in microbenchmarks, but the fleet question is whether those microseconds are meaningful next to shell/build/test time.

Recommendation: secondary target until trace profiling says otherwise.

### Chimera — Rust GitHub Actions runner

Repository: https://github.com/quinck-io/chimera

Recommendation: benchmark/contribute rather than starting another runner rewrite.

## Tier D — Possible rewrite candidates, benchmark first

### Anthropic Sandbox Runtime — TypeScript/Node

Repository: https://github.com/anthropics/sandbox-runtime

Wraps native OS isolation primitives.

A Rust wrapper could reduce Node startup/RSS/orchestration cost, but the core isolation work already happens in native tools. Benchmark before investing.

## Main conclusion

The OSS gap is not "a Rust agent framework" or "a Rust VM runtime."

The highest-value gap worth testing is a small framework-neutral native execution plane that can:

- replace repeated Python/Node process/session hot paths
- preserve existing high-level agent APIs
- run inside existing sandboxes
- expose resource accounting
- pair with systematic reuse of immutable workspace/build/package state

Even that hypothesis must survive the optimized-Python benchmark before implementation expands.
