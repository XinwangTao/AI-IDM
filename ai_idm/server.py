"""ai_idm.server — 浏览器接管服务（本地 HTTP API + 网页面板）。

浏览器扩展把链接 POST 到 `http://127.0.0.1:<port>/add`，这里落到 SQLite 队列，
由内置 Runner 多连接下载。命令行 `ai-idm serve` 启动。

安全模型：
- 默认只监听 127.0.0.1，拒绝非回环来源；
- 除 `/ping` 外所有接口都要 `X-AI-IDM-Token`（或 `?token=`）；
- 令牌首次启动自动生成并写进 config.json，扩展里填同一个值；
- 请求体上限 256 KB，URL 只接受 http/https/ftp/ftps，禁止 header 注入。
"""

from __future__ import annotations

import collections
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from . import config as cfgmod
from .queue import Queue, Runner

MAX_BODY = 256 * 1024
ALLOWED_SCHEMES = ("http", "https", "ftp", "ftps")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}

_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


# ---------------------------------------------------------------- 工具

def validate_url(url):
    if not isinstance(url, str) or not url.strip():
        raise ValueError("缺少 url")
    url = url.strip()
    if len(url) > 8192:
        raise ValueError("url 过长")
    p = urllib.parse.urlparse(url)
    if p.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"不支持的协议 {p.scheme!r}（只允许 http/https/ftp/ftps）")
    if not p.netloc:
        raise ValueError("url 缺少主机名")
    return url


def sanitize_filename(name):
    """只保留纯文件名，挡掉路径穿越与 Windows 保留名。"""
    if not name:
        return None
    name = str(name).replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "_", name).strip(" .")
    if not name or name in (".", ".."):
        return None
    stem = name.split(".")[0].upper()
    if stem in _WIN_RESERVED:
        name = "_" + name
    return name[:180]


def clean_headers(raw):
    """过滤自定义请求头：拒绝 CRLF 注入与非 ASCII 名。"""
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, v in list(raw.items())[:40]:
        if not isinstance(k, str) or not isinstance(v, (str, int, float)):
            continue
        k, v = k.strip(), str(v)
        if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}", k):
            continue
        if "\r" in v or "\n" in v or len(v) > 4096:
            continue
        out[k] = v
    return out


# ---------------------------------------------------------------- 服务

