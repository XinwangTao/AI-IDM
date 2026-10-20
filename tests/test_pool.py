"""连接复用（keep-alive 连接池）测试。

对齐 IDM 日志里的 `next %d` + `Reusing connection.`：同一条 TCP 连续取块。
断言不看"下载成功"，而是看服务端**实际建立了几条 TCP 连接**——这才是复用的证据。
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
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    from ai_idm.stdio import ensure_utf8_stdio
    ensure_utf8_stdio()          # 输出含中文，非 UTF-8 控制台会崩
except Exception:
    pass


from ai_idm.engine import Download  # noqa: E402

TOTAL = 6 * 1024 * 1024
PAYLOAD = bytes((i * 67 + 19) & 0xFF for i in range(TOTAL))
STATE = {"conns": 0, "requests": 0, "force_close": False,
         "redirect": False, "handshake": 0.0}
LOCK = threading.Lock()


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        with LOCK:
            STATE["conns"] += 1
        if STATE["handshake"]:
            time.sleep(STATE["handshake"])      # 模拟每次建链（TCP+TLS）的成本

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
        with LOCK:
            STATE["requests"] += 1
        if STATE["redirect"] and self.path.startswith("/r/"):
            self.send_response(302)
            self.send_header("Location", "/f.bin")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

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
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        if STATE["force_close"]:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
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


def reset(**kw):
    STATE.update({"conns": 0, "requests": 0, "force_close": False,
                  "redirect": False, "handshake": 0.0})
    STATE.update(kw)


def main():
    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    url = f"{base}/f.bin"
    work = tempfile.mkdtemp(prefix="pool-")
    expect = hashlib.sha256(PAYLOAD).hexdigest()
    results = []

    # ---- 1. 默认（复用）：6 块 / 2 连接 → 服务端只应看到 2 条数据连接 ----
    reset()
    d1 = os.path.join(work, "keep.bin")
    r1 = Download(url, dest=d1, connections=2, keepalive=True).run()
    conns_keep = STATE["conns"]
    pool = r1.get("pool") or {}
    ok1 = (r1["status"] == "done" and sha(d1) == expect
           and r1["segments"] == 6
           and pool.get("reused", 0) >= 3        # 6 块 / 2 连接 → 至少复用 3 次
           and conns_keep <= 3)                  # 1 条探测 + 2 条数据连接
    results.append(("keepalive-reuse", ok1,
                    {"segments": r1["segments"], "tcp_conns": conns_keep,
                     "pool": pool}))

    # ---- 2. 关闭复用：每块一条连接 ----
    reset()
    d2 = os.path.join(work, "plain.bin")
    r2 = Download(url, dest=d2, connections=2, keepalive=False).run()
    ok2 = (r2["status"] == "done" and sha(d2) == expect
           and STATE["conns"] >= 6 and r2.get("pool") is None)
    results.append(("no-keepalive", ok2,
                    {"tcp_conns": STATE["conns"], "pool": r2.get("pool")}))

    # ---- 3. 服务端要求关闭（Connection: close）→ 池子不能复用 ----
    reset(force_close=True)
    d3 = os.path.join(work, "close.bin")
    r3 = Download(url, dest=d3, connections=2, keepalive=True).run()
    ok3 = r3["status"] == "done" and sha(d3) == expect and STATE["conns"] >= 6
    results.append(("honor-connection-close", ok3,
                    {"tcp_conns": STATE["conns"], "pool": r3.get("pool")}))

    # ---- 4. 重定向：池子自己跟随后仍能正确取数 ----
    reset(redirect=True)
    d4 = os.path.join(work, "redir.bin")
    r4 = Download(f"{base}/r/f.bin", dest=d4, connections=2, keepalive=True).run()
    ok4 = r4["status"] == "done" and sha(d4) == expect
    results.append(("redirect", ok4,
                    {"tcp_conns": STATE["conns"], "requests": STATE["requests"]}))

    # ---- 5. 建链成本：24 块 / 8 连接，每条新连接 0.12s ----
    reset(handshake=0.3)
    d5 = os.path.join(work, "ka.bin")
    t0 = time.time()
    r5 = Download(url, dest=d5, connections=8, min_seg=256 * 1024,
                  keepalive=True).run()
    t_keep = time.time() - t0
    conns_keep = STATE["conns"]
    reset(handshake=0.3)
    d6 = os.path.join(work, "nka.bin")
    t0 = time.time()
    r6 = Download(url, dest=d6, connections=8, min_seg=256 * 1024,
                  keepalive=False).run()
    t_plain = time.time() - t0
    # 结构性断言才是可靠信号：块数固定 24，keep-alive 只该建 ~8+1 条连接；
    # 墙钟时间在共享 CI 机器上是噪声（实测出现过 keepalive 反而更慢），只留一个宽松上限
    # 兜住"病态回归"，不拿它当通过判据。
    ok5 = (r5["status"] == "done" and r6["status"] == "done"
           and sha(d5) == expect and sha(d6) == expect
           and r5["segments"] == 24
           and conns_keep <= 10                     # 8 连接 + 1 探测，24 块不该建 24 条
           and STATE["conns"] >= 20                 # 对照组确实每块建一条
           and t_keep < t_plain * 1.5)              # 宽松兜底：明显变慢才算回归
    results.append(("handshake-savings", ok5,
                    {"segments": r5["segments"],
                     "conns_keep": conns_keep, "conns_plain": STATE["conns"],
                     "keepalive_sec": round(t_keep, 2),
                     "plain_sec": round(t_plain, 2),
                     "ratio": round(t_keep / t_plain, 2) if t_plain else None}))

    print("\n=== 结果 ===")
    allok = True
    for name, ok, detail in results:
        allok &= bool(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name:24} {detail}")
    print("\nALL PASS" if allok else "\nSOME FAILED")
    srv.shutdown()
    shutil.rmtree(work, ignore_errors=True)
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
