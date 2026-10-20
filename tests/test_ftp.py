"""FTP 下载测试：多连接分片 + 断点续传（自建最小 FTP 服务端）。"""

import hashlib
import os
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    from idmclone.stdio import ensure_utf8_stdio
    ensure_utf8_stdio()          # 输出含中文，非 UTF-8 控制台会崩
except Exception:
    pass


from idmclone.ftp import FtpDownload  # noqa: E402

PAYLOAD = bytes((i * 31 + 7) & 0xFF for i in range(12 * 1024 * 1024))   # 12MB
RESTS = []          # 每次 RETR 前的 REST 偏移
SIZE_SUPPORT = {"on": True}


class FtpHandler(socketserver.StreamRequestHandler):
    def handle(self):
        conn = self.connection
        conn.sendall(b"220 idmclone-test FTP\r\n")
        offset = 0
        pasv = None
        while True:
            line = self.rfile.readline()
            if not line:
                break
            try:
                cmd, _, arg = line.decode("latin1").strip().partition(" ")
            except Exception:
                break
            cmd = cmd.upper()
            if cmd == "USER":
                conn.sendall(b"331 need password\r\n")
            elif cmd == "PASS":
                conn.sendall(b"230 logged in\r\n")
            elif cmd in ("TYPE", "NOOP", "CWD", "OPTS"):
                conn.sendall(b"200 ok\r\n")
            elif cmd == "SIZE":
                if SIZE_SUPPORT["on"]:
                    conn.sendall(f"213 {len(PAYLOAD)}\r\n".encode())
                else:
                    conn.sendall(b"502 no SIZE\r\n")
            elif cmd == "REST":
                offset = int(arg.strip() or 0)
                conn.sendall(b"350 rest ok\r\n")
            elif cmd == "PASV":
                pasv = socket.socket()
                pasv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                pasv.bind(("127.0.0.1", 0))
                pasv.listen(1)
                p = pasv.getsockname()[1]
                conn.sendall(f"227 Entering Passive Mode (127,0,0,1,{p >> 8},{p & 0xFF})\r\n".encode())
            elif cmd == "RETR":
                if pasv is None:
                    conn.sendall(b"425 need PASV\r\n")
                    continue
                conn.sendall(b"150 opening data\r\n")
                data, _ = pasv.accept()
                RESTS.append(offset)
                body = PAYLOAD[offset:]
                try:
                    for i in range(0, len(body), 65536):
                        data.sendall(body[i:i + 65536])
                        time.sleep(0.03)
                except Exception:
                    pass
                finally:
                    try:
                        data.close()
                    except Exception:
                        pass
                    pasv.close()
                    pasv = None
                conn.sendall(b"226 transfer complete\r\n")
                offset = 0
            elif cmd == "QUIT":
                conn.sendall(b"221 bye\r\n")
                break
            else:
                conn.sendall(b"500 unknown\r\n")


class FtpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    srv = FtpServer(("127.0.0.1", 0), FtpHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"ftp://test:test@127.0.0.1:{port}/big.bin"
    expect = hashlib.sha256(PAYLOAD).hexdigest()
    work = tempfile.mkdtemp(prefix="ftp-")
    results = []

    # ---- 1. 多连接分片 ----
    RESTS.clear()
    dest = os.path.join(work, "a.bin")
    r = FtpDownload(url, dest=dest, connections=4).run()
    distinct = len(set(RESTS))
    results.append(("ftp-multipart",
                    r["status"] == "done" and sha(dest) == expect and distinct >= 2,
                    {"segments": r["segments"], "distinct_rest": distinct,
                     "rests": sorted(set(RESTS))[:5]}))

    # ---- 2. 断点续传 ----
    dest2 = os.path.join(work, "b.bin")
    dl = FtpDownload(url, dest=dest2, connections=2)
    t = threading.Thread(target=lambda: dl.run(), daemon=True)
    t.start()
    time.sleep(0.9)
    dl.cancel()
    t.join(15)
    part = dl.snapshot()
    has_temp = os.path.exists(dest2 + ".idmpart")
    has_state = os.path.exists(dest2 + ".idmpart.json")
    r2 = FtpDownload(url, dest=dest2, connections=2).run()
    results.append(("ftp-resume",
                    has_temp and has_state and 0 < part["downloaded"] < len(PAYLOAD)
                    and r2["status"] == "done" and sha(dest2) == expect,
                    {"partial": part["downloaded"], "temp": has_temp,
                     "state": has_state, "final": r2["status"]}))

    # ---- 3. SIZE 不支持 → 单连接顺序读 ----
    SIZE_SUPPORT["on"] = False
    dest3 = os.path.join(work, "c.bin")
    r3 = FtpDownload(url, dest=dest3, connections=4).run()
    results.append(("ftp-no-size",
                    r3["status"] == "done" and sha(dest3) == expect,
                    {"size": os.path.getsize(dest3), "segments": r3["segments"]}))
    SIZE_SUPPORT["on"] = True

    print("\n=== 结果 ===")
    allok = True
    for name, ok, detail in results:
        allok &= bool(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name:16} {detail}")
    print("\nALL PASS" if allok else "\nSOME FAILED")
    shutil.rmtree(work, ignore_errors=True)
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
