"""idmclone.mcp_server — MCP stdio 服务器（换行分隔 JSON-RPC）。

用途：让 AI 客户端（WorkBuddy / Codex / Claude Code 等）直接调用下载引擎。
传输：stdio，消息为单行 JSON（MCP 规范）。
"""

from __future__ import annotations

import json
import sys
import threading
import time

from . import __version__
from .engine import Download, DownloadError, probe

PROTOCOL_VERSION = "2025-06-18"

_registry = {}          # id -> Download
_reg_lock = threading.Lock()


# ---------------------------------------------------------------- 工具实现

def _json(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)


def t_probe_url(args):
    p = probe(args["url"], args.get("headers"), args.get("proxy"),
              bool(args.get("insecure")))
    return _json(p.as_dict())


def t_start_download(args):
    from . import config as cfgmod
    headers = dict(args.get("headers") or {})
    cfg = cfgmod.load()
    r = cfgmod.resolve(args["url"], cfg, args.get("connections"),
                       args.get("limit_bps"), args.get("proxy"))
    dl = Download(
        args["url"],
        dest=args.get("output"),
        connections=r["connections"],
        limit_bps=r["limit_bps"],
        headers=headers,
        proxy=r["proxy"],
        insecure=bool(args.get("insecure")),
        resume=bool(args.get("resume", True)),
        verify=args.get("checksum"),
        verify_sample=int(args.get("verify_sample") or 0),
        verify_seam=bool(args.get("verify_seam", cfg.get("seam_verify", True))),
        open_range=bool(args.get("open_range", cfg.get("open_range", False))),
        dynamic_chunks=bool(args.get("dynamic_chunks", cfg.get("dynamic_chunks", True))),
        keepalive=bool(args.get("keepalive", cfg.get("keepalive", True))),
        progress=None,
    )
    with _reg_lock:
        _registry[dl.id] = dl

    def runner():
        try:
            dl.run()
        except DownloadError:
            pass
        except Exception as e:                      # noqa: BLE001
            dl.status = "error"
            dl.error = f"{type(e).__name__}: {e}"
            dl._emit()

    threading.Thread(target=runner, daemon=True).start()
    return _json({"ok": True, "id": dl.id, "dest": dl.dest,
                  "status": "running", "note": "用 download_status 查询进度"})


def t_download_status(args):
    with _reg_lock:
        if args.get("id"):
            dl = _registry.get(args["id"])
            if not dl:
                return _json({"ok": False, "error": f"未知 id: {args['id']}"})
            return _json(dl.snapshot())
        return _json([d.snapshot() for d in _registry.values()])


def t_wait_download(args):
    tid = args["id"]
    timeout = float(args.get("timeout_sec") or 300)
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _reg_lock:
            dl = _registry.get(tid)
        if not dl:
            return _json({"ok": False, "error": f"未知 id: {tid}"})
        if dl.status in ("done", "error", "cancelled"):
            return _json(dl.snapshot())
        time.sleep(0.5)
    with _reg_lock:
        dl = _registry.get(tid)
    return _json({"ok": False, "error": "timeout", "snapshot": dl.snapshot() if dl else None})


def t_cancel_download(args):
    with _reg_lock:
        dl = _registry.get(args["id"])
    if not dl:
        return _json({"ok": False, "error": f"未知 id: {args['id']}"})
    dl.cancel()
    return _json({"ok": True, "id": dl.id, "status": "cancelling"})


def t_list_downloads(args):
    with _reg_lock:
        return _json([d.snapshot() for d in _registry.values()])


def t_queue_add(args):
    from . import config as cfgmod
    from .queue import Queue
    q = Queue(args.get("db"))
    r = cfgmod.resolve(args["url"], cfgmod.load(), args.get("connections"),
                       args.get("limit_bps"), args.get("proxy"))
    tid = q.add(args["url"], dest=args.get("output"),
                connections=r["connections"],
                limit_bps=r["limit_bps"],
                proxy=r["proxy"],
                headers=args.get("headers") or {},
                priority=int(args.get("priority") or 0))
    return _json({"ok": True, "id": tid, "status": "pending",
                  "note": "用 queue_run 开始消费，或 queue_list 查看"})


def t_queue_list(args):
    from .queue import Queue
    q = Queue(args.get("db"))
    return _json({"stats": q.stats(), "tasks": q.list(args.get("status"))})


def t_queue_run(args):
    from .queue import Queue, Runner
    q = Queue(args.get("db"))
    runner = Runner(q, parallel=int(args.get("parallel") or 2))
    stats = runner.run(idle_exit=True)
    return _json({"ok": True, "stats": stats, "tasks": q.list()})


def t_queue_remove(args):
    from .queue import Queue
    return _json({"ok": Queue(args.get("db")).remove(args["id"])})


def t_queue_retry(args):
    from .queue import Queue
    return _json({"ok": Queue(args.get("db")).retry(args["id"])})


def t_download_hls(args):
    from .hls import download_hls
    r = download_hls(
        args["url"], args.get("output"),
        connections=int(args.get("connections") or 8),
        quality=str(args.get("quality") or "best"),
        headers=args.get("headers") or {},
        proxy=args.get("proxy"),
        insecure=bool(args.get("insecure")),
        keep_segments=bool(args.get("keep_segments")),
        restart=bool(args.get("restart")),
    )
    return _json({"ok": True, **r})


