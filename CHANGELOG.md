# Changelog

> **Naming:** the project is called **AI-IDM**. Before v1.11.0 it was named `idmclone`
> (repo, distribution and CLI); `ai_idm` is the Python import package.

All notable changes to this project. Versions are `MAJOR.MINOR.PATCH`; before 2.0 the CLI flags may
still change.

## Extension 1.2.0 - 2026-10-21

### Added
- **In-page download HUD.** A content script renders a floating card in the bottom-right corner
  of the page with live progress, speed and the final result. It does not depend on OS
  notifications, which Windows Focus Assist or per-browser notification settings can suppress.
  The background worker injects it on demand (`chrome.scripting`) so tabs opened before the
  extension was reloaded still get it.
- The popup footer now shows the **extension version** next to the service version, so it is
  obvious whether a reload took effect.

## Extension 1.1.0 - 2026-10-21

### Added
- **Completion / failure notifications.** The extension now tracks the task id returned by
  `POST /add` and polls `/status` (2 s interval, `idm-tick` alarm as a fallback) until the job
  finishes, then raises "AI-IDM 下载完成" with the saved path — or "AI-IDM 下载失败" with the
  error. Previously a successful takeover was invisible: the browser download showed as
  "已取消" and the file simply appeared in `Downloads`, which looked like a failure.
- The hand-off notification now states the connection count.

## [1.12.2] - 2026-10-21

### Fixed
- **Mirror hosts download in a single request.** GitHub accelerators (`xget.xi-xu.me`,
  `ghproxy.com`, …) rate-limit by request count: the usual "HEAD probe + GET body" pattern
  makes the second request 429 and pay a full `Retry-After` (10 s). Those hosts now take a
  dedicated path that opens one `GET`, derives size/name from the response headers and streams
  that same response to disk — no HEAD, no `Range`, no 429. Measured on a 17 MB asset:
  29 s → 5.4 s (on par with curl).
- `Retry-After` is honoured on `429/5xx` in both `probe()` and segment retries.
- `POST /add` reports the effective connection count (1 for mirror hosts).
- Reverts the 1.12.1 URL rewriting: mirror URLs are passed through untouched, since the user's
  accelerator is what makes those hosts reachable in the first place.

### Added
- `tests/test_mirror.py` (5 checks) — asserts exactly one GET, no HEAD, no `Range`.
  Suite count is now 14 / 60 checks.

## [1.12.1] - 2026-10-21

### Fixed
- **GitHub release downloads no longer stall behind "download accelerator" mirrors.**
  The extension now normalizes known GitHub mirror hosts (`xget.xi-xu.me`, `ghproxy.com`, …)
  whose `/gh/<path>` form maps back to `github.com/<path>`, so the engine fetches the
  canonical URL directly instead of hitting the mirror's rate limit (HTTP 429).
- The engine `probe()` now retries on `429/500/502/503/504` with exponential backoff
  instead of failing immediately on a transient `429`.
- `POST /add` no longer returns the literal placeholder `"(按响应头命名)"` as `dest`; it
  now returns `null` until the real name is known.

## [1.12.0] - 2026-10-20

### Added
- **Browser takeover.** `ai-idm serve` starts a loopback HTTP service (default `127.0.0.1:13391`)
  that turns browser downloads into engine jobs. Ships with a Manifest V3 extension in
  `browser-extension/` for Chrome/Edge: auto interception of `chrome.downloads.onCreated`,
  context-menu entries (link / media / page) and a popup with live progress.
- HTTP API: `GET /ping|/status|/status/<id>|/events|/config`, `POST /add|/cancel/<id>|/remove/<id>`,
  plus a self-contained web panel at `/`. JSON and form-encoded bodies both accepted.
- `POST /add` forwards `referer`, `ua`, `cookie` and arbitrary `headers`; `.m3u8` URLs (or `hls: true`)
  go through the HLS downloader.
- Token auth (`X-AI-IDM-Token`, constant-time compare), loopback-only binding and client check,
  256 KB body cap, scheme allow-list and CRLF-injection filtering. Token is generated on first run
  (`secrets.token_urlsafe`) and stored as `server_token` in `config.json`; `ai-idm token` prints it.
- New config keys: `server_host`, `server_port`, `server_token`, `server_parallel`, `download_dir`.
- `Download(dest_dir=…)` and `Runner(dest_dir=…)`: when no explicit destination is given, the file
  is written into that directory under the name from `Content-Disposition`.
- `Runner.cancel(tid)` and an active-download registry.
- `tests/test_server.py` (8 checks); `docs/BROWSER-INTEGRATION.md`.

### Fixed
- A cancelled queue task was recorded as `done`: `Download.run()` returns a snapshot with
  `status="cancelled"` instead of raising, and the runner now honours it.

## [1.11.0] - 2026-10-20

### Changed
- **Renamed to AI-IDM.** Repository `idmclone` -> `AI-IDM` (GitHub redirects the old URLs), distribution
  `idmclone` -> `ai-idm`, Python package `idmclone` -> `ai_idm`, console script `idmclone` -> `ai-idm`,
  MCP server name `idmclone` -> `ai-idm`, user data directory `%LOCALAPPDATA%\idmclone` -> `%LOCALAPPDATA%\ai-idm`.
- `AI_IDM_HOME` is the new environment variable for the data directory; `IDMCLONE_HOME` is still honoured.
- `python -m idmclone` becomes `python -m ai_idm`.

## [1.10.1] - 2026-10-20

### Fixed
- **Crash on non-UTF-8 consoles.** `ai-idm --help` (and any Chinese output) raised
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
