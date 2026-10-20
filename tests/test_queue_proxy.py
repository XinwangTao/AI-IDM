"""队列持久化 + SOCKS5 代理的集成测试。"""

import hashlib
import http.server
import json
import os
import shutil
import socket
import socketserver
import struct
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
from ai_idm.queue import Queue, Runner  # noqa: E402

PAYLOAD = bytes((i * 91 + 7) & 0xFF for i in range(3 * 1024 * 1024))  # 3 MB


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"q1"')
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


# ---------------- 极简 SOCKS5 服务端（仅 CONNECT + 无认证） ----------------

SOCKS_LOG = []


def socks5_serve(port_holder):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    port_holder.append(srv.getsockname()[1])

    def handle(conn):
        try:
            ver, n = conn.recv(2)
            conn.recv(n)
            conn.sendall(b"\x05\x00")
            head = conn.recv(4)
            atyp = head[3]
            if atyp == 1:
                host = socket.inet_ntoa(conn.recv(4))
            elif atyp == 3:
                ln = conn.recv(1)[0]
                host = conn.recv(ln).decode()
            else:
                raise RuntimeError("bad atyp")
            port = struct.unpack(">H", conn.recv(2))[0]
            SOCKS_LOG.append((host, port))
            upstream = socket.create_connection((host, port), 5)
            conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            def pipe(a, b):
                try:
                    while True:
                        d = a.recv(65536)
                        if not d:
                            break
                        b.sendall(d)
                except Exception:
                    pass
                finally:
                    try: b.shutdown(socket.SHUT_WR)
                    except Exception: pass
            t = threading.Thread(target=pipe, args=(conn, upstream), daemon=True)
            t.start()
            pipe(upstream, conn)
            t.join(timeout=2)
            upstream.close()
        except Exception:
            pass
        finally:
            try: conn.close()
            except Exception: pass

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return srv


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    results = []
    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/f.bin"
    expect = hashlib.sha256(PAYLOAD).hexdigest()

    work = tempfile.mkdtemp(prefix="idmq-")
    db = os.path.join(work, "queue.db")

    # ---- 1. 队列：入队 → 并行消费 → 全部完成 ----
    q = Queue(db)
    ids = [q.add(url, dest=os.path.join(work, f"q{i}.bin"), connections=4) for i in range(3)]
    stats = Runner(q, parallel=2).run(idle_exit=True)
    files_ok = all(os.path.exists(os.path.join(work, f"q{i}.bin"))
                   and sha256(os.path.join(work, f"q{i}.bin")) == expect for i in range(3))
    results.append(("queue-run", stats.get("done", {}).get("count") == 3 and files_ok,
                    {"stats": {k: v["count"] for k, v in stats.items()}}))

    # ---- 2. 跨进程恢复：遗留 running 被回收，且续传完成 ----
    tid = q.add(url, dest=os.path.join(work, "crash.bin"), connections=4)
    q.update(tid, status="running", started_at=time.time())
    q2 = Queue(db)                      # 模拟新进程打开同一个库
    stats2 = Runner(q2, parallel=1).run(idle_exit=True)
    rec = q2.get(tid)
    results.append(("queue-recover", rec["status"] == "done"
                    and sha256(os.path.join(work, "crash.bin")) == expect,
                    {"status": rec["status"]}))

    # ---- 3. 断点续传后由队列接续（先取消一次，再入队跑完）----
    part = os.path.join(work, "resume.bin")
    dl = Download(url, dest=part, connections=2, limit_bps=800_000)
    t = threading.Thread(target=dl.run, daemon=True)
    t.start(); time.sleep(1.0); dl.cancel(); t.join(10)
    tid3 = q.add(url, dest=part, connections=4)
    q3 = Queue(db)
    Runner(q3, parallel=1).run(idle_exit=True)
    results.append(("queue-resume", q3.get(tid3)["status"] == "done" and sha256(part) == expect,
                    {"status": q3.get(tid3)["status"]}))

    # ---- 4. SOCKS5 代理 ----
    holder = []
    socks5_serve(holder)
    time.sleep(0.2)
    pxy = f"socks5h://127.0.0.1:{holder[0]}"
    dest2 = os.path.join(work, "socks.bin")
    r = Download(url, dest=dest2, connections=4, proxy=pxy).run()
    results.append(("socks5-proxy",
                    r["status"] == "done" and sha256(dest2) == expect and len(SOCKS_LOG) >= 1,
                    {"socks_connects": len(SOCKS_LOG), "target": SOCKS_LOG[0] if SOCKS_LOG else None}))

    # ---- 5. 队列经 SOCKS5 下载 ----
    tid4 = q.add(url, dest=os.path.join(work, "socksq.bin"), connections=2, proxy=pxy)
    q4 = Queue(db)
    Runner(q4, parallel=1).run(idle_exit=True)
    ok4 = (q4.get(tid4)["status"] == "done"
           and sha256(os.path.join(work, "socksq.bin")) == expect)
    results.append(("queue+socks", ok4, {"status": q4.get(tid4)["status"]}))

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
