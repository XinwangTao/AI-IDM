"""分时段限速：规则判定 + 运行中动态切换。"""

import http.server
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    from idmclone.stdio import ensure_utf8_stdio
    ensure_utf8_stdio()          # 输出含中文，非 UTF-8 控制台会崩
except Exception:
    pass


from idmclone import config as C  # noqa: E402
from idmclone.engine import Download  # noqa: E402

PAYLOAD = bytes((i * 29 + 5) & 0xFF for i in range(8 * 1024 * 1024))


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        rng = self.headers.get("Range")
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


ALL_DAY = [{"start": "00:00", "end": "23:59", "limit_bps": 1_000_000}]


def main():
    results = []

    # ---- 1. 规则判定 ----
    sch = [{"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00", "limit_bps": 1 << 20},
           {"start": "23:00", "end": "07:00", "limit_bps": 1 << 18}]
    cases = [
        ("工作日白天", datetime(2026, 10, 19, 12, 0), 1 << 20),
        ("工作日边界(18:00)", datetime(2026, 10, 19, 18, 0), None),
        ("跨天区间凌晨", datetime(2026, 10, 19, 3, 0), 1 << 18),
        ("跨天区间深夜", datetime(2026, 10, 19, 23, 30), 1 << 18),
        ("周六白天", datetime(2026, 10, 24, 12, 0), None),
    ]
    ok1 = all(C.schedule_limit(sch, t) == exp for _, t, exp in cases)
    results.append(("rules", ok1, {n: C.schedule_limit(sch, t) for n, t, _ in cases}))

    # ---- 2. 空规则 ----
    ok2 = C.schedule_limit([], datetime(2026, 10, 19, 12, 0)) is None
    results.append(("empty", ok2, {}))

    # ---- 3. 优先级：时段 > 全局默认，显式又压时段 ----
    cfg = {"connections": 8, "limit_bps": 5_000_000, "speed_schedule": ALL_DAY,
           "site_connections": {}}
    r_sched = C.resolve("https://x/y", cfg, now=datetime(2026, 10, 19, 12, 0))
    r_exp = C.resolve("https://x/y", cfg, limit_bps=2_000_000,
                      now=datetime(2026, 10, 19, 12, 0))
    ok3 = r_sched["limit_bps"] == 1_000_000 and r_exp["limit_bps"] == 2_000_000
    results.append(("precedence", ok3, {"sched": r_sched["limit_bps"],
                                        "explicit": r_exp["limit_bps"]}))

    # ---- 4. 运行中动态切换 ----
    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/f.bin"
    work = tempfile.mkdtemp(prefix="sched-")
    dl = Download(url, dest=os.path.join(work, "a.bin"), connections=2,
                  limit_bps=None, schedule=list(ALL_DAY))
    t = threading.Thread(target=lambda: dl.run(), daemon=True)
    t.start()
    time.sleep(1.5)
    first = dl._limiter.bps
    dl.schedule = [{"start": "00:00", "end": "23:59", "limit_bps": 400_000}]
    time.sleep(6.5)                      # 监听线程间隔 5s
    second = dl._limiter.bps
    dl.cancel()
    t.join(10)
    ok4 = first == 1_000_000 and second == 400_000
    results.append(("runtime-switch", ok4, {"before": first, "after": second}))

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
