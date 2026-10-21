#!/usr/bin/env python3
"""镜像站专用路径：一次 GET 完成探测+下载（不 HEAD、不发 Range）。

镜像（xget / ghproxy）按请求数限流，传统 "HEAD + GET" 的第二发必被 429。
这里用一个本地服务器冒充镜像，断言全流程只打了一个 GET 请求。
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

PAYLOAD = bytes((i * 37 + 11) & 0xFF for i in range(3 * 1024 * 1024))
SEEN = []          # [(method, range_header)]


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        SEEN.append(("HEAD", None))
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        SEEN.append(("GET", self.headers.get("Range")))
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", 'attachment; filename="mirror.bin"')
        self.end_headers()
        self.wfile.write(PAYLOAD)


def main():
    results = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/gh/o/r/releases/download/v1/mirror.bin"

    # 把本机地址冒充成镜像域名：只影响 host 判定，不碰真实网络
    orig = E.is_mirror_host
    E.is_mirror_host = lambda u: "127.0.0.1" in u
    tmp = tempfile.mkdtemp(prefix="ai-idm-mirror-")
    try:
        r = E.Download(url, dest_dir=tmp, connections=8).run()
        path = os.path.join(tmp, "mirror.bin")
        got = open(path, "rb").read() if os.path.exists(path) else b""

        results.append(("mirror-single-request", len(SEEN) == 1,
                        {"requests": len(SEEN), "seen": SEEN}))
        results.append(("mirror-no-head", all(m != "HEAD" for m, _ in SEEN),
                        {"methods": [m for m, _ in SEEN]}))
        results.append(("mirror-no-range", all(rg is None for _, rg in SEEN),
                        {"ranges": [rg for _, rg in SEEN]}))
        results.append(("mirror-complete", r["status"] == "done" and got == PAYLOAD,
                        {"status": r["status"], "bytes": len(got)}))
        results.append(("mirror-connections-1", r["connections"] == 0 and r["segments"] == 1,
                        {"segments": r["segments"]}))
    finally:
        E.is_mirror_host = orig
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== 结果 ===")
    ok_all = True
    for name, ok, info in results:
        ok_all = ok_all and ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:22} {info}")
    print("\nALL PASS" if ok_all else "\nSOME FAILED")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
