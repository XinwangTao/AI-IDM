# Behavior notes: what is evidence-based, what is my own design

This project exists because of a question: *why* does a mature commercial download manager behave the
way it does? Rather than guess, I installed a copy, pointed it at a local instrumented HTTP server,
and read the request pattern and the program's own log output. Then I rebuilt the engine in Python.

Two rules I kept:

1. **No code was copied.** Different language, different runtime, and copying a commercial product is
   neither legal nor useful. Only the *behavior model* was reused.
2. **Claims are labeled.** Everything below says whether it comes from an observation or from my own
   engineering judgement. Nothing is presented as "this is how product X implements it" unless it was
   actually observed.

## Observed at runtime

Method: a local HTTP server logged every request (time, client port, `Range`, `User-Agent`), the
program downloaded a synthetic file with `/d /p /f`, and its per-task log file was read afterwards.

| Observed behavior | How it shows up | Where it is in this project |
|---|---|---|
| Connections start staggered, not all at once | `C0` requests with **no** `Range`; `C1` starts 8 ms later at the current write position; more connections appear ~3.5 s in | dynamic chunking: chunks are pulled from a queue, not pre-assigned (`engine.plan_segments`) |
| Chunk boundaries appear as the download progresses | request starts track the write frontier rather than `total/N` | same |
| Adjacent chunks overlap by **64 bytes** | per-chunk log line `posInCmpBuff3=64`, `0` for the last chunk; the server also saw `bytes=X-(X+63)` requests | `engine.SEAM = 64`, `_seam_len`, `_check_seam` |
| A chunk can be moved to another connection | log: `The file chunk of connection 3 is reassigned to connection 1`, with `downloaded=0` | `engine._reassign` → `handoffs` counter when `done == 0` |
| Connections are reused for consecutive chunks | log: `next 3`, `Reusing connection.` | `pool.Pool` (keep-alive) |
| Parts are written to a temp area and renamed at the end | log: `Cannot rename downloaded file from temp folder`, `Rebuilding complete, real size=…` | `<dest>.idmpart` + `os.replace` |
| Default connection count is 8 | reads a configured value, falls back to 8 | `engine.DEFAULT_CONNECTIONS = 8` |
| Per-site connection override | separate config root keyed by host | `config.set_site` / `site_connections` |
| Speed limit is a 64-bit byte/second value | 8-byte registry value + on/off flag | `limit_bps` as a Python int |

## My own engineering decisions

These have **no** observation behind them; they are choices that fit Python and this codebase.

| Decision | Why |
|---|---|
| `.idmpart.json` as the resume format | simple, inspectable, and the product's own part metadata format was not something I needed to copy |
| `ETag` / `Last-Modified` / `If-Range` validation on resume | standard HTTP practice |
| Fall back to a single stream on `200` (rather than on a checksum mismatch) | cheaper and earlier signal; the invariant is the same |
| SQLite queue with `UPDATE … RETURNING` for atomic claim | the product uses its own scheduler storage; a database is the right tool here |
| Token bucket for throttling | the product has a limiter, its algorithm was not examined |
| 64 KB read buffer, 1 MB minimum chunk, 4 retries, 0.5→8 s backoff | empirical values, tuned on local and public servers |
| Chunk count = connections × 4, capped at 256 | measured trade-off (below) |
| SOCKS5 client, and `handler_order = 100` for the urllib handler | pure engineering: urllib's built-in handler (order 500) wins otherwise, and the proxy is silently bypassed |
| Sampling verification (`--verify-sample`) | complements seams: seams only cover chunk boundaries |

## Measurements

Local, deterministic (a server-side delay emulates per-connection setup cost):

| Experiment | Result |
|---|---|
| 32 MB, 8 connections, one connection throttled to ~400 KB/s | static partitioning 11.7 s → dynamic chunking **4.2 s** |
| 24 chunks, 8 connections, 0.3 s emulated handshake per new connection | no reuse: 25 TCP connections / 2.96 s → keep-alive: **9** / **1.73 s** |

Public network (14 MB from `cdn.kernel.org`, 8 connections): 14 chunks were fetched over **8** TCP
connections with 6 reuses, and the SHA-256 matched the single-stream download. Wall-clock numbers on
that host were dominated by an unstable local proxy, so they are deliberately not quoted as results.

## Honest gaps

- The **exact seam comparison technique** (a byte compare, a hash, or something else) was not
  determined. Only "64 bytes are compared, 0 for the last chunk" is observed. A byte compare is used.
- Connection **ramp-up timing** (2 connections, then 4 after ~3.5 s) is not reproduced; this project
  opens `connections` workers immediately.
- The product's own part-metadata format, scheduler storage and browser integration are untouched.
- No license-check, DRM or paywall logic was examined, and none is implemented.
