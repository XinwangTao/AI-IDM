---
name: Bug report
about: Something downloads wrong, stalls, or crashes
title: ""
labels: bug
---

**What happened**
A clear description of the failure. If the download produced a corrupt file, say where it differs
(e.g. `sha256sum` mismatch, or a specific offset range).

**Command / MCP call**
```
idmclone get <url> -o <path> -n 8 ...
```

**Output**
Paste the JSON error output (`--quiet` off is fine). If it crashed, the full traceback.

**Environment**
- idmclone version (`idmclone --version`):
- OS / Python:
- Server: does it support `Range`? (`idmclone probe <url>` shows `accept_ranges`)
- Proxy in use? (`--proxy`, or a system proxy)

**Reproduction**
The fastest path to a fix is a local reproduction: a tiny HTTP server that responds the same way as
the real one (status codes, `Content-Range`, `Content-Length`, `Connection` header). `tests/` has
several to copy from.
