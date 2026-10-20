"""动态分块：块由工作队列在运行时分配，而不是开跑前按连接数均分。

场景：8 条连接里有一条被服务端限速。静态分段时这条连接握着 1/8 的文件，
整单被它拖住；动态分块时它只握着 1/32，其余连接会把剩下的块吃掉。

对照 IDM 动态分析：分块是运行时按实时进度生成/分配的
（C0 从 0 起、C1 从当前写位置起、3.5s 后才加到 4 条连接）。
"""

import hashlib
import http.server
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    from ai_idm.stdio import ensure_utf8_stdio
    ensure_utf8_stdio()          # 输出含中文，非 UTF-8 控制台会崩
except Exception:
    pass


from ai_idm.engine import Download  # noqa: E402

TOTAL = 32 * 1024 * 1024
PAYLOAD = bytes((i * 41 + 5) & 0xFF for i in range(TOTAL))
SLOW_START = 8 * 1024 * 1024      # 静态：第 3 段起点；动态：第 9 块起点
DELAY = 0.16                      # 每 64KB 停 0.16s ≈ 400KB/s


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(TOTAL))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def _write(self, data):
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        rng = self.headers.get("Range")
        s, e = 0, TOTAL - 1
        if rng:
            a, _, b = rng.split("=", 1)[1].partition("-")
            s = int(a) if a else 0
            e = min(int(b) if b else TOTAL - 1, TOTAL - 1)
        data = PAYLOAD[s:e + 1]

        if rng:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {s}-{e}/{TOTAL}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(TOTAL))
            self.end_headers()

        if s == SLOW_START:
            # 只掐"从 SLOW_START 开始"的那一次请求（接缝多读的请求起止不同，不受影响）
            for i in range(0, len(data), 64 * 1024):
                self._write(data[i:i + 64 * 1024])
                time.sleep(DELAY)
        else:
            self._write(data)


class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/big.bin"
    work = tempfile.mkdtemp(prefix="dyn-")
    expect = hashlib.sha256(PAYLOAD).hexdigest()
    results = []

    # ---- 1. 静态分段（旧行为）：慢连接握住 4MB ----
    d1 = os.path.join(work, "static.bin")
    t0 = time.time()
    r1 = Download(url, dest=d1, connections=8, dynamic_chunks=False).run()
    t_static = time.time() - t0
    ok1 = r1["status"] == "done" and sha(d1) == expect and r1["segments"] == 8
    results.append(("static-8segs", ok1,
                    {"segments": r1["segments"], "sec": round(t_static, 1)}))

    # ---- 2. 动态分块：32 块，慢连接只握 1MB，其余被别的连接吃掉 ----
    d2 = os.path.join(work, "dynamic.bin")
    t0 = time.time()
    r2 = Download(url, dest=d2, connections=8).run()
    t_dyn = time.time() - t0
    ok2 = (r2["status"] == "done" and sha(d2) == expect
           and r2["segments"] == 32 and r2.get("seams_checked", 0) >= 30)
    results.append(("dynamic-32chunks", ok2,
                    {"segments": r2["segments"], "sec": round(t_dyn, 1),
                     "seams": r2.get("seams_checked")}))

    # ---- 3. 动态明显快于静态（慢连接不再拖住整单）----
    ok3 = ok1 and ok2 and t_dyn < t_static * 0.7
    results.append(("tail-latency", ok3,
                    {"static_sec": round(t_static, 1), "dynamic_sec": round(t_dyn, 1),
                     "ratio": round(t_dyn / t_static, 2) if t_static else None}))

    print("\n=== 结果 ===")
    allok = True
    for name, ok, detail in results:
        allok &= bool(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name:18} {detail}")
    print("\nALL PASS" if allok else "\nSOME FAILED")
    srv.shutdown()
    shutil.rmtree(work, ignore_errors=True)
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