class TakeoverServer:
    def __init__(self, host="127.0.0.1", port=13391, token=None, parallel=2,
                 download_dir=None, run_downloads=True, queue=None):
        cfg = cfgmod.load()
        self.host = host or cfg.get("server_host") or "127.0.0.1"
        self.port = int(port if port is not None else (cfg.get("server_port") or 13391))
        self.token = token or cfg.get("server_token") or ""
        self.parallel = int(parallel or cfg.get("server_parallel") or 2)
        self.download_dir = (download_dir or cfg.get("download_dir")
                             or cfgmod.default_download_dir())
        self.run_downloads = run_downloads
        self.cfg = cfg
        self.q = queue or Queue()
        self.events = collections.deque(maxlen=200)
        self.hls_tasks = {}
        self._hls_lock = threading.Lock()
        self._httpd = None
        self._thread = None
        self.runner = None
        self._loopback_only = self.host in LOOPBACK
        if not self.token:
            raise ValueError("缺少令牌：请先调用 ensure_token(cfg)")

    # ---------- 生命周期 ----------

    def _make_handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "ai-idm"
            sys_version = ""

            # --- 基础设施 ---

            def log_message(self, fmt, *args):
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers",
                                 "Content-Type, X-AI-IDM-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Cache-Control", "no-store")

            def _json(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self._cors()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _html(self, code, text):
                body = text.encode("utf-8")
                self.send_response(code)
                self._cors()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _client_ok(self):
                if not app._loopback_only:
                    return True
                ip = self.client_address[0]
                if ip in LOOPBACK or ip.startswith("127."):
                    return True
                self._json(403, {"ok": False, "error": "只接受本机请求"})
                return False

            def _token_ok(self, qs, payload=None):
                given = (self.headers.get("X-AI-IDM-Token")
                         or (qs.get("token", [""])[0] if qs else "")
                         or (payload or {}).get("token", ""))
                if given and hmac.compare_digest(str(given), str(app.token)):
                    return True
                self._json(401, {"ok": False, "error": "令牌无效，请在扩展选项里填入 ai-idm serve 打印的令牌"})
                return False

            def _read_body(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if n > MAX_BODY:
                    self._json(413, {"ok": False, "error": "请求体过大"})
                    return None
                raw = self.rfile.read(n) if n else b""
                if not raw:
                    return {}
                ctype = (self.headers.get("Content-Type") or "").lower()
                try:
                    if "application/json" in ctype or raw[:1] in (b"{", b"["):
                        data = json.loads(raw.decode("utf-8", "replace"))
                        return data if isinstance(data, dict) else {}
                    return {k: v[0] for k, v in
                            urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
                    return None

            # --- 路由 ---

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if not self._client_ok():
                    return
                u = urllib.parse.urlparse(self.path)
                path, qs = u.path, urllib.parse.parse_qs(u.query)

                if path in ("/", "/index.html"):
                    return self._html(200, DASHBOARD)
                if path == "/ping":
                    return self._json(200, {"ok": True, "name": "ai-idm",
                                            "version": __version__,
                                            "download_dir": app.download_dir})
                if not self._token_ok(qs):
                    return
                if path == "/status":
                    return self._json(200, app.status())
                if path.startswith("/status/"):
                    row = app.get_task(path.rsplit("/", 1)[-1])
                    if row is None:
                        return self._json(404, {"ok": False, "error": "任务不存在"})
                    return self._json(200, row)
                if path == "/events":
                    return self._json(200, {"ok": True,
                                            "events": list(app.events)[-50:]})
                if path == "/config":
                    c = {k: v for k, v in app.cfg.items() if k != "server_token"}
                    c["download_dir"] = app.download_dir
                    c["server_port"] = app.port
                    return self._json(200, {"ok": True, "config": c})
                if path == "/add":
                    return self._handle_add({k: v[0] for k, v in qs.items()})
                return self._json(404, {"ok": False, "error": "未知接口"})

            def do_POST(self):
                if not self._client_ok():
                    return
                u = urllib.parse.urlparse(self.path)
                path, qs = u.path, urllib.parse.parse_qs(u.query)
                payload = self._read_body()
                if payload is None:
                    return
                if not self._token_ok(qs, payload):
                    return
                if path == "/add":
                    return self._handle_add(payload)
                if path.startswith("/cancel/"):
                    return self._handle_cancel(path.rsplit("/", 1)[-1])
                if path.startswith("/remove/"):
                    ok = app.q.remove(path.rsplit("/", 1)[-1])
                    return self._json(200, {"ok": ok})
                return self._json(404, {"ok": False, "error": "未知接口"})

            # --- 业务 ---

            def _handle_add(self, p):
                try:
                    url = validate_url(p.get("url"))
                except ValueError as e:
                    return self._json(400, {"ok": False, "error": str(e)})

                headers = clean_headers(p.get("headers"))
                for key, hname in (("referer", "Referer"), ("ua", "User-Agent"),
                                   ("cookie", "Cookie"), ("user_agent", "User-Agent")):
                    val = p.get(key)
                    if isinstance(val, str) and val and "\r" not in val and "\n" not in val:
                        headers[hname] = val

                try:
                    conns = int(p["connections"]) if p.get("connections") else None
                    limit = int(p["limit"]) if p.get("limit") else None
                except (TypeError, ValueError):
                    return self._json(400, {"ok": False, "error": "connections/limit 必须是整数"})

                r = cfgmod.resolve(url, app.cfg, conns, limit, p.get("proxy") or None)
                name = sanitize_filename(p.get("filename"))
                dest = os.path.join(app.download_dir, name) if name else None
                prefer_hls = bool(p.get("hls")) or url.lower().split("?")[0].endswith(".m3u8")

                if prefer_hls:
                    task = app.start_hls(url, name, headers, p.get("proxy") or None)
                    return self._json(200, {"ok": True, **task})

                if bool(app.cfg.get("dedup_add", True)):
                    dup = app.q.find_active(url, dest=dest, proxy=r["proxy"],
                                            headers=headers)
                    if dup is not None:
                        from .engine import is_mirror_host
                        eff = 1 if is_mirror_host(url) else (
                            dup.get("connections") or r["connections"])
                        return self._json(200, {
                            "ok": True, "id": dup["id"], "url": url,
                            "status": dup.get("status"), "dest": dup.get("dest"),
                            "connections": eff, "deduped": True,
                        })
                try:
                    tid = app.q.add(url, dest=dest, connections=r["connections"],
                                    limit_bps=r["limit_bps"], proxy=r["proxy"],
                                    headers=headers, priority=int(p.get("priority") or 0))
                except Exception as e:                       # noqa: BLE001
                    return self._json(500, {"ok": False, "error": f"入队失败: {e}"})
                app.push_event({"event": "added", "id": tid, "url": url})
                # 镜像站会被引擎强制降为单连接，这里如实回报，避免"显示8连接其实1条"
                from .engine import is_mirror_host
                eff_conns = 1 if is_mirror_host(url) else r["connections"]
                return self._json(200, {
                    "ok": True, "id": tid, "url": url, "status": "pending",
                    "dest": dest,
                    "connections": eff_conns,
                })

            def _handle_cancel(self, tid):
                if app.runner and app.runner.cancel(tid):
                    return self._json(200, {"ok": True, "id": tid, "status": "cancelling"})
                row = app.get_task(tid)
                if row is None:
                    return self._json(404, {"ok": False, "error": "任务不存在"})
                if row["status"] == "pending":
                    app.q.update(tid, status="cancelled", finished_at=time.time())
                    return self._json(200, {"ok": True, "id": tid, "status": "cancelled"})
                return self._json(409, {"ok": False, "error": f"任务已是 {row['status']}"})

        return Handler

    def start(self):
        self._httpd = ThreadingHTTPServer((self.host, self.port), self._make_handler())
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.2}, daemon=True)
        self._thread.start()
        try:
            retain = int(self.cfg.get("retain_days") or 0)
            if retain > 0:
                self.q.purge(("done", "error", "cancelled"), older_than_days=retain)
        except Exception:                                    # noqa: BLE001
            pass
        if self.run_downloads:
            self.runner = Runner(self.q, parallel=self.parallel, on_event=self.push_event,
                                 dest_dir=self.download_dir)
            threading.Thread(target=self.runner.run, kwargs={"idle_exit": False},
                             daemon=True).start()
        return self

    def serve_forever(self):
        if self._httpd is None:
            self.start()
        assert self._httpd is not None
        self._httpd.serve_forever(poll_interval=0.5)

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:                                # noqa: BLE001
                pass
            self._httpd = None
        if self.runner is not None:
            self.runner.stop()

    # ---------- 数据 ----------

    def push_event(self, evt):
        try:
            self.events.append({"ts": round(time.time(), 3), **evt})
        except Exception:                                    # noqa: BLE001
            pass

    def get_task(self, tid):
        row = self.q.get(tid)
        with self._hls_lock:
            if row is None and tid in self.hls_tasks:
                row = dict(self.hls_tasks[tid])
        if row:
            total = row.get("total") or 0
            row = dict(row)
            row["percent"] = round((row.get("downloaded") or 0) / total * 100, 2) if total else 0
        return row

    def status(self, limit=100):
        rows = []
        for t in self.q.list(limit=limit):
            total = t.get("total") or 0
            t = dict(t)
            t["percent"] = round((t.get("downloaded") or 0) / total * 100, 2) if total else 0
            t.pop("headers", None)
            rows.append(t)
        with self._hls_lock:
            for h in self.hls_tasks.values():
                total = h.get("total") or 0
                rows.append({**h,
                             "percent": round((h.get("downloaded") or 0) / total * 100, 2)
                             if total else 0})
        return {"ok": True, "tasks": rows, "stats": self.q.stats(),
                "download_dir": self.download_dir, "version": __version__}

    # ---------- HLS ----------

    def start_hls(self, url, name, headers, proxy):
        from .hls import download_hls

        tid = "hls-" + secrets.token_hex(5)
        dest = os.path.join(self.download_dir, name) if name else None
        rec = {"id": tid, "url": url, "dest": dest, "status": "running",
               "downloaded": 0, "total": 0, "percent": 0, "error": None,
               "added_at": time.time(), "finished_at": None, "kind": "hls"}
        with self._hls_lock:
            self.hls_tasks[tid] = rec

        def worker():
            try:
                res = download_hls(url, dest, headers=headers, proxy=proxy,
                                   progress=lambda d, t: rec.update(
                                       downloaded=d, total=t,
                                       percent=round(d / t * 100, 2) if t else 0))
                rec.update(status="done", dest=res.get("dest"), downloaded=res.get("downloaded") or 0,
                           total=res.get("total") or 0, finished_at=time.time())
                self.push_event({"event": "done", "id": tid, "result": res})
            except Exception as e:                           # noqa: BLE001
                rec.update(status="error", error=str(e), finished_at=time.time())
                self.push_event({"event": "error", "id": tid, "error": str(e)})

        threading.Thread(target=worker, daemon=True).start()
        return rec


# ---------------------------------------------------------------- 启动

def ensure_token(cfg=None, path=None):
    """没有令牌就生成一个并落盘，返回令牌。"""
    cfg = cfg if cfg is not None else cfgmod.load(path)
    tok = cfg.get("server_token") or os.environ.get("AI_IDM_TOKEN") or ""
    if not tok:
        tok = secrets.token_urlsafe(18)
        cfg["server_token"] = tok
        try:
            cfgmod.save(cfg, path)
        except OSError:
            pass
    return tok


def serve(host=None, port=None, token=None, parallel=None, download_dir=None,
          run_downloads=True, quiet=False):
    cfg = cfgmod.load()
    tok = token or ensure_token(cfg)
    srv = TakeoverServer(host=host, port=port, token=tok, parallel=parallel,
                         download_dir=download_dir, run_downloads=run_downloads)
    srv.start()
    base = f"http://{srv.host}:{srv.port}"
    if not quiet:
        print(f"AI-IDM 接管服务 v{__version__} 已启动", flush=True)
        print(f"  地址    : {base}", flush=True)
        print(f"  令牌    : {srv.token}", flush=True)
        print(f"  下载目录: {srv.download_dir}", flush=True)
        print(f"  面板    : {base}/?token={srv.token}", flush=True)
        print(f"  队列    : {'自动下载' if run_downloads else '仅入队（需另跑 ai-idm queue run）'}",
              flush=True)
        print("  扩展    : chrome://extensions → 开发者模式 → 加载已解压的扩展程序 → 选 browser-extension/",
              flush=True)
        print("  停止    : Ctrl+C", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        if not quiet:
            print("\n已停止", flush=True)
    finally:
        srv.stop()
    return srv


# ---------------------------------------------------------------- 面板

DASHBOARD = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI-IDM 接管面板</title>
<style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;padding:24px;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",
 "Microsoft YaHei",sans-serif;background:#f6f7f9;color:#111827}
@media (prefers-color-scheme:dark){body{background:#0f1115;color:#e5e7eb}
 .card{background:#181b21!important;border-color:#272b33!important}
 .row{border-color:#24282f!important} .bar{background:#272b33!important}
 input{background:#11141a!important;color:#e5e7eb!important;border-color:#2d3239!important}}
h1{font-size:18px;margin:0 0 4px}
.sub{color:#6b7280;font-size:12px;margin-bottom:18px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin-bottom:14px}
.row{display:flex;gap:12px;align-items:center;padding:10px 0;border-bottom:1px dashed #e5e7eb}
.row:last-child{border-bottom:0}
.name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar{height:6px;background:#e9ecf1;border-radius:3px;overflow:hidden;width:120px;flex:none}
.bar>i{display:block;height:100%;background:#2563eb;width:0}
.tag{font-size:11px;padding:2px 7px;border-radius:999px;background:#eef2ff;color:#3730a3;flex:none}
.tag.done{background:#e8f7ee;color:#166534}.tag.error{background:#fdecec;color:#991b1b}
.tag.running{background:#e8f1ff;color:#1d4ed8}.tag.cancelled{background:#f3f4f6;color:#4b5563}
.muted{color:#6b7280;font-size:12px;flex:none}
input{flex:1;padding:7px 10px;border:1px solid #d1d5db;border-radius:8px;font:inherit}
button{padding:7px 14px;border:0;border-radius:8px;background:#2563eb;color:#fff;font:inherit;cursor:pointer}
button.ghost{background:transparent;color:#2563eb;border:1px solid #c7d2fe}
code{background:#eef1f5;padding:1px 5px;border-radius:4px;font-size:12px}
</style></head>
<body>
<h1>AI-IDM 接管面板</h1>
<div class="sub" id="sub">连接中…</div>

<div class="card">
  <div style="display:flex;gap:8px">
    <input id="tok" type="password" placeholder="粘贴 ai-idm serve 打印的令牌">
    <button onclick="save()">保存</button>
  </div>
</div>

<div class="card"><div id="tasks" class="muted">暂无任务</div></div>

<script>
const $=(s)=>document.querySelector(s);
const q=new URLSearchParams(location.search);
let token=q.get("token")||localStorage.getItem("aiidm_token")||"";
if(q.get("token"))localStorage.setItem("aiidm_token",q.get("token"));
$("#tok").value=token;
function save(){token=$("#tok").value.trim();localStorage.setItem("aiidm_token",token);tick();}
function fmt(n){if(!n)return "-";const u=["B","KB","MB","GB","TB"];let v=n,i=0;
 while(v>=1024&&i<u.length-1){v/=1024;i++}return (i?v.toFixed(1):v|0)+u[i];}
async function tick(){
  try{
    const r=await fetch("/status",{headers:{"X-AI-IDM-Token":token}});
    if(!r.ok)throw new Error(r.status==401?"令牌无效":"HTTP "+r.status);
    const d=await r.json();
    $("#sub").textContent=`v${d.version} · 下载目录 ${d.download_dir} · 共 ${d.tasks.length} 个任务`;
    if(!d.tasks.length){$("#tasks").textContent="暂无任务";return}
    $("#tasks").innerHTML=d.tasks.slice(0,40).map(t=>{
      const name=(t.dest||t.url||"").split(/[\\\\/]/).pop()||t.url;
      const st=t.status||"";
      return `<div class="row">
        <span class="tag ${st}">${st}</span>
        <span class="name" title="${(t.dest||t.url||"").replace(/"/g,"&quot;")}">${name}</span>
        <span class="muted">${fmt(t.downloaded)} / ${fmt(t.total)}</span>
        <span class="bar"><i style="width:${t.percent||0}%"></i></span>
        <span class="muted">${(t.percent||0).toFixed(1)}%</span>
      </div>`}).join("");
  }catch(e){
    $("#sub").textContent="未连接："+e.message+"（先运行 ai-idm serve）";
    $("#tasks").textContent="—";
  }
}
tick();setInterval(tick,1500);
</script>
</body></html>"""
