"""接缝校验 / 开区间 Range / 未开始块移交 —— 对齐 IDM 动态分析结论的三项测试。

动态分析证据（见 IDMan_动态分析.md）：
- 日志 `posInCmpBuff3=64`（最后一块 0）→ 相邻块重叠 64 字节逐字节比对
- 服务端看到 C0 无 Range、C1 `bytes=X-`、以及 `bytes=X-(起点+63)` 的闭区间
- `The file chunk of connection 3 is reassigned to connection 1`，且 downloaded=0
"""

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

from idmclone.engine import Download  # noqa: E402

TOTAL = 4 * 1024 * 1024
PAYLOAD = bytes((i * 29 + 7) & 0xFF for i in range(TOTAL))
MID = TOTAL // 2

STATE = {
    "corrupt_seam": False,     # 是否让第 2 段开头返回坏数据
    "seam_done": False,
    "ranges": [],              # 原始 Range 头（None = 完全不带）
    "fail_at": None,           # 对 start==这个值的请求返回 500
    "fail_left": 0,
}


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
            pass                        # 客户端读够就关，属正常（IDM 同样如此）

    def do_GET(self):
        rng = self.headers.get("Range")
        STATE["ranges"].append(rng)
        s, e = 0, TOTAL - 1
        if rng:
            a, _, b = rng.split("=", 1)[1].partition("-")
            s = int(a) if a else 0
            e = min(int(b) if b else TOTAL - 1, TOTAL - 1)

        if STATE["fail_at"] is not None and s == STATE["fail_at"] and STATE["fail_left"] > 0:
            STATE["fail_left"] -= 1
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        data = bytearray(PAYLOAD[s:e + 1])
        if STATE["corrupt_seam"] and not STATE["seam_done"] and s == MID:
            # 第 2 段开头 16 字节被改坏：与第 1 段多读的接缝数据对不上
            STATE["seam_done"] = True
            for i in range(16):
                data[i] ^= 0xFF

        if rng:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {s}-{e}/{TOTAL}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(TOTAL))
            self.end_headers()
        self._write(bytes(data))


class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass                        # 客户端读够即关会产生 BrokenPipe，属正常


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def reset(**kw):
    STATE.update({"corrupt_seam": False, "seam_done": False, "ranges": [],
                  "fail_at": None, "fail_left": 0})
    STATE.update(kw)


def main():
    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/f.bin"
    work = tempfile.mkdtemp(prefix="seam-")
    expect = hashlib.sha256(PAYLOAD).hexdigest()
    results = []

    # ---- 1. 接缝比对能抓住段边界的坏数据 ----
    reset(corrupt_seam=True)
    d1 = os.path.join(work, "seam.bin")
    r1 = Download(url, dest=d1, connections=2).run()
    results.append(("seam-detect",
                    r1["status"] == "done" and sha(d1) == expect
                    and r1.get("mismatch_detected")
                    and r1.get("seams_mismatched", 0) >= 1,
                    {"mismatch": r1.get("mismatch_detected"),
                     "seams": f'{r1.get("seams_checked")}/{r1.get("seams_mismatched")}'}))

    # ---- 2. 开区间 Range：首段不带 Range，其余 bytes=N-；接缝确有比对 ----
    reset()
    d2 = os.path.join(work, "open.bin")
    R = 4
    r2 = Download(url, dest=d2, connections=R, open_range=True).run()
    seen = [x for x in STATE["ranges"]]
    plain = [x for x in seen if x is None]
    open_form = [x for x in seen if x and x.endswith("-")]
    closed_form = [x for x in seen if x and not x.endswith("-")]
    ok2 = (r2["status"] == "done" and sha(d2) == expect
           and len(plain) == 1 and len(open_form) >= R - 1 and not closed_form
           and r2.get("seams_checked", 0) >= R - 1)
    results.append(("open-range", ok2,
                    {"plain_get": len(plain), "open": len(open_form),
                     "closed": len(closed_form), "seams_checked": r2.get("seams_checked")}))

    # ---- 3. 闭区间模式：请求要向后多要 64 字节（接缝重叠读）----
    reset()
    d3 = os.path.join(work, "closed.bin")
    r3 = Download(url, dest=d3, connections=4).run()   # 默认：闭区间 + 64 字节接缝
    extended = []
    for x in STATE["ranges"]:
        if not x:
            continue
        a, _, b = x.split("=", 1)[1].partition("-")
        if b:                       # 闭区间才可能带尾数
            s, e = int(a), int(b)
            if e - s + 1 > TOTAL // 4:      # 段长 1MB，超出说明多读了接缝
                extended.append((s, e - s + 1))
    ok3 = r3["status"] == "done" and sha(d3) == expect and len(extended) >= 3
    results.append(("closed-seam-overread", ok3,
                    {"extended_requests": len(extended),
                     "sample": extended[:2]}))

    # ---- 4. 未开始的块被移交给别的连接（handoffs）----
    reset(fail_at=MID, fail_left=3)
    d4 = os.path.join(work, "handoff.bin")
    r4 = Download(url, dest=d4, connections=2, max_retries=2).run()
    ok4 = (r4["status"] == "done" and sha(d4) == expect
           and r4.get("handoffs", 0) >= 1)
    results.append(("handoff-unstarted", ok4,
                    {"handoffs": r4.get("handoffs"),
                     "reassigned": r4.get("reassigned")}))

    print("\n=== 结果 ===")
    allok = True
    for name, ok, detail in results:
        allok &= bool(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name:22} {detail}")
    print("\nALL PASS" if allok else "\nSOME FAILED")
    srv.shutdown()
    shutil.rmtree(work, ignore_errors=True)
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
