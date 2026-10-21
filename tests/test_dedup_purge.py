#!/usr/bin/env python3
"""去重 / 队列清理 / ±1 字节容错（单连接 + 镜像整取）。

- find_active / find_or_add：相同 url+dest+headers+proxy 的活跃任务命中；find_or_add 原子化；
- purge：只删指定状态，pending 保留；
- 服务器 Content-Length 比实际 body 多 1 字节时，单连接整取与镜像整取都应成功。
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
    results.append(("find-active-hit", bool(dup) and dup["id"] == t1,
                    {"got": dup and dup["id"], "want": t1}))
    results.append(("find-active-diff-headers-miss",
                    q.find_active("http://x/a.zip", dest=dest, proxy=None,
                                  headers={"Referer": "z"}) is None, {}))
    row, deduped = q.find_or_add("http://x/a.zip", dest=dest, headers={"Referer": "r"})
    results.append(("find-or-add-dedupe", deduped and row["id"] == t1,
                    {"deduped": deduped, "id": row["id"]}))
    row2, deduped2 = q.find_or_add("http://x/new.zip", dest=os.path.join(tmp, "n.zip"))
    results.append(("find-or-add-new", (not deduped2) and row2["id"] != t1,
                    {"deduped": deduped2}))
    q.update(t1, status="done")
    results.append(("dedupe-ignore-finished",
                    q.find_active("http://x/a.zip", dest=dest,
                                  headers={"Referer": "r"}) is None, {}))

    t2 = q.add("http://x/e.zip")
    q.update(t2, status="error")
    q.add("http://x/p.zip")           # pending，必须保留
    removed = q.purge(("done", "error", "cancelled"))
    statuses = [r["status"] for r in q.list(limit=100)]
    results.append(("purge-removes-finished",
                    removed == 2 and set(statuses) == {"pending"},
                    {"removed": removed, "left": statuses}))

    # ---- B/C. ±1 字节容错：单连接 + 镜像整取 ----
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Liar)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    orig = E.is_mirror_host
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        out = os.path.join(tmp, "f.bin")
        r = E.Download(base + "/f.bin", dest=out, connections=4).run()
        data = open(out, "rb").read() if os.path.exists(out) else b""
        results.append(("stream-tolerance-done", r.get("status") == "done",
                        {"status": r.get("status")}))
        results.append(("stream-tolerance-bytes", data == PAYLOAD,
                        {"got": len(data), "want": len(PAYLOAD)}))

        E.is_mirror_host = lambda u: True
        out2 = os.path.join(tmp, "m.bin")
        r2 = E.Download(base + "/m.bin", dest=out2).run()
        data2 = open(out2, "rb").read() if os.path.exists(out2) else b""
        results.append(("mirror-tolerance-done", r2.get("status") == "done",
                        {"status": r2.get("status")}))
        results.append(("mirror-tolerance-bytes", data2 == PAYLOAD,
                        {"got": len(data2), "want": len(PAYLOAD)}))
    finally:
        E.is_mirror_host = orig
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== 结果 ===")
    ok_all = True
    for name, ok, info in results:
        ok_all = ok_all and ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:28} {info}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
