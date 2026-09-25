# Pinned Third-Party Revisions

Every benchmark result must cite these SHAs. Update only deliberately and
re-run affected comparisons when a pin moves.

| Component | Revision | Role |
| --- | --- | --- |
| SWE-ReX (`https://github.com/SWE-agent/SWE-ReX`) | `5c995c365dfb1fd5bc56fda688be5d8538f9931f` (main, 2026-09-24) | Upstream Python baseline and P0.5 optimization target |

Install the baseline with:

```sh
pip install 'git+https://github.com/SWE-agent/SWE-ReX@5c995c365dfb1fd5bc56fda688be5d8538f9931f'
```

| SWE-agent (`https://github.com/SWE-agent/SWE-agent`) | `3ea751c087f32b16e039a2233dd6eefecef325d5` (main, 2026-09-25) | Workload-D trace replay driver (v1.1.0) |
