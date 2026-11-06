"""批量小文件：并发上限、单请求、失败清单、--resume 补齐。"""

import hashlib
import http.server
import os
import re
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    from ai_idm.stdio import ensure_utf8_stdio
    ensure_utf8_stdio()
except Exception:
    pass

from ai_idm.batch import dest_for, read_urls, run_batch  # noqa: E402

N = 60
SIZE = 20 * 1024
FILES = [bytes([i % 251]) * SIZE for i in range(N)]
STATE = {"inflight": 0, "peak": 0, "requests": 0, "flaky": 2, "bad_ok": False}
LOCK = threading.Lock()


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self):
        p = self.path
        # 先匹配具名文件：/flaky.bin 也以 /f 开头、.bin 结尾，不能先走通配
        if p == "/flaky.bin":
            return FILES[0], True
        if p == "/bad.bin":
            return b"", STATE["bad_ok"]
        if p.startswith("/f") and p.endswith(".bin"):
            return FILES[int(p[2:-4])], True
        return b"", False

    def do_GET(self):
        with LOCK:
            STATE["requests"] += 1
            STATE["inflight"] += 1
            STATE["peak"] = max(STATE["peak"], STATE["inflight"])
        try:
            if self.path == "/flaky.bin":
                with LOCK:
                    limit = STATE["flaky"] > 0
                    if limit:
                        STATE["flaky"] -= 1
                if limit:
                    self.send_response(500)
                    self.send_header("Retry-After", "0")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            body, ok = self._body()
            if not ok:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(body)
        finally:
            with LOCK:
                STATE["inflight"] -= 1


def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    work = tempfile.mkdtemp(prefix="batch-")
    results = []

    urls = [f"{base}/f{i}.bin" for i in range(N)] + [f"{base}/flaky.bin", f"{base}/bad.bin"]
    summary = run_batch(urls, os.path.join(work, "out"), concurrency=8, per_host=4,
                        timeout=10, max_retries=4)
    ok_files = all(
        open(os.path.join(work, "out", "127.0.0.1", f"f{i}.bin"), "rb").read() == FILES[i]
        for i in range(N))
    failed_file = os.path.join(work, "out", "failed.txt")
    failed_urls = open(failed_file, encoding="utf-8").read() if os.path.exists(failed_file) else ""
    results.append(("batch-download",
                    summary["done"] == N + 1 and summary["failed"] == 1 and ok_files
                    and "bad.bin" in failed_urls,
                    {"done": summary["done"], "failed": summary["failed"],
                     "peak": STATE["peak"], "requests": STATE["requests"]}))
    results.append(("per-host-cap", STATE["peak"] <= 4, {"peak": STATE["peak"]}))

    # resume：已存在的跳过，失败的补齐
    STATE["bad_ok"] = True
    before = STATE["requests"]
    summary2 = run_batch(urls, os.path.join(work, "out"), concurrency=8, per_host=4,
                         timeout=10, resume=True)
    results.append(("resume",
                    summary2["skipped"] == N + 1 and summary2["failed"] == 0
                    and summary2["done"] == 1 and STATE["requests"] - before <= 3,
                    {"skipped": summary2["skipped"], "done": summary2["done"],
                     "requests": STATE["requests"] - before}))

    # 去重/路径规则
    dedupe = read_urls_brief(urls + urls[:3], work)
    results.append(("dedupe-paths",
                    dedupe["unique"] == len(urls)
                    and dedupe["dest"].endswith(os.path.join("127.0.0.1", "f0.bin")),
                    dedupe))

    # CLI 入口
    import json
    import subprocess
    cli_list = os.path.join(work, "cli_urls.txt")
    with open(cli_list, "w", encoding="utf-8") as f:
        f.write("\n".join(urls[:5]) + "\n")
    p = subprocess.run([sys.executable, "-m", "ai_idm", "batch", cli_list,
                        "-o", os.path.join(work, "cli"), "--json", "-q"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        out = json.loads(p.stdout)
    except Exception:                                          # noqa: BLE001
        out = {}
    results.append(("cli-batch", p.returncode == 0 and out.get("done") == 5,
                    {"rc": p.returncode, "done": out.get("done"),
                     "err": (p.stderr or "")[-120:]}))

    print("\n=== 结果 ===")
    allok = True
    for name, ok, detail in results:
        allok &= bool(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name:16} {detail}")
    print("\nALL PASS" if allok else "\nSOME FAILED")
    srv.shutdown()
    shutil.rmtree(work, ignore_errors=True)
    return 0 if allok else 1


def read_urls_brief(items, work):
    path = os.path.join(work, "urls.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# comment\n\n" + "\n".join(items) + "\n")
    got = read_urls(path)
    return {"unique": len(got), "dest": dest_for(got[0], os.path.join(work, "out"))}


if __name__ == "__main__":
    raise SystemExit(main())
