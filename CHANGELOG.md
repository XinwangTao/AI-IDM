# Changelog

All notable changes to this project. Versions are `MAJOR.MINOR.PATCH`; before 2.0 the CLI flags may
still change.

## [1.10.1] - 2026-10-20

### Fixed
- **Crash on non-UTF-8 consoles.** `idmclone --help` (and any Chinese output) raised
  `UnicodeEncodeError` under a cp1252 console — i.e. every English-locale Windows user. The CLI and
  the MCP server now force UTF-8 on stdout/stderr at startup, falling back to `errors="replace"`.
  Caught by CI on `windows-latest`; regression test in `tests/test_stdio.py`.

## [1.10.0] - 2026-10-20

### Added
- Keep-alive connection pool (`pool.py`): a worker fetches several chunks over one TCP/TLS connection.
  Works with direct connections, HTTP(S) proxies (CONNECT tunnels) and SOCKS5/SOCKS5H.
- `--no-keepalive` / `keepalive` config key / `keepalive` MCP argument.
- `pool` diagnostics in the download result: `{opened, reused, idle}`.
- `tests/test_pool.py`, `tests/run_all.py`.

### Fixed
- Pool kept a single idle connection per host, so concurrent workers overwrote each other (and leaked
  sockets). Now one host maps to a list of idle connections.
- A pooled connection closed by the peer while idle is now retried once on a fresh connection instead
  of failing the chunk with a backoff.

## [1.9.0] - 2026-10-20

### Added
- Dynamic chunking (default): `~4×` more chunks than connections, assigned at runtime from a work
  queue, so a slow connection no longer stalls the transfer. `--static-chunks` restores one-chunk-per-
  connection.
- `handoffs` counter distinguishing "chunk moved before it started" from "partially downloaded chunk
  moved".
- `tests/test_dynamic.py` (throttled-connection comparison).

### Changed
- `_claim()` honours a per-chunk `retry_at`, so a released chunk is not immediately re-grabbed by the
  connection that just failed it (was a hot retry loop).

## [1.8.0] - 2026-10-20

### Added
- Seam verification (default on): every chunk except the last reads 64 extra bytes and compares them
  with the neighbouring chunk already on disk; a mismatch triggers a single-stream re-download.
- `--open-range` for strict `bytes=N-` requests (wastes bandwidth, kept for fidelity).
- `seams_checked` / `seams_mismatched` in the result; `tests/test_seam.py`.

### Changed
- Default `Range` is closed: `bytes=N-(M+63)` - only 64 bytes of overhead per chunk.
- `config set` coerces boolean keys (`seam_verify`, `open_range`, …) from strings.

## [1.7.0] - 2026-10-20

### Added
- Optional sampling verification after download (`--verify-sample N`); a mismatch re-downloads the file
  as a single stream.
- `tests/test_verify.py`.

## [1.6.0] - 2026-10-20

### Added
- FTP/FTPS download with multiple connections and `REST`-based resume; `AUTH TLS` + `PROT P` for FTPS.
- `tests/test_ftp.py` with a minimal in-process FTP server.

## [1.5.0] - 2026-10-20

### Added
- Time-of-day speed limits (`config schedule add "09:00-18:00=1M"`); the limit switches **while a
  download is running**.
- `tests/test_schedule.py`.

## [1.4.0] - 2026-10-20

### Added
- Work stealing: a chunk that keeps failing is released back to the pool instead of aborting the whole
  download; bounded by `max(2, max_retries)` attempts.
- `reassigned` counter in the result; `tests/test_steal.py`.

### Fixed
- Internal failures also set the stop event, so `run()` reported `cancelled` instead of raising the
  actual error. User cancellation now has its own flag.

## [1.3.0] - 2026-10-20

### Added
- Config layer (`config.json`): global defaults plus per-site connection overrides with wildcard
  suffix matching (longest suffix wins).
- `--site-connections host=N`, `--timeout`, `--retries`; `tests/test_config.py`.

## [1.2.0] - 2026-10-20

### Added
- HLS (`m3u8`): master playlist selection (`best` / `worst` / index), `EXT-X-MAP`, `download_hls` MCP
  tool, resumable segment downloads.
- Pure-Python AES-128-CBC (NIST SP 800-38A test vector), so encrypted HLS needs no dependencies.

### Fixed
- A single chunk covering the whole file no longer sends `Range`, which used to trigger a pointless
  fallback re-download on servers without `Range` support.

## [1.1.0] - 2026-10-20

### Added
- SQLite job queue with atomic claim, restart recovery and parallel runner (`queue add|run|list|remove|retry`).
- SOCKS5 / SOCKS5H / system proxy support; 5 more MCP tools.

### Fixed
- The SOCKS5 urllib handler had to run before the built-in one (`handler_order = 100`), otherwise the
  proxy was silently bypassed.

## [1.0.0] - 2026-10-20

### Added
- HTTP(S) multi-connection download, `Range` splitting, resume, single-stream fallback, token-bucket
  throttling, checksums, custom headers.
- CLI (`get` / `probe` / `list`) and an MCP stdio server.
