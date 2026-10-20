# AI-IDM

[![CI](https://github.com/XinwangTao/AI-IDM/actions/workflows/ci.yml/badge.svg)](https://github.com/XinwangTao/AI-IDM/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

**AI-IDM** is a multi-connection download engine written with the **Python standard library only** —
no third-party dependencies. It speaks HTTP/HTTPS, HLS (`m3u8`, including AES-128 encrypted segments) and FTP/FTPS,
resumes from where it stopped, and ships an **MCP server** so AI agents can drive it.

> 中文说明见 [README.zh-CN.md](README.zh-CN.md)。

```
$ ai-idm get https://example.com/big.iso -o ~/Downloads/big.iso -n 8
  big.iso  100.0%  8 workers  12.4MB/s  done in 6.3s
```

## Features

| Area | What you get |
|---|---|
| **Multi-connection** | `Range` splitting, 8 connections by default, 1 MB minimum chunk |
| **Dynamic chunking** | ~4× more chunks than connections, claimed at runtime from a work queue — one slow connection cannot stall the transfer |
| **Connection reuse** | keep-alive pool: a worker fetches several chunks over the same TCP/TLS connection (no re-handshake per chunk) |
| **Resume** | progress in `.idmpart.json`, data in `<dest>.idmpart`, `os.replace()` on completion — never a half-written target file |
| **Integrity** | 64-byte overlap between adjacent chunks compared byte-by-byte; optional sampling at the end (`--verify-sample N`) |
| **Fallback** | server ignores `Range` → single stream, automatically |
| **Throttling** | token bucket, plus time-of-day schedules that switch **while downloading** |
| **Per-site limits** | `config set-site example.com 24`, wildcards supported (`*.cdn.com`) |
| **Proxies** | HTTP/HTTPS proxy, SOCKS5, SOCKS5H (remote DNS), Windows system proxy |
| **HLS** | master playlist quality selection, `EXT-X-MAP`, pure-Python AES-128-CBC, resumable |
| **FTP / FTPS** | multi-connection via `REST`, `AUTH TLS` + `PROT P` |
| **Queue** | SQLite job queue, atomic claim, survives restarts, parallel workers |
| **AI-ready** | MCP stdio server with 12 tools (`start_download`, `wait_download`, `queue_*`, `download_hls`, …) |

## Install

```bash
git clone https://github.com/XinwangTao/AI-IDM.git
cd AI-IDM
pip install -e .

# or run it without installing
python -m ai_idm get https://example.com/file.zip
```

Python 3.10+. No runtime dependencies.

## CLI

```bash
# download with 8 connections, verify the checksum afterwards
ai-idm get https://example.com/file.zip -o file.zip -n 8 --checksum sha256

# inspect only: size / Range support / ETag / filename
ai-idm probe https://example.com/file.zip

# interrupted? run the same command again — it resumes
ai-idm get https://example.com/file.zip -o file.zip

# throttle to 2 MB/s, or go through a proxy
ai-idm get https://example.com/file.zip --limit 2M --proxy socks5h://127.0.0.1:1080

# HLS: pick the best variant, download the segments, merge them
ai-idm hls https://example.com/master.m3u8 -o video.ts --quality best

# FTP / FTPS
ai-idm get ftp://user:pass@host/path/file.iso -o file.iso -n 4

# persistent queue
ai-idm queue add https://example.com/a.zip -o a.zip -n 8 --priority 5
ai-idm queue run --parallel 3
ai-idm queue list

# configuration
ai-idm config show
ai-idm config set connections=16 timeout=45
ai-idm config set-site example.com 24
ai-idm config schedule add "09:00-18:00=1M" --days mon,tue,wed,thu,fri
```

Every command prints a JSON result, so it composes with `jq` and with scripts.

## MCP server (for AI agents)

`ai-idm mcp` speaks MCP over stdio. Register it with your client:

```json
{
  "mcpServers": {
    "idm": {
      "command": "python",
      "args": ["-m", "ai_idm", "mcp"],
      "env": { "PYTHONPATH": "/path/to/AI-IDM" }
    }
  }
}
```

Tools: `probe_url`, `start_download`, `download_status`, `wait_download`, `cancel_download`,
`list_downloads`, `queue_add`, `queue_run`, `queue_list`, `queue_remove`, `queue_retry`,
`download_hls`.

An agent can then be told *"download these three URLs into `D:\dl`, 4 in parallel, tell me which
ones failed"* and it drives the tools itself.

## How it works

```
probe   HEAD → Range: bytes=0-0        → total size, ETag, Accept-Ranges
plan    chunks ≈ connections × 4
workers pull a chunk from the queue, reuse a kept-alive connection
        Range: bytes=N-(M+63)          → +64 bytes = seam sample (not written)
  ├─ write at offset into <dest>.idmpart
  ├─ compare the seam sample with the neighbour chunk already on disk
  └─ on failure: release the chunk, back off, another worker takes it
verify  seams → optional sampling → fsync → os.replace(<dest>.idmpart, <dest>)
```

Module map and concurrency model: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Design notes and provenance

This project began as an exercise: observe *why* a well-known commercial download manager behaves
the way it does, then rebuild that engine from scratch in Python. Several design decisions
(64-byte seams, dynamic chunking, connection reuse, chunk hand-off) come from **runtime observation**
of that program. All code here is original, written for Python; no code was copied from any product,
and no binary is redistributed. What is evidence-based versus my own engineering judgement is laid out
in [docs/BEHAVIOR-NOTES.md](docs/BEHAVIOR-NOTES.md).

There is no license-check bypass, DRM circumvention or paywall logic in this project, and none will
be accepted.

## Tests

```bash
python tests/run_all.py           # every suite
python tests/run_all.py -k seam   # one suite
```

12 suites / 47 checks, fully self-contained: local HTTP servers, a hand-written SOCKS5 server, a
minimal FTP server, TLS-free fixtures. CI runs them on Linux and Windows over Python 3.10 / 3.12 / 3.13.

What the suites actually assert is worth knowing — e.g. connection reuse is verified by *counting TCP
connections on the server side*, not by "the download succeeded". See the comments at the top of each
file under `tests/`.

## Known limits

| Not implemented | Notes |
|---|---|
| GUI | CLI + MCP only |
| Browser integration | Would need WFP filters, a browser extension and native messaging |
| DASH (`.mpd`) | HLS only |
| DRM (Widevine, …) | Out of scope by design |
| PAC scripts | Needs a JS engine; use an explicit proxy or `auto` |
| HTTP/2, BitTorrent, Metalink | Out of scope for now |

## License

MIT — see [LICENSE](LICENSE).
