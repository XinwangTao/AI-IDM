"""HLS(m3u8) 端到端测试：清单解析、码率选择、AES-128、断点续传。"""

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


from idmclone.hls import (aes128_cbc_encrypt, download_hls, parse_media,  # noqa: E402
                          parse_master)

KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
IV_HEX = "000102030405060708090a0b0c0d0e0f"
IV = bytes.fromhex(IV_HEX)

# 两种码率，各 5 个分片，内容不同
HI = [bytes([0xA0 + i]) * (100_000 + i) for i in range(5)]
LO = [bytes([0x50 + i]) * (50_000 + i) for i in range(5)]
ENC = [bytes([0x11 + i]) * (40_000 + i) for i in range(4)]
HITS = {}


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/octet-stream"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        body = self._resolve()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        p = self.path
        HITS[p] = HITS.get(p, 0) + 1
        self._send(self._resolve())

    def _resolve(self):
        p = self.path
        if p == "/master.m3u8":
            return (b"#EXTM3U\n"
                    b"#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=640x360\n"
                    b"lo/index.m3u8\n"
                    b"#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720\n"
                    b"hi/index.m3u8\n")
        if p == "/hi/index.m3u8":
            return self._media(HI, "/hi/seg{i}.ts")
        if p == "/lo/index.m3u8":
            return self._media(LO, "/lo/seg{i}.ts")
        if p == "/enc/index.m3u8":
            body = (f'#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n'
                    f'#EXT-X-KEY:METHOD=AES-128,URI="k.bin",IV=0x{IV_HEX}\n')
            for i in range(len(ENC)):
                body += f"#EXTINF:2.0,\nseg{i}.ts\n"
            return body.encode()
        if p == "/enc/k.bin":
            return KEY
        if p.startswith("/hi/seg") and p.endswith(".ts"):
            return HI[int(p[len("/hi/seg"):-3])]
        if p.startswith("/lo/seg") and p.endswith(".ts"):
            return LO[int(p[len("/lo/seg"):-3])]
        if p.startswith("/enc/seg") and p.endswith(".ts"):
            i = int(p[len("/enc/seg"):-3])
            return aes128_cbc_encrypt(KEY, IV, ENC[i])
        return b""

    @staticmethod
    def _media(segs, tmpl):
        body = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:0\n"
        for i in range(len(segs)):
            body += f"#EXTINF:2.0,\n{tmpl.format(i=i)}\n"
        body += "#EXT-X-ENDLIST\n"
        return body.encode()


class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    def handle_error(self, request, client_address):
        pass                      # 客户端读够即关会产生 BrokenPipe，属正常
    allow_reuse_address = True


def main():
    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    work = tempfile.mkdtemp(prefix="hls-")
    results = []

    # 0. AES 自洽（加密→解密）
    from idmclone.hls import aes128_cbc_decrypt
    ok_aes = aes128_cbc_decrypt(KEY, IV, aes128_cbc_encrypt(KEY, IV, b"hello hls" * 100)) == b"hello hls" * 100
    results.append(("aes-roundtrip", ok_aes, {"len": len(b"hello hls" * 100)}))

    # 1. best → 选高码率
    out1 = os.path.join(work, "best.ts")
    r1 = download_hls(f"{base}/master.m3u8", out1, connections=4)
    ok1 = open(out1, "rb").read() == b"".join(HI)
    results.append(("master-best", ok1, {"variant": r1["variant"]["bandwidth"], "size": r1["size"]}))

    # 2. worst → 选低码率
    out2 = os.path.join(work, "worst.ts")
    r2 = download_hls(f"{base}/master.m3u8", out2, quality="worst", connections=2)
    ok2 = open(out2, "rb").read() == b"".join(LO)
    results.append(("master-worst", ok2, {"variant": r2["variant"]["bandwidth"], "size": r2["size"]}))

    # 3. AES-128 加密分片
    out3 = os.path.join(work, "enc.ts")
    r3 = download_hls(f"{base}/enc/index.m3u8", out3, connections=3)
    ok3 = open(out3, "rb").read() == b"".join(ENC)
    results.append(("aes128-hls", ok3, {"encrypted": r3["encrypted"], "size": r3["size"]}))

    # 4. 断点续传：保留分片 → 删一个 → 重跑，只应重下那一个
    out4 = os.path.join(work, "resume.ts")
    download_hls(f"{base}/hi/index.m3u8", out4, connections=2, keep_segments=True)
    segdir = out4 + ".hlsseg"
    os.remove(os.path.join(segdir, "seg_00002.bin"))
    before_all = {k: v for k, v in HITS.items() if k.startswith("/hi/seg")}
    before = HITS.get("/hi/seg2.ts", 0)
    r4 = download_hls(f"{base}/hi/index.m3u8", out4, connections=2, keep_segments=True)
    after = HITS.get("/hi/seg2.ts", 0)
    after_all = {k: v for k, v in HITS.items() if k.startswith("/hi/seg")}
    print("   DEBUG resume  before:", before_all, " after:", after_all)
    ok4 = (open(out4, "rb").read() == b"".join(HI)) and (after - before == 1)
    results.append(("hls-resume", ok4, {"refetch": after - before}))

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
