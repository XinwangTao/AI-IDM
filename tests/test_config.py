"""配置层测试：站点并发匹配、优先级、以及配置真的影响实际连接数。"""

import hashlib
import http.server
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from idmclone import config as C  # noqa: E402

PAYLOAD = bytes((i * 17 + 3) & 0xFF for i in range(6 * 1024 * 1024))
RANGES = []
# 动态分块下"不同 Range 数"不再等于连接数（块比连接多），所以直接量并发度
INFLIGHT = {"cur": 0, "max": 0}
LOCK = threading.Lock()


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"c1"')
        self.end_headers()

    def do_GET(self):
        with LOCK:
            INFLIGHT["cur"] += 1
            INFLIGHT["max"] = max(INFLIGHT["max"], INFLIGHT["cur"])
        try:
            self._serve()
        finally:
            with LOCK:
                INFLIGHT["cur"] -= 1

    def _serve(self):
        time.sleep(0.05)           # 稍微拖慢，保证并发峰值可稳定观测
        rng = self.headers.get("Range")
        RANGES.append(rng)
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
    results = []

    # ---- 1. 匹配规则 ----
    m = {"example.com": 16, "*.cdn.com": 32, "*.a.cdn.com": 64}
    ok1 = (C.match_site("example.com", m) == 16
           and C.match_site("x.cdn.com", m) == 32
           and C.match_site("y.a.cdn.com", m) == 64        # 最长后缀优先
           and C.match_site("nope.org", m) is None
           and C.match_site("example.com.evil.net", m) is None)
    results.append(("match-site", ok1, {}))

    # ---- 2. 优先级：显式 > 站点 > 默认 ----
    cfg = dict(C.DEFAULTS, connections=8, site_connections={"h.com": 20})
    r1 = C.resolve("https://h.com/f", cfg)
    r2 = C.resolve("https://h.com/f", cfg, connections=3)
    r3 = C.resolve("https://other.com/f", cfg)
    ok2 = (r1["connections"] == 20 and r2["connections"] == 3 and r3["connections"] == 8)
    results.append(("precedence", ok2, {"site": r1["connections"], "explicit": r2["connections"],
                                        "default": r3["connections"]}))

    # ---- 3. 配置文件读写 ----
    home = tempfile.mkdtemp(prefix="cfg-")
    os.environ["IDMCLONE_HOME"] = home
    cfg2 = C.load()
    cfg2["connections"] = 12
    cfg2["site_connections"]["127.0.0.1"] = 3
    C.save(cfg2)
    cfg3 = C.load()
    ok3 = cfg3["connections"] == 12 and cfg3["site_connections"].get("127.0.0.1") == 3
    results.append(("io-roundtrip", ok3, {"path": C.config_path()}))

    # ---- 4. CLI config set-site / show ----
    env = dict(os.environ, IDMCLONE_HOME=home, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")
    subprocess.run([sys.executable, "-m", "idmclone", "config", "set-site", "demo.test", "24"],
                   cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    show = subprocess.run([sys.executable, "-m", "idmclone", "config", "show"],
                          cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok4 = '"demo.test": 24' in show.stdout.replace("\n", " ") or '"demo.test":24' in show.stdout
    results.append(("cli-config", ok4, {"show_head": show.stdout[:60].replace("\n", " ")}))

    # ---- 5. 端到端：配置的站点并发真的生效 ----
    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/big.bin"
    dest = os.path.join(home, "out.bin")
    RANGES.clear()
    INFLIGHT.update({"cur": 0, "max": 0})
    p = subprocess.run([sys.executable, "-m", "idmclone", "get", url, "-o", dest, "-q"],
                       cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    # 首段按 IDM 的做法不带 Range（纯 GET），所以这里把 None 也算一票
    distinct = len({"(none)" if r is None else r for r in RANGES})
    expect = hashlib.sha256(PAYLOAD).hexdigest()
    got = hashlib.sha256(open(dest, "rb").read()).hexdigest() if os.path.exists(dest) else ""
    # 站点并发=3：并发请求峰值不得超过 3，且确实并行过（>=2）
    peak = INFLIGHT["max"]
    results.append(("config-connections",
                    got == expect and peak == 3 and distinct >= 3,
                    {"peak_inflight": peak, "distinct_ranges": distinct,
                     "rc": p.returncode}))

    print("\n=== 结果 ===")
    allok = True
    for name, ok, detail in results:
        allok &= bool(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name:18} {detail}")
    print("\nALL PASS" if allok else "\nSOME FAILED")
    srv.shutdown()
    shutil.rmtree(home, ignore_errors=True)
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
