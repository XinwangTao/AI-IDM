"""自适应并发：服务端 429 时把有效并发减半（只降不升），下载仍能完成。"""

import hashlib
import http.server
import os
import re
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    from ai_idm.stdio import ensure_utf8_stdio
    ensure_utf8_stdio()
except Exception:
    pass

from ai_idm.engine import Download  # noqa: E402

PAYLOAD = bytes((i * 53 + 7) & 0xFF for i in range(4 * 1024 * 1024))
STATE = {"left": 3}
LOCK = threading.Lock()


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
        with LOCK:
            limited = STATE["left"] > 0
            if limited:
                STATE["left"] -= 1
        if limited:
            # Retry-After: 0 让重试立即发生，测试不拖时间
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(m.group(1))
            end = min(int(m.group(2) or len(PAYLOAD) - 1), len(PAYLOAD) - 1)
            body = PAYLOAD[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)


def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/big.bin"
    work = tempfile.mkdtemp(prefix="idmadapt-")
    dest = os.path.join(work, "a.bin")

    r = Download(url, dest=dest, connections=8, max_retries=4).run()
    expect = hashlib.sha256(PAYLOAD).hexdigest()
    got = hashlib.sha256(open(dest, "rb").read()).hexdigest() if os.path.exists(dest) else ""
    ok = (r["status"] == "done" and got == expect
          and r.get("adaptive_downs", 0) >= 1 and r.get("connection_cap", 8) < 8)
    print("\n=== 结果 ===")
    print(f"{'PASS' if ok else 'FAIL'}  adaptive-429  "
          f"{ {'status': r['status'], 'drops': r.get('adaptive_downs'), 'cap': r.get('connection_cap')} }")
    print("\nALL PASS" if ok else "\nSOME FAILED")
    srv.shutdown()
    import shutil
    shutil.rmtree(work, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
