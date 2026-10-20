"""完整性抽样校验：模拟"第一次返回的内容与后续不一致"，应被发现并整包重下。"""

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
try:
    from idmclone.stdio import ensure_utf8_stdio
    ensure_utf8_stdio()          # 输出含中文，非 UTF-8 控制台会崩
except Exception:
    pass


from idmclone.engine import Download  # noqa: E402

TOTAL = 4 * 1024 * 1024
PAYLOAD = bytes((i * 71 + 13) & 0xFF for i in range(TOTAL))
MID = TOTAL // 2
STATE = {"corrupted": False, "plain_gets": 0}


def corrupt(chunk, start):
    """把落在 MID 附近的一段字节改坏，模拟传输/磁盘损坏。"""
    b = bytearray(chunk)
    for off in range(max(0, MID - start), min(len(b), MID - start + 16)):
        b[off] ^= 0xFF
    return bytes(b)


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(TOTAL))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        rng = self.headers.get("Range")
        if not rng:
            STATE["plain_gets"] += 1
            self.send_response(200)
            self.send_header("Content-Length", str(TOTAL))
            self.end_headers()
            self.wfile.write(PAYLOAD)
            return
        a, _, b = rng.split("=", 1)[1].partition("-")
        s = int(a) if a else 0
        e = min(int(b) if b else TOTAL - 1, TOTAL - 1)
        data = PAYLOAD[s:e + 1]
        if not STATE["corrupted"] and s == MID:
            # 只有"从 MID 开始"的那一段会被改坏（含接缝多读的请求不算），
            # 保证坏数据确实落进文件，而不是只落在被丢弃的接缝样本里
            data = corrupt(data, s)
            STATE["corrupted"] = True
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {s}-{e}/{TOTAL}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    def handle_error(self, request, client_address):
        pass                      # 客户端读够即关会产生 BrokenPipe，属正常
    allow_reuse_address = True


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/f.bin"
    work = tempfile.mkdtemp(prefix="verify-")
    expect = hashlib.sha256(PAYLOAD).hexdigest()
    results = []

    # ---- 1. 不校验：坏数据被写进结果（对照组）----
    # 必须同时关掉接缝校验，否则这个"坏在段边界上"的场景会被接缝比对抓住
    STATE.update({"corrupted": False, "plain_gets": 0})
    d0 = os.path.join(work, "no-verify.bin")
    r0 = Download(url, dest=d0, connections=2, verify_sample=0,
                  verify_seam=False).run()
    bad = sha(d0) != expect
    results.append(("control-bad", r0["status"] == "done" and bad, {"corrupted_saved": bad}))

    # ---- 2. 开抽样：发现不一致 → 整包重下 → 结果正确 ----
    STATE.update({"corrupted": False, "plain_gets": 0})
    d1 = os.path.join(work, "verified.bin")
    r1 = Download(url, dest=d1, connections=2, verify_sample=3).run()
    ok1 = r1["status"] == "done" and sha(d1) == expect and r1.get("mismatch_detected")
    results.append(("sample-verify", ok1,
                    {"mismatch": r1.get("mismatch_detected"),
                     "plain_gets": STATE["plain_gets"], "hash_ok": sha(d1) == expect}))

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
