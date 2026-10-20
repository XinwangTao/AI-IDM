"""浏览器接管服务（ai-idm serve）的集成测试：鉴权 / 入队下载 / 路径穿越 / 取消。"""

import hashlib
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

WORK = tempfile.mkdtemp(prefix="idmsrv-")
# 隔离用户配置，避免读到本机 config.json
os.environ["AI_IDM_HOME"] = WORK

try:
    from ai_idm.stdio import ensure_utf8_stdio
    ensure_utf8_stdio()
except Exception:
    pass

from ai_idm.queue import Queue  # noqa: E402
from ai_idm.server import TakeoverServer, sanitize_filename  # noqa: E402

PAYLOAD = bytes((i * 37 + 11) & 0xFF for i in range(3 * 1024 * 1024))
SEEN_HEADERS = []
FILENAME = "server-name.bin"


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        SEEN_HEADERS.append(dict(self.headers))
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"srv1"')
        self.send_header("Content-Disposition", f'attachment; filename="{FILENAME}"')
        self.end_headers()

    def do_GET(self):
        SEEN_HEADERS.append(dict(self.headers))
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


class S(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def call(base, path, payload=None, token=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=body,
                                 method="POST" if body else "GET")
    if body:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-AI-IDM-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return r.status, {"_raw": raw.decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def wait_status(base, token, tid, want, timeout=25):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        code, d = call(base, f"/status/{tid}", token=token)
        last = d.get("status")
        if code == 200 and last in want:
            return last
        time.sleep(0.25)
    return last


def main():
    results = []

    web = S(("127.0.0.1", 0), H)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{web.server_address[1]}/f.bin"
    expect = hashlib.sha256(PAYLOAD).hexdigest()

    dl_dir = os.path.join(WORK, "dl")
    os.makedirs(dl_dir, exist_ok=True)
    db = os.path.join(WORK, "queue.db")
    token = "test-token-123"
    srv = TakeoverServer(host="127.0.0.1", port=0, token=token, parallel=1,
                         download_dir=dl_dir, run_downloads=True, queue=Queue(db))
    srv.start()
    base = f"http://127.0.0.1:{srv.port}"

    # ---- 1. 鉴权：ping 开放；status/add 需要令牌 ----
    c1, d1 = call(base, "/ping")
    c2, _ = call(base, "/status")
    c3, _ = call(base, "/status", token=token)
    c4, _ = call(base, "/add", {"url": url}, token="wrong-token")
    results.append(("auth", c1 == 200 and d1.get("name") == "ai-idm"
                    and c2 == 401 and c3 == 200 and c4 == 401,
                    {"ping": c1, "status_no_token": c2, "status_token": c3, "bad_token": c4}))

    # ---- 2. 入队并下载：文件名来自 Content-Disposition，落到下载目录 ----
    c5, d5 = call(base, "/add", {"url": url, "referer": "https://example.com/a",
                                 "ua": "TestUA/1.0"}, token=token)
    tid = d5.get("id")
    st = wait_status(base, token, tid, {"done", "error"}, timeout=30)
    dest = os.path.join(dl_dir, FILENAME)
    results.append(("download", c5 == 200 and st == "done" and os.path.exists(dest)
                    and sha256(dest) == expect,
                    {"http": c5, "status": st, "dest": os.path.basename(dest)}))

    # ---- 3. Referer/UA 是否透传到源站 ----
    flat = [{k.lower(): v for k, v in h.items()} for h in SEEN_HEADERS]
    ref_ok = any(h.get("referer") == "https://example.com/a" for h in flat)
    ua_ok = any(h.get("user-agent") == "TestUA/1.0" for h in flat)
    results.append(("headers", ref_ok and ua_ok, {"referer": ref_ok, "ua": ua_ok}))

    # ---- 4. 非法协议 / 缺少 url ----
    c6, _ = call(base, "/add", {"url": "file:///C:/Windows/win.ini"}, token=token)
    c7, _ = call(base, "/add", {"url": "javascript:alert(1)"}, token=token)
    c8, _ = call(base, "/add", {}, token=token)
    results.append(("url-validate", c6 == 400 and c7 == 400 and c8 == 400,
                    {"file": c6, "js": c7, "empty": c8}))

    # ---- 5. 路径穿越与 header 注入 ----
    c9, d9 = call(base, "/add", {"url": url, "filename": "../../evil.bin",
                                 "headers": {"X-Bad": "a\r\nInjected: 1"}}, token=token)
    got = d9.get("dest") or ""
    inside = os.path.abspath(got).startswith(os.path.abspath(dl_dir) + os.sep)
    st9 = wait_status(base, token, d9.get("id"), {"done", "error"}, timeout=30)
    injected = any("injected" in h for h in (x for x in
                                             ({k.lower(): v for k, v in h.items()}
                                              for h in SEEN_HEADERS)))
    results.append(("path+inject", c9 == 200 and inside and st9 == "done"
                    and not injected and not os.path.exists(
                        os.path.join(WORK, "..", "evil.bin")),
                    {"dest": os.path.basename(got), "injected": injected}))

    # ---- 6. sanitize_filename 单测 ----
    cases = {
        "../../etc/passwd": "passwd",
        "C:\\Windows\\system32\\drivers\\etc\\hosts": "hosts",
        "CON": "_CON",
        "  ..  ": None,
        "": None,
    }
    ok6 = all(sanitize_filename(k) == v for k, v in cases.items())
    results.append(("sanitize", ok6, {k: sanitize_filename(k) for k in list(cases)[:3]}))

    # ---- 7. 取消正在下载的任务 ----
    c10, d10 = call(base, "/add", {"url": url, "filename": "slow.bin",
                                   "limit": 200_000}, token=token)
    slow_id = d10.get("id")
    wait_status(base, token, slow_id, {"running"}, timeout=15)
    time.sleep(1.2)                      # 让它真的下起来一点
    c11, d11 = call(base, f"/cancel/{slow_id}", {}, token=token)
    st11 = wait_status(base, token, slow_id, {"cancelled", "done", "error"}, timeout=20)
    results.append(("cancel", c10 == 200 and c11 == 200 and st11 == "cancelled"
                    and not os.path.exists(os.path.join(dl_dir, "slow.bin")),
                    {"cancel_http": c11, "status": st11}))

    # ---- 8. 面板与事件接口 ----
    c12, _ = call(base, "/", token=token)
    c13, d13 = call(base, "/events", token=token)
    c14, d14 = call(base, "/status", token=token)
    has_done = any(t.get("event") == "done" for t in d13.get("events", []))
    results.append(("panel+events", c12 == 200 and c13 == 200 and has_done
                    and len(d14.get("tasks", [])) >= 3,
                    {"panel": c12, "events": len(d13.get("events", [])),
                     "tasks": len(d14.get("tasks", []))}))

    print("\n=== 结果 ===")
    allok = True
    for name, ok, detail in results:
        allok &= bool(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name:14} {detail}")
    print("\nALL PASS" if allok else "\nSOME FAILED")

    srv.stop()
    web.shutdown()
    shutil.rmtree(WORK, ignore_errors=True)
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
