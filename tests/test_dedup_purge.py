#!/usr/bin/env python3
"""去重 / 队列清理 / ±1 字节容错。

- Queue.find_active: 相同 url+dest+headers+proxy 的活跃任务命中，其余返回 None；
- Queue.purge: 只删指定状态，pending 保留；
- 服务器 Content-Length 比实际 body 多 1 字节时，Download 仍应成功（单连接整取路径）。
"""

import http.server
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_idm import engine as E                      # noqa: E402
from ai_idm.queue import Queue                      # noqa: E402

PAYLOAD = bytes((i * 31 + 7) & 0xFF for i in range(4096))


class Liar(http.server.BaseHTTPRequestHandler):
    """Content-Length 报 len+1，实际只发 len 字节；不支持 Range。"""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _head(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD) + 1))
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Connection", "close")   # 发完即断，客户端读到 EOF 得到真实长度
        self.end_headers()

    def do_HEAD(self):
        self._head()

    def do_GET(self):
        self._head()
        try:
            self.wfile.write(PAYLOAD)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    results = []
    tmp = tempfile.mkdtemp(prefix="ai-idm-dp-")

    # ---- A. 队列去重 / 清理 ----
    q = Queue(os.path.join(tmp, "q.db"))
    dest = os.path.join(tmp, "a.zip")
    t1 = q.add("http://x/a.zip", dest=dest, proxy=None, headers={"Referer": "r"})
    dup = q.find_active("http://x/a.zip", dest=dest, proxy=None, headers={"Referer": "r"})
    results.append(("dedupe-hit", bool(dup) and dup["id"] == t1,
                    {"got": dup and dup["id"], "want": t1}))
    results.append(("dedupe-diff-headers-miss",
                    q.find_active("http://x/a.zip", dest=dest, proxy=None,
                                  headers={"Referer": "z"}) is None, {}))
    results.append(("dedupe-diff-url-miss", q.find_active("http://x/b.zip") is None, {}))
    q.update(t1, status="done")
    results.append(("dedupe-ignore-finished",
                    q.find_active("http://x/a.zip", dest=dest,
                                  headers={"Referer": "r"}) is None, {}))

    t2 = q.add("http://x/e.zip")
    q.update(t2, status="error")
    q.add("http://x/p.zip")           # pending，必须保留
    removed = q.purge(("done", "error", "cancelled"))
    statuses = sorted(r["status"] for r in q.list(limit=100))
    results.append(("purge-removes-finished",
                    removed == 2 and statuses == ["pending"],
                    {"removed": removed, "left": statuses}))

    # ---- B. ±1 字节容错 ----
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Liar)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/f.bin"
    try:
        out = os.path.join(tmp, "f.bin")
        r = E.Download(url, dest=out, connections=4).run()
        data = open(out, "rb").read() if os.path.exists(out) else b""
        results.append(("tolerance-done", r.get("status") == "done",
                        {"status": r.get("status")}))
        results.append(("tolerance-bytes", data == PAYLOAD,
                        {"got": len(data), "want": len(PAYLOAD)}))
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== 结果 ===")
    ok_all = True
    for name, ok, info in results:
        ok_all = ok_all and ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:26} {info}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
