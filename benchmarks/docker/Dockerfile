# Linux comparison environment for the benchmark harness.
#
# Build context is the REPO ROOT:
#   docker buildx build --platform linux/amd64 \
#     -f benchmarks/docker/Dockerfile -t fleet-bench --load .
#
# Pinned third-party revisions (see benchmarks/pins.md):
#   SWE-ReX @ 5c995c365dfb1fd5bc56fda688be5d8538f9931f
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# git: required for the pip install from a git URL below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Exact pinned upstream baseline from benchmarks/pins.md.
RUN pip install 'git+https://github.com/SWE-agent/SWE-ReX@5c995c365dfb1fd5bc56fda688be5d8538f9931f'

# Harness scripts (record_host.py, run_workload_a.py, ...).
COPY benchmarks/scripts/ /opt/harness/scripts/

WORKDIR /opt/harness

# Mount the host's gitignored benchmark-results/ here at run time:
#   -v "$PWD/benchmark-results:/results"
RUN mkdir -p /results
