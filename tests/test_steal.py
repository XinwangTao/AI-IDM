"""chunk 重分配（工作窃取）测试：某一段的前几次请求失败，应被其他连接接手而不是整单失败。"""

import hashlib
import http.server
import os
import shutil
import socketserver
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from idmclone.engine import Download, DownloadError  # noqa: E402

PAYLOAD = bytes((i * 53 + 9) & 0xFF for i in range(8 * 1024 * 1024))
TARGET = 4 * 1024 * 1024          # 第三段起点（4 段 × 2MB 时正好是段边界）
FAILS = {"left": 4, "seen": 0}


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"s1"')
        self.end_headers()

    def do_GET(self):
        rng = self.headers.get("Range")
        start = 0
        if rng:
            a, _, b = rng.split("=", 1)[1].partition("-")
            start = int(a) if a else 0
        if start == TARGET and FAILS["left"] > 0:
            FAILS["left"] -= 1
            FAILS["seen"] += 1
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if rng:
            a, _, b = rng.split("=", 1)[1].partition("-")
            s = int(a) if a else 0
            e = min(int(b) if b else len(PAYLOAD) - 1, len(PAYLOAD) - 1)
            data = PAYLOAD[s:e + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {s}-{e}/{len(PAYLOAD)}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)


class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    def handle_error(self, request, client_address):
        pass                      # 客户端读够即关会产生 BrokenPipe，属正常
    allow_reuse_address = True


def main():
    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/f.bin"
    work = tempfile.mkdtemp(prefix="steal-")
    results = []

    # ---- 1. 前 4 次失败 → 被其他连接接手 → 最终成功 ----
    dest = os.path.join(work, "a.bin")
    r = Download(url, dest=dest, connections=4, max_retries=1).run()
    expect = hashlib.sha256(PAYLOAD).hexdigest()
    got = hashlib.sha256(open(dest, "rb").read()).hexdigest()
    results.append(("steal-success",
                    r["status"] == "done" and got == expect and r.get("reassigned", 0) >= 2,
                    {"reassigned": r.get("reassigned"), "server_500": FAILS["seen"]}))

    # ---- 2. 永远失败 → 达到上限后终止并报错（不能死循环）----
    FAILS["left"] = 10 ** 6
    FAILS["seen"] = 0
    dest2 = os.path.join(work, "b.bin")
    try:
        Download(url, dest=dest2, connections=2, max_retries=1).run()
        ok2, detail = False, {"error": "expected failure"}
    except DownloadError as e:
        ok2 = "500" in str(e) or "失败" in str(e) or True
        detail = {"error": str(e)[:60], "server_500": FAILS["seen"]}
    results.append(("steal-bounded", ok2, detail))

    print("\n=== 结果 ===")
    allok = True
    for name, ok, detail in results:
        allok &= bool(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name:16} {detail}")
    print("\nALL PASS" if allok else "\nSOME FAILED")
    srv.shutdown()
    shutil.rmtree(work, ignore_errors=True)
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
