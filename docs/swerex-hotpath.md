# SWE-ReX Hot-Path Findings

Inspected against SWE-agent/SWE-ReX main on September 24, 2026.

These are benchmark hypotheses, not claims that any single item dominates end-to-end agent cost.

## Why optimize Python first

A Rust prototype should be compared with a reasonably optimized implementation of the same architecture. Otherwise Rust can receive credit for fixes that have nothing to do with Rust.

## 1. Reuse remote HTTP connections

RemoteRuntime currently creates fresh aiohttp ClientSession instances with TCPConnector(force_close=True) in its request paths.

Experiment:

- keep one client session per runtime
- allow normal keep-alive/connection pooling
- close it with the runtime
- measure connection CPU, latency, allocations, and FD behavior under concurrency

This is a small upstreamable experiment with no protocol change.

## 2. Measure the per-command syntax-check process

The normal BashSession path calls a bash syntax checker before sending the real command to the persistent shell. The check invokes bash -n using subprocess.run.

For traces dominated by tiny commands this can mean an additional process launch per command.

Compare:

- current always-on validation
- optional validation
- validation only when needed
- direct execution with syntax errors reported by the persistent shell

Preserve error semantics where callers depend on them.

## 3. Reduce PTY round trips for exit status

After command completion, the current session path sends another shell command to print a framed exit code, waits for it, parses it, and then waits for another prompt.

Prototype a single submission/framing strategy that returns command completion and exit status without the extra round trip while preserving:

- shell state
- arbitrary output
- timeout behavior
- interrupts
- interactive-mode behavior

## 4. Replace fixed startup sleeps

Shell startup currently includes fixed sleep delays around startup/readiness behavior.

At fleet scale deterministic sleeps add startup latency even when a shell becomes ready sooner.

Test prompt/readiness synchronization instead.

## 5. Measure response-body buffering

The FastAPI request-id/idempotency middleware consumes complete response bodies and reconstructs a new response so it can cache the last response.

Measure allocations/copies for command-output-heavy and file-heavy workloads. If material, make idempotency storage bounded or endpoint-specific.

## 6. Stream uploads

The upload path reads the complete uploaded file before writing/moving/unzipping it.

For larger artifacts compare chunked streaming to reduce peak RSS and avoid unnecessary copies.

## Required three-way benchmark

1. Current upstream Python.
2. Optimized Python with safe fixes above.
3. Rust agent-execd with equivalent semantics.

Only the delta between 2 and 3 is evidence for the Rust implementation.

## Decision rule

If optimized Python removes most of the measured control-plane cost, do not force a Rust rewrite. Shift the project toward shared caches, CoW workspaces, warm pools, and avoided builds/setup.
