# Contributing

Thanks for taking a look. This is a small, dependency-free project, so contributing is mostly about
keeping it that way.

## Ground rules

- **No third-party runtime dependencies.** Everything ships on the Python standard library. If a
  feature truly needs a library, it belongs behind an optional extra and must degrade gracefully.
- **No DRM circumvention, license-check bypass, or paywall logic.** Issues and pull requests about
  those will be closed.
- **Every behavior change needs a test that would fail before it.** The suite is self-contained — no
  network access — so it can run in CI on Linux and Windows.

## Run the tests

```bash
python tests/run_all.py          # all 11 suites
python tests/run_all.py -k pool  # filter by suite name
```

Individual suites are plain scripts, so you can run one directly:

```bash
python tests/test_seam.py
```

## Code layout

| File | Responsibility |
|---|---|
| `ai_idm/engine.py` | HTTP(S) engine: probe, chunking, workers, seams, verification |
| `ai_idm/pool.py` | keep-alive connection pool (HTTP, proxies, SOCKS5) |
| `ai_idm/proxy.py` | proxy parsing and SOCKS5 handshake |
| `ai_idm/ftp.py`, `ai_idm/hls.py` | protocol front ends |
| `ai_idm/queue.py` | SQLite job queue |
| `ai_idm/config.py` | defaults, per-site overrides, schedules |
| `ai_idm/cli.py`, `ai_idm/mcp_server.py` | entry points |

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before touching `engine.py` — it lists the
invariants that keep concurrent writes correct (claiming, write-position accounting, seam samples).

## Style

- Python 3.10+, `from __future__ import annotations`, type hints where they clarify.
- Comments and commit messages: Chinese or English are both fine. Keep them about *why*, not *what*.
- Keep functions small; `engine.py` is already the biggest file — new protocol code goes in its own module.

## Pull requests

1. One logical change per PR.
2. `python tests/run_all.py` green, plus a new/updated test for the change.
3. Describe the failure mode you fixed, not just the diff. If you measured something, include both
   numbers (before/after) and how you measured.

## Reporting bugs

Please include: the command line (or MCP call), the JSON error output, whether the server supports
`Range`, and whether a proxy is involved. A minimal reproduction against a local HTTP server is the
fastest path to a fix.
