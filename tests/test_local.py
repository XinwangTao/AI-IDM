"""本地验证：分片下载 / 断点续传 / 服务端忽略 Range 的降级 / MCP 协议。"""

import hashlib
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from idmclone.engine import Download, probe  # noqa: E402

PAYLOAD = bytes((i * 37 + 11) & 0xFF for i in range(8 * 1024 * 1024))  # 8 MB
RANGES = []
SUPPORT_RANGE = {"on": True}
WORK = os.path.join(ROOT, ".testtmp")
os.makedirs(WORK, exist_ok=True)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _size(self):
        return len(PAYLOAD)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(self._size()))
        self.send_header("Accept-Ranges", "bytes" if SUPPORT_RANGE["on"] else "none")
        self.send_header("ETag", '"v1"')
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()

    def do_GET(self):
        rng = self.headers.get("Range")
        RANGES.append(rng)
        if rng and SUPPORT_RANGE["on"]:
            spec = rng.split("=", 1)[1]
            a, _, b = spec.partition("-")
            start = int(a) if a else 0
            end = int(b) if b else self._size() - 1
            end = min(end, self._size() - 1)
            data = PAYLOAD[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{self._size()}")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", '"v1"')
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(self._size()))
            self.send_header("ETag", '"v1"')
            self.end_headers()
            self.wfile.write(PAYLOAD)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    def handle_error(self, request, client_address):
        pass                      # 客户端读够即关会产生 BrokenPipe，属正常
    allow_reuse_address = True


def start_server():
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/big.bin"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    srv, url = start_server()
    expect = hashlib.sha256(PAYLOAD).hexdigest()
    results = []

    # --- 1. 探测 ---
    p = probe(url)
    results.append(("probe", p.total == len(PAYLOAD) and p.ranges, p.as_dict()))

    # --- 2. 8 连接分片下载 ---
    dest = os.path.join(WORK, "a.bin")
    for f in (dest, dest + ".idmpart.json"):
        if os.path.exists(f):
            os.remove(f)
    RANGES.clear()
    r = Download(url, dest=dest, connections=8).run()
    distinct = len({x for x in RANGES if x})
    results.append(("multipart", r["status"] == "done" and sha256(dest) == expect and distinct >= 2,
                    {"ranges_seen": distinct, "size": os.path.getsize(dest), "sec": r.get("elapsed_sec")}))

    # --- 3. 限速 + 取消 + 续传 ---
    dest2 = os.path.join(WORK, "b.bin")
    for f in (dest2, dest2 + ".idmpart.json"):
        if os.path.exists(f):
            os.remove(f)
    dl = Download(url, dest=dest2, connections=4, limit_bps=1_500_000)
    t = threading.Thread(target=dl.run, daemon=True)
    t.start()
    time.sleep(1.6)
    dl.cancel()
    t.join(timeout=10)
    partial = dl.snapshot()
    has_state = os.path.exists(dest2 + ".idmpart.json")
    r2 = Download(url, dest=dest2, connections=4).run()
    results.append(("resume", has_state and partial["downloaded"] > 0
                    and r2["status"] == "done" and sha256(dest2) == expect,
                    {"partial_bytes": partial["downloaded"], "final": r2["status"]}))

    # --- 4. 服务端忽略 Range → 单连接降级 ---
    SUPPORT_RANGE["on"] = False
    dest3 = os.path.join(WORK, "c.bin")
    for f in (dest3, dest3 + ".idmpart.json"):
        if os.path.exists(f):
            os.remove(f)
    RANGES.clear()
    r3 = Download(url, dest=dest3, connections=8).run()
    results.append(("fallback-single", r3["status"] == "done" and sha256(dest3) == expect,
                    {"size": os.path.getsize(dest3), "ranges": len(RANGES)}))
    SUPPORT_RANGE["on"] = True

    # --- 5. MCP stdio 协议 ---
    proc = subprocess.Popen(
        [sys.executable, "-m", "idmclone", "mcp"],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
    )

    def rpc(obj, wait=True):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()
        if wait:
            return json.loads(proc.stdout.readline())

    init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}}})
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    proc.stdin.flush()
    tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    call = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "probe_url", "arguments": {"url": url}}})
    text = call["result"]["content"][0]["text"]
    info = json.loads(text)
    results.append(("mcp-stdio",
                    init["result"]["serverInfo"]["name"] == "idmclone"
                    and len(tools["result"]["tools"]) >= 6
                    and info["total"] == len(PAYLOAD),
                    {"tools": len(tools["result"]["tools"]), "probe_total": info["total"]}))
    proc.stdin.close()
    proc.wait(timeout=5)

    print("\n=== 结果 ===")
    ok_all = True
    for name, ok, detail in results:
        ok_all &= bool(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name:16} {detail}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    srv.shutdown()
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
