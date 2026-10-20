# Architecture

```
idmclone/
├── engine.py      HTTP(S) download engine: probe, chunk planning, workers, seams, verification
├── pool.py        keep-alive connection pool (http.client) — direct / HTTP proxy / SOCKS5
├── proxy.py       proxy parsing, SOCKS5 handshake, urllib opener wiring
├── ftp.py         FTP/FTPS download with multi-connection REST
├── hls.py         m3u8 parsing, AES-128-CBC, segment download + merge
├── queue.py       SQLite job queue and parallel runner
├── config.py      user config: defaults, per-site concurrency, time-of-day limits
├── cli.py         argparse front end, JSON output
└── mcp_server.py  MCP stdio server exposing 12 tools
```

Everything is standard library only. `engine.py` is the core; the rest is either a protocol
front end (`ftp`, `hls`), a scheduling layer (`queue`), or an entry point (`cli`, `mcp_server`).

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
| `%LOCALAPPDATA%\idmclone\queue.db` | SQLite queue (`IDMCLONE_HOME` overrides) |
| `%LOCALAPPDATA%\idmclone\config.json` | user config |

## Configuration precedence

`config.resolve(url, cfg, ...)`:

- `connections`: CLI explicit → per-site override (`site_connections`, longest suffix wins) → global default
- `limit_bps`: CLI explicit → time-of-day schedule rule (first match) → global default
- `proxy`, `timeout`, `max_retries`: CLI explicit → global default

Boolean toggles (`seam_verify`, `open_range`, `dynamic_chunks`, `keepalive`) live in the config file and
are coerced from strings, so `idmclone config set keepalive=false` does what you expect.

## MCP server

`mcp_server.py` implements the MCP stdio transport by hand: newline-delimited JSON-RPC, `initialize` /
`tools/list` / `tools/call`. Downloads are held in an in-process registry so `start_download` can
return immediately and `wait_download` can block on the same object.
