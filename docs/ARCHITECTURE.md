# Architecture

```
ai_idm/
├── engine.py      HTTP(S) download engine: probe, chunk planning, workers, seams, verification
├── pool.py        keep-alive connection pool (http.client) — direct / HTTP proxy / SOCKS5
├── proxy.py       proxy parsing, SOCKS5 handshake, urllib opener wiring
├── ftp.py         FTP/FTPS download with multi-connection REST
├── hls.py         m3u8 parsing, AES-128-CBC, segment download + merge
├── queue.py       SQLite job queue and parallel runner
├── config.py      user config: defaults, per-site concurrency, time-of-day limits
├── stdio.py       force UTF-8 on stdout/stderr (Windows cp1252 safety)
├── cli.py         argparse front end, JSON output
├── server.py      browser-takeover HTTP service (token auth, queue API, web panel)
└── mcp_server.py  MCP stdio server exposing 12 tools
```

Everything is standard library only. `engine.py` is the core; the rest is either a protocol
front end (`ftp`, `hls`), a scheduling layer (`queue`), or an entry point (`cli`, `mcp_server`,
`server`).

## Download lifecycle (`engine.Download.run`)

1. **Probe** — `HEAD`, falling back to `GET Range: bytes=0-0` when the server does not answer `HEAD`.
   Yields total size, `Accept-Ranges`, `ETag`, `Last-Modified`, and the **final URL** after redirects.
2. **Plan** — `plan_segments(total, connections, min_seg, dynamic)`:
   - `dynamic=True` (default): `target = min(256, max(conn, conn*4))`, `size = max(min_seg, total//target)`,
     then split evenly. Chunks are therefore 4× more numerous than connections.
   - `dynamic=False`: one chunk per connection (the old behavior, `--static-chunks`).
   - A file smaller than `2 * min_seg` or `connections <= 1` degenerates to a single chunk.
3. **Open the part file** — `<dest>.idmpart`, pre-truncated to `total`; progress lives separately in
   `<dest>.idmpart.json`. The **target path is only created by the final `os.replace`**.
4. **Workers** — `min(connections, len(segments))` threads run `_worker` → `_claim` → `_fetch_segment`.
5. **Verify** — seam comparison (see below), then optional sampling (`--verify-sample N`).
6. **Finish** — `flush` + `fsync` + `os.replace`, then checksum if requested, then remove the state file.

If the server ignores `Range` (a `200` answer to a ranged request), the engine sets `_fallback`,
clears the stop flag and restarts as a single stream into the same part file.

## The invariants that keep it correct

These are the things that bit during development; each is enforced in code and covered by a test.

| Invariant | Why |
|---|---|
| A chunk is claimed under `_lock` before a worker may touch it | otherwise two workers write the same offsets and the server sees the same `Range` twice |
| `done` counts **bytes written**, never `os.path.getsize()` | the part file is pre-truncated, so its size is `total` from the first second |
| The seam sample (64 bytes past the chunk end) is **never written** | that range belongs to the neighbour chunk; writing it would corrupt a concurrent write |
| Completion is `done == total`, then `os.replace` | a cancelled download must leave no plausible-looking target file |
| `_stop` ≠ cancelled; `_user_cancelled` is separate | internal failures also set `_stop`; treating that as "cancelled" silently swallowed errors |
| A pooled connection is only reusable when the response body was read **completely** | otherwise the next request on that socket reads leftover bytes |
| Resume state is validated against URL, total, `ETag`, `Last-Modified` | the file may have changed on the server since the last attempt |

## Concurrency model

```
main thread ── run() ─────────────────────────────────────────────┐
   │  spawns: monitor thread (500ms speed sampling)               │
   │          schedule thread (5s, re-applies time-of-day limit)  │
   │          N worker threads ── claim → fetch → release           │
   └─ joins workers, then verifies seams, then renames               ┘
```

- One `_lock` guards chunk bookkeeping (`done`, `claimed`, counters); one `_io_lock` serialises
  positioned writes to the part file (Windows has no `os.pwrite`, so it is `seek` + `write`).
- `_claim()` skips chunks whose `retry_at` is in the future, so a failed chunk cannot be grabbed
  again immediately by the same unlucky connection.
- A worker exits when there is nothing left to claim **and** nothing is being waited on.

## Connection reuse (`pool.py`)

`Pool` keeps `{(scheme, host, port): [connection, ...]}`. A stream is handed back only when:

- no exception occurred,
- the response body was fully consumed (`done`/`isclosed()`/`length == 0`),
- the server did not send `Connection: close`.

Consequence: **keep-alive is tied to closed-range requests**. With `--open-range` (`bytes=N-`) the
body extends to EOF, the client stops early, and the connection must be dropped — that is one of the
reasons closed ranges are the default.

