# Changelog

> **Naming:** the project is called **AI-IDM**. Before v1.11.0 it was named `idmclone`
> (repo, distribution and CLI); `ai_idm` is the Python import package.

All notable changes to this project. Versions are `MAJOR.MINOR.PATCH`; before 2.0 the CLI flags may
still change.

## Extension 1.7.0 - 2026-10-21

### Fixed
- **浮层 × 关闭真正关得掉了。** 1.5.0 起 × 只是从 DOM 移除卡片，下载进行中时 1.5s 内的
  下一条进度推送会把它原样重建出来。现在 × 把任务加入页面会话内的 dismissed 屏蔽集，
  该任务后续所有浮层消息一律忽略；下载本身不受影响（完成/失败仍走系统通知与徽标）。
  页面刷新/跳转后屏蔽自然清空（卡片本来也随文档销毁）。

## Extension 1.6.0 - 2026-10-21

### Added
- **弹窗任务行点击复制。** 点任务行即复制落盘路径（尚未落盘的任务复制 URL），底部状态栏
  提示「已复制」。此前想拿文件完整路径只能去下载目录里翻。

## Extension 1.5.0 - 2026-10-21

### Added
- **同页多任务浮层堆叠。** content script 原先每页一张固定单例卡：同一标签页里第 2 个被
  接管任务会覆盖第 1 个的进度，前一个的完成/失败终态也被顶掉。现在浮层消息携带任务 id
  （track 的初始推送与轮询更新均带），content 侧按任务 id 建卡/更新，多卡纵向堆叠（新卡
  贴近右下角、向上生长），终态停留（完成 6s / 失败 12s）后从 DOM 移除、堆叠收缩。
- 单任务时视觉与 1.2.x 一致（样式、三态文案、停留时长均未变）。

### Fixed
- 终态卡片原先只隐藏不销毁（× 与超时都只是加 class），长会话里 DOM 节点持续累积；现在
  一律从 DOM 移除。

## Extension 1.4.0 - 2026-10-21

### Added
- **弹窗任务管理。** 任务列表每一行带操作按钮：进行中（pending/running）显示「取消」
  （`POST /cancel/<id>`，运行中的会中断并保留断点），已结束（done/error/cancelled）显示
  「移除」（`POST /remove/<id>`，只删记录不删文件）。操作后立即刷新列表，失败原因显示在
  底部状态栏。此前弹窗只能看不能管，要取消得去网页面板。

### Fixed
- 任务名/错误信息渲染改为 HTML 转义：名称可能来自远端 `Content-Disposition`，此前直接
  拼进 innerHTML，存在注入面。

## Extension 1.3.0 - 2026-10-21

### Added
- **跟踪状态跨 service worker 重启持久化。** 被跟踪任务表（PENDING）原先只在 MV3 service
  worker 的内存里：worker 被休眠/杀掉重启后清空，而 `idm-tick` 闹钟唤醒的 `pollOnce()` 见
  空表直接返回——"worker 被休眠时靠它续上完成通知"的兜底实际不成立，长下载的完成/失败
  通知与页面浮层终态会静默丢失。现在任务登记与收尾都把快照写入 `chrome.storage.session`
  （浏览器会话内存活），SW 重启后恢复并续上轮询；storage 不可用时降级为原内存跟踪。
- 恢复路径不重发初始「排队中」浮层，避免唤醒时闪一下。速度采样不持久化：恢复后首个
  采样周期速度显示 0 属预期，下一轮即恢复。

### Fixed
- 任务收尾时同步清理其速度采样条目（原先跨 SW 生命周期缓慢累积）。

## Extension 1.2.1 - 2026-10-21

### Fixed
- **浮层不再把"排队中"显示成 0%。** 任务处于 `pending`（等并发槽）时，浮层原先显示
  "AI-IDM 下载中 0.0%"，看起来像卡死——实际可能只是排在别的任务后面。现在区分三种状态：
  排队中（"排队中 · 等待空闲连接"）、连接中（已开跑但还没探到大小）、下载中（百分比 + 速度）。

## [1.12.5] - 2026-10-21

### Fixed
- **镜像整取路径的 ±1 字节容错补齐。** 1.12.4 只修了单连接整取；镜像单请求路径
  （`_run_single_request`）仍是 `pos < total` 硬判，服务器少发 1 字节照样失败。现同样容忍 ±1。
- **`/add` 去重的并发竞态。** 由"先查再插"改为原子 `Queue.find_or_add()`（同一把锁内完成
  查重 + 插入），避免两个并发 `/add` 各建一条重复任务。
- **去重命中不再重复推送 `added` 事件**，`/events` 不会再被同一 URL 的周期补加刷屏。

### Changed
- `tasks(url)` 增加索引 `idx_tasks_url`，`/add` 查重从全表扫变为索引查找。

### Tests
- `tests/test_dedup_purge.py` 扩展为 10 检查：原子 `find_or_add`、`purge`、单连接与镜像两条
  路径的 ±1 字节容错。

## [1.12.4] - 2026-10-21

### Fixed
- **±1 字节大小误判。** 少数服务器上报的 `Content-Length` 比实际 body 多 1 字节（实测
  `大小不符: 7132 != 7133`，队列里约 15 条因此失败）。单连接整取路径现在容忍 ±1 字节、
  采信实际读到的字节数，不再把完好文件判死。
- **重复入队。** `POST /add` 现在对 `url+dest+headers+proxy` 相同的**活跃**任务（pending/running）
  去重，命中则返回已有任务 id（`"deduped": true`）。此前同一 URL 会被周期性重复补加
  （实测间隔约 8–10 秒）撑大队列。由 `config dedup_add` 控制，默认开。

### Added
- **`ai-idm queue purge`**：批量清理记录，`--status done,error,cancelled`（默认），可选 `--older-than N`。
- **`config retain_days`（默认 7）**：`ai-idm serve` 启动时自动清理 N 天前的已完成/失败记录，
  避免队列无限增长；设为 0 关闭。
- `tests/test_dedup_purge.py`（7 检查）：去重命中/未命中、purge 只删指定状态、±1 字节下载成功。

## [1.12.3] - 2026-10-21

### Changed
- **MCP downloads honour `download_dir`.** `start_download` and `queue_run` now pass the
  configured `download_dir` to the engine, so downloads started by an AI agent land in the same
  folder as browser takeovers instead of the server process's working directory.

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