TOOLS = [
    {
        "name": "probe_url",
        "description": "探测 URL：返回文件大小、是否支持 Range（分片）、ETag、文件名、Content-Type。不下载。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标 URL，支持 http/https"},
                "headers": {"type": "object", "description": "附加请求头，如 Cookie/Referer"},
                "proxy": {"type": "string", "description": "HTTP 代理，如 http://127.0.0.1:7890"},
                "insecure": {"type": "boolean", "description": "跳过 TLS 证书校验"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "start_download",
        "description": "启动一个分片多连接下载任务（异步）。返回任务 id，用 download_status 查看进度。支持断点续传。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "output": {"type": "string", "description": "保存路径（绝对或相对）。省略则按 URL 推断"},
                "connections": {"type": "integer", "description": "并发连接数，默认 8"},
                "limit_bps": {"type": "integer", "description": "限速（字节/秒），省略为不限"},
                "headers": {"type": "object"},
                "proxy": {"type": "string"},
                "insecure": {"type": "boolean"},
                "resume": {"type": "boolean", "description": "是否续传，默认 true"},
                "checksum": {"type": "string", "enum": ["md5", "sha256"],
                             "description": "下载完成后计算校验和"},
                "verify_sample": {"type": "integer",
                                  "description": "完成后抽样回读次数，不一致则整包重下（0=关）"},
                "verify_seam": {"type": "boolean",
                                "description": "接缝校验：相邻块重叠 64 字节逐字节比对，默认 true"},
                "open_range": {"type": "boolean",
                               "description": "用开区间 Range bytes=N-（严格模拟 IDM，默认 false：闭区间只多要 64 字节）"},
                "dynamic_chunks": {"type": "boolean",
                                   "description": "动态分块（默认 true）：块数≈连接数×4，由工作队列运行时分配"},
                "keepalive": {"type": "boolean",
                              "description": "连接复用（默认 true）：同一连接连续取块，减少握手"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "download_status",
        "description": "查询下载进度。给定 id 查单个，不给则返回全部任务。",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
        },
    },
    {
        "name": "wait_download",
        "description": "阻塞等待任务结束（或超时），返回最终状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "timeout_sec": {"type": "number", "description": "默认 300 秒"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "cancel_download",
        "description": "取消一个下载任务（保留断点状态，可再次续传）。",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "list_downloads",
        "description": "列出当前会话内的所有下载任务及其状态。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "queue_add",
        "description": "把下载加入持久化队列（SQLite），进程退出后仍保留，可断点续传。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "output": {"type": "string"},
                "connections": {"type": "integer"},
                "limit_bps": {"type": "integer"},
                "proxy": {"type": "string", "description": "http://… | socks5://… | socks5h://… | auto"},
                "headers": {"type": "object"},
                "priority": {"type": "integer"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "queue_run",
        "description": "消费队列直到清空（并行下载），返回各任务最终状态。",
        "inputSchema": {
            "type": "object",
            "properties": {"parallel": {"type": "integer", "description": "并行任务数，默认 2"}},
        },
    },
    {
        "name": "queue_list",
        "description": "查看队列：按状态统计 + 任务明细。",
        "inputSchema": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
        },
    },
    {
        "name": "queue_remove",
        "description": "从队列删除一个任务。",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "queue_retry",
        "description": "把失败任务重新置为 pending。",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "download_hls",
        "description": "下载 HLS(m3u8) 并合并为单文件。支持 master 选码率、AES-128 加密分片、断点续传。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "m3u8 地址（master 或媒体列表）"},
                "output": {"type": "string"},
                "connections": {"type": "integer", "description": "分片并发数，默认 8"},
                "quality": {"type": "string", "description": "best / worst / 码率索引"},
                "headers": {"type": "object"},
                "proxy": {"type": "string"},
                "insecure": {"type": "boolean"},
                "keep_segments": {"type": "boolean"},
                "restart": {"type": "boolean"},
            },
            "required": ["url"],
        },
    },
]

HANDLERS = {
    "probe_url": t_probe_url,
    "start_download": t_start_download,
    "download_status": t_download_status,
    "wait_download": t_wait_download,
    "cancel_download": t_cancel_download,
    "list_downloads": t_list_downloads,
    "download_hls": t_download_hls,
    "queue_add": t_queue_add,
    "queue_run": t_queue_run,
    "queue_list": t_queue_list,
    "queue_remove": t_queue_remove,
    "queue_retry": t_queue_retry,
}


# ---------------------------------------------------------------- 协议层

def _respond(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle(msg):
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        _respond(mid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "idmclone", "version": __version__},
        })
        return
    if method in ("notifications/initialized", "initialized"):
        return
    if method == "ping":
        _respond(mid, {})
        return
    if method == "tools/list":
        _respond(mid, {"tools": TOOLS})
        return
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            _respond(mid, error={"code": -32602, "message": f"未知工具: {name}"})
            return
        try:
            text = fn(args)
            _respond(mid, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as e:                      # noqa: BLE001
            _respond(mid, {"content": [{"type": "text",
                                        "text": f"{type(e).__name__}: {e}"}],
                           "isError": True})
        return
    if method in ("resources/list", "prompts/list"):
        _respond(mid, {"resources": []} if method.startswith("resources") else {"prompts": []})
        return
    if mid is not None:
        _respond(mid, error={"code": -32601, "message": f"不支持的方法: {method}"})


def serve():
    from .stdio import ensure_utf8_stdio
    ensure_utf8_stdio()          # JSON 里有中文，非 UTF-8 控制台会崩
    """从 stdin 读取换行分隔的 JSON-RPC，处理到 EOF。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(msg)
        except Exception as e:                      # noqa: BLE001
            sys.stderr.write(f"[idmclone] handler error: {e}\n")