If a pooled connection was closed by the peer while idle (`RemoteDisconnected`, `BrokenPipeError`,
`BadStatusLine`), the pool opens a fresh connection and retries the request once instead of failing
the chunk.

## Seam verification

For every chunk except the last, the request asks for `+64` extra bytes (default closed range:
`bytes=N-(M+63)`). Those bytes are kept in memory only. When the neighbouring chunk has already
written its head, the sample is compared with what is on disk; if not, the comparison is deferred to
the end of the download. A mismatch sets `mismatch_detected` and triggers a single-stream re-download
of the whole file.

This detects "the same range was served differently to two connections" — the failure mode that
otherwise produces a file with a quiet hole at a chunk boundary.

## State files

| File | Content |
|---|---|
| `<dest>.idmpart` | the data being downloaded (pre-truncated to `total`) |
| `<dest>.idmpart.json` | url, dest, total, etag, last_modified, per-chunk `{i,s,e,d}` |
| `%LOCALAPPDATA%\ai-idm\queue.db` | SQLite queue (`AI_IDM_HOME` overrides) |
| `%LOCALAPPDATA%\ai-idm\config.json` | user config |

## Configuration precedence

`config.resolve(url, cfg, ...)`:

- `connections`: CLI explicit → per-site override (`site_connections`, longest suffix wins) → global default
- `limit_bps`: CLI explicit → time-of-day schedule rule (first match) → global default
- `proxy`, `timeout`, `max_retries`: CLI explicit → global default

Boolean toggles (`seam_verify`, `open_range`, `dynamic_chunks`, `keepalive`) live in the config file and
are coerced from strings, so `ai-idm config set keepalive=false` does what you expect.

## MCP server

`mcp_server.py` implements the MCP stdio transport by hand: newline-delimited JSON-RPC, `initialize` /
`tools/list` / `tools/call`. Downloads are held in an in-process registry so `start_download` can
return immediately and `wait_download` can block on the same object.

## Browser takeover service

```
extension  ──POST /add──▶  server.Handler ──▶ Queue (SQLite) ──▶ Runner ──▶ engine.Download
           ◀─GET /status──                (claim_next)      (parallel N)   dest_dir=download_dir
```

`server.py` is a `ThreadingHTTPServer` bound to loopback. Every request that can *cause* something
(`/add`, `/cancel`, `/remove`, `/reveal`) or *reveal* something (`/status`, `/events`, `/config`)
requires the
`X-AI-IDM-Token` header (or `?token=`), compared with `hmac.compare_digest`. `/ping` is open so the
extension can tell "service down" (leave the browser download alone) from "service up" (take over).

Three design consequences worth noting:

- **Destination resolution.** The extension only knows the URL, so `Download` gained `dest_dir`:
  when no explicit path is given, the engine probes first and writes the `Content-Disposition`
  filename *into* that directory. Nothing has to pre-guess the name.
- **Cancellation.** `Download.run()` returns a snapshot with `status="cancelled"` rather than
  raising, so `Runner` keeps a `tid → Download` registry and translates that into a queue row
  update; without it a cancelled task used to be recorded as `done`.
- **Opening the file manager.** The extension cannot open an arbitrary folder itself (its only API
  is `downloads.showDefaultFolder()`, which targets the browser's own directory), so the completion
  path calls `POST /reveal`. That endpoint shells out (`open -R` on macOS, `xdg-open` elsewhere)
  only for paths whose `realpath` stays inside `download_dir` — a leaked token must not turn the
  service into an arbitrary-path opener. On Windows the file path goes through
  `SHOpenFolderAndSelectItems` first: the shell reuses an existing window (or tab) already showing
  that folder and selects the item instead of stacking a new window per download; if the API fails
  the service falls back to spawning `explorer /select,<path>`. The child process is spawned and
  never awaited: `explorer.exe` returns non-zero even when it succeeds. Reuse leaves no "new
  handle" to identify, so a short-lived thread then locates the window by title (folder name, or
  the drive letter for a root directory) and raises it — `SetForegroundWindow`, then the
  `AttachThreadInput` variant, then `SwitchToThisWindow`, falling back to a z-order bump plus a
  taskbar flash — because a background service is denied foreground rights and the window would
  otherwise open behind everything. This step only succeeds when the service runs **non-elevated**:
  an elevated process cannot attach to / steal focus from the medium-integrity desktop (UIPI), so
  it degrades to the taskbar flash.

HLS jobs from `/add` stay in memory (`hls-…` ids) because the HLS downloader has no resumable
state file to reconcile with the queue on restart.
