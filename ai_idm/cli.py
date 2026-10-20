"""ai_idm.cli — 命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__
from . import config as cfgmod
from .engine import (Download, DownloadError, DEFAULT_CONNECTIONS, MIN_SEG,
                     probe)


def _human(n):
    if n is None:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.1f}{u}" if u != "B" else f"{int(v)}B"
        v /= 1024


def _rate(n):
    return _human(n) + "/s" if n else "-"


def _parse_size(text):
    if not text:
        return None
    t = text.strip().upper().replace("/S", "")
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
    if t and t[-1] in mult:
        return int(float(t[:-1]) * mult[t[-1]])
    return int(float(t))


def _parse_headers(items):
    h = {}
    for item in items or []:
        if ":" not in item:
            raise SystemExit(f"header 格式应为 'Name: Value'，收到 {item!r}")
        k, v = item.split(":", 1)
        h[k.strip()] = v.strip()
    return h


def _bar(done, total, width=28):
    if not total:
        return "?" * width
    filled = int(width * done / total)
    return "#" * filled + "-" * (width - filled)


def cmd_get(args):
    headers = _parse_headers(args.header)
    if args.cookie:
        headers["Cookie"] = args.cookie
    if args.ua:
        headers["User-Agent"] = args.ua
    if args.referer:
        headers["Referer"] = args.referer

    def progress(s):
        if args.quiet:
            return
        line = (f"\r[{_bar(s['downloaded'], s['total'])}] "
                f"{s['percent']:6.2f}%  {_human(s['downloaded'])}/{_human(s['total'])}  "
                f"{_rate(s['speed_bps'])}  ETA {s['eta_sec'] or '-'}s  ")
        sys.stderr.write(line)
        sys.stderr.flush()

    cfg = cfgmod.load()
    for spec in args.site_connections or []:
        if "=" not in spec:
            raise SystemExit(f"--site-connections 需要 host=N，收到 {spec!r}")
        h, n = spec.split("=", 1)
        cfg["site_connections"][h.strip().lower()] = int(n)
    r = cfgmod.resolve(args.url, cfg, args.connections, _parse_size(args.limit),
                       args.proxy, args.timeout, args.retries)

    is_ftp = args.url.lower().startswith(("ftp://", "ftps://"))
    if is_ftp:
        from .ftp import FtpDownload
        dl = FtpDownload(
            args.url,
            dest=args.output,
            connections=r["connections"],
            insecure=args.insecure,
            resume=not args.no_resume,
            progress=progress,
            timeout=r["timeout"],
            max_retries=r["max_retries"],
        )
        try:
            result = dl.run()
        except Exception as e:
            if not args.quiet:
                sys.stderr.write("\n")
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            return 1
        if not args.quiet:
            sys.stderr.write("\n")
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
        return 0

    dl = Download(
        args.url,
        dest=args.output,
        connections=r["connections"],
        min_seg=_parse_size(args.min_seg) or MIN_SEG,
        limit_bps=r["limit_bps"],
        headers=headers,
        proxy=r["proxy"],
        insecure=args.insecure,
        resume=not args.no_resume,
        verify=args.checksum,
        progress=progress,
        timeout=r["timeout"],
        max_retries=r["max_retries"],
        schedule=cfg.get("speed_schedule") or [],
        verify_sample=(args.verify_sample if args.verify_sample is not None
                       else int(cfg.get("verify_sample") or 0)),
        verify_seam=(False if args.no_seam_verify
                     else bool(cfg.get("seam_verify", True))),
        open_range=(True if args.open_range
                    else bool(cfg.get("open_range", False))),
        dynamic_chunks=(False if args.static_chunks
                        else bool(cfg.get("dynamic_chunks", True))),
        keepalive=(False if args.no_keepalive
                   else bool(cfg.get("keepalive", True))),
    )
    try:
        result = dl.run()
    except DownloadError as e:
        if not args.quiet:
            sys.stderr.write("\n")
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    if not args.quiet:
        sys.stderr.write("\n")
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


def cmd_probe(args):
    p = probe(args.url, _parse_headers(args.header), args.proxy, args.insecure)
    print(json.dumps(p.as_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_list(args):
    d = args.dir or "."
    rows = []
    for name in sorted(os.listdir(d)):
        if name.endswith(".idmpart.json"):
            try:
                st = json.load(open(os.path.join(d, name), encoding="utf-8"))
            except Exception:
                continue
            total = sum(s["e"] - s["s"] + 1 for s in st.get("segments", []))
            done = sum(s.get("d", 0) for s in st.get("segments", []))
            rows.append({
                "url": st.get("url"),
                "dest": st.get("dest"),
                "percent": round(done / total * 100, 2) if total else 0,
                "done": done,
                "total": total,
            })
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_hls(args):
    from .hls import download_hls, HlsError
    headers = _parse_headers(args.header)
    if args.cookie:
        headers["Cookie"] = args.cookie
    if args.ua:
        headers["User-Agent"] = args.ua
    if args.referer:
        headers["Referer"] = args.referer

    def progress(done, total):
        if args.quiet:
            return
        sys.stderr.write(f"\r[hls] 分片 {done}/{total} ")
        sys.stderr.flush()

    try:
        r = download_hls(args.url, args.output, connections=args.connections,
                         quality=args.quality, headers=headers, proxy=args.proxy,
                         insecure=args.insecure, keep_segments=args.keep_segments,
                         restart=args.restart, progress=progress)
    except HlsError as e:
        if not args.quiet:
            sys.stderr.write("\n")
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    if not args.quiet:
        sys.stderr.write("\n")
    print(json.dumps({"ok": True, **r}, ensure_ascii=False, indent=2))
    return 0


def cmd_config(args):
    cfg = cfgmod.load()
    if args.action == "show":
        print(json.dumps({**cfg, "path": cfgmod.config_path()}, ensure_ascii=False, indent=2))
        return 0
    if args.action == "set":
        for spec in args.pairs or []:
            if "=" not in spec:
                raise SystemExit(f"需要 k=v，收到 {spec!r}")
            k, v = spec.split("=", 1)
            if k not in cfgmod.DEFAULTS:
                raise SystemExit(f"未知配置项 {k}，可用: {', '.join(cfgmod.DEFAULTS)}")
            if k == "site_connections":
                raise SystemExit("请用 config set-site 设置站点并发")
            try:
                cfg[k] = None if v.lower() in ("none", "null", "") else (
                    int(v) if v.isdigit() else v)
            except ValueError:
                cfg[k] = v
        p = cfgmod.save(cfg)
        print(json.dumps({"ok": True, "path": p, "config": cfg}, ensure_ascii=False, indent=2))
        return 0
    if args.action == "set-site":
        cfg["site_connections"][args.host.lower()] = int(args.number)
        cfgmod.save(cfg)
        print(json.dumps({"ok": True, "site_connections": cfg["site_connections"]},
                         ensure_ascii=False, indent=2))
        return 0
    if args.action == "schedule-show":
        sch = cfg.get("speed_schedule") or []
        print(json.dumps({"schedule": sch,
                          "active_limit_bps": cfgmod.schedule_limit(sch)},
                         ensure_ascii=False, indent=2))
        return 0
    if args.action == "schedule-add":
        rng, _, rate = args.spec.partition("=")
        start, _, end = rng.partition("-")
        if not (start and end and rate):
            raise SystemExit("格式应为 'HH:MM-HH:MM=RATE'，如 09:00-18:00=1M")
        rule = {"start": start.strip(), "end": end.strip(),
                "limit_bps": _parse_size(rate)}
        if args.days:
            dmap = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
            try:
                rule["days"] = [dmap[d.strip().lower()[:3]] for d in args.days.split(",")]
            except KeyError as e:
                raise SystemExit(f"无法识别的星期: {e}")
        cfg.setdefault("speed_schedule", []).append(rule)
        cfgmod.save(cfg)
        print(json.dumps({"ok": True, "rule": rule, "schedule": cfg["speed_schedule"]},
                         ensure_ascii=False, indent=2))
        return 0
    if args.action == "schedule-clear":
        cfg["speed_schedule"] = []
        cfgmod.save(cfg)
        print(json.dumps({"ok": True, "schedule": []}, ensure_ascii=False))
        return 0
    if args.action == "unset-site":
        cfg["site_connections"].pop(args.host.lower(), None)
        cfgmod.save(cfg)
        print(json.dumps({"ok": True, "site_connections": cfg["site_connections"]},
                         ensure_ascii=False, indent=2))
        return 0
    return 1


def cmd_mcp(args):
    from .mcp_server import serve
    serve()
    return 0


# ---------------------------------------------------------------- 队列

def _queue(args):
    from .queue import Queue
    return Queue(getattr(args, "db", None))


def cmd_queue_add(args):
    q = _queue(args)
    headers = _parse_headers(args.header)
    if args.cookie:
        headers["Cookie"] = args.cookie
    if args.ua:
        headers["User-Agent"] = args.ua
    cfg = cfgmod.load()
    for spec in args.site_connections or []:
        if "=" not in spec:
            raise SystemExit(f"--site-connections 需要 host=N，收到 {spec!r}")
        h, n = spec.split("=", 1)
        cfg["site_connections"][h.strip().lower()] = int(n)
    r = cfgmod.resolve(args.url, cfg, args.connections, _parse_size(args.limit), args.proxy)
    tid = q.add(args.url, dest=args.output, connections=r["connections"],
                limit_bps=r["limit_bps"], proxy=r["proxy"],
                headers=headers, priority=args.priority)
    print(json.dumps({"ok": True, "id": tid, "status": "pending"}, ensure_ascii=False))
    return 0


def cmd_queue_list(args):
    q = _queue(args)
    rows = q.list(args.status)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_queue_run(args):
    q = _queue(args)
    from .queue import Runner

    def on_event(evt):
        if args.quiet:
            return
        e = evt.get("event")
        if e == "start":
            sys.stderr.write(f"[queue] 开始 {evt['id']} {evt['url']}\n")
        elif e == "done":
            r = evt.get("result") or {}
            sys.stderr.write(f"[queue] 完成 {evt['id']} {r.get('downloaded')}B "
                             f"{r.get('elapsed_sec')}s\n")
        elif e == "retry":
            sys.stderr.write(f"[queue] 重试 {evt['id']} ({evt.get('attempts')}) {evt.get('error')}\n")
        elif e == "error":
            sys.stderr.write(f"[queue] 失败 {evt['id']} {evt.get('error')}\n")
        sys.stderr.flush()

    stats = Runner(q, parallel=args.parallel, on_event=on_event).run(idle_exit=True)
    print(json.dumps({"ok": True, "stats": stats}, ensure_ascii=False, indent=2))
    return 0


def cmd_queue_remove(args):
    q = _queue(args)
    ok = q.remove(args.id)
    print(json.dumps({"ok": ok, "id": args.id}, ensure_ascii=False))
    return 0 if ok else 1


def cmd_queue_retry(args):
    q = _queue(args)
    ok = q.retry(args.id)
    print(json.dumps({"ok": ok, "id": args.id, "status": "pending"}, ensure_ascii=False))
    return 0 if ok else 1


def build_parser():
    p = argparse.ArgumentParser(
        prog="ai-idm",
        description="分片多连接下载器：CLI + MCP（供 AI 调用）",
    )
    p.add_argument("--version", action="version", version=f"ai-idm {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get", help="下载一个 URL")
    g.add_argument("url")
    g.add_argument("-o", "--output", help="保存路径（默认按 URL/Content-Disposition 推断）")
    g.add_argument("-n", "--connections", type=int, default=None,
                   help=f"并发连接数（默认取配置，内置 {DEFAULT_CONNECTIONS}）")
    g.add_argument("--site-connections", action="append", metavar="HOST=N",
                   help="临时覆盖某站点并发数，可多次")
    g.add_argument("--timeout", type=float, default=None, help="单次请求超时秒数")
    g.add_argument("--retries", type=int, default=None, help="每段最大重试次数")
    g.add_argument("--verify-sample", type=int, default=None, metavar="N",
                   help="完成后抽样回读 N 处校验，不一致则整包重下（0=关）")
    g.add_argument("--no-seam-verify", action="store_true",
                   help="关闭接缝校验（默认开启：相邻块重叠 64 字节比对，与 IDM 一致）")
    g.add_argument("--no-keepalive", action="store_true",
                   help="关闭连接复用（默认复用：同一连接连续取块，对应 IDM 的 Reusing connection.）")
    g.add_argument("--static-chunks", action="store_true",
                   help="每连接一段（旧行为）；默认动态分块：块数≈连接数×4，谁空谁取")
    g.add_argument("--open-range", action="store_true",
                   help="严格模拟 IDM 的开区间写法 bytes=N-（默认用闭区间，只多要 64 字节接缝）")
    g.add_argument("--min-seg", default="1M", help="最小分段大小，低于则单连接（默认 1M）")
    g.add_argument("--limit", help="限速，如 2M / 512K")
    g.add_argument("--proxy", help="HTTP 代理，如 http://127.0.0.1:7890")
    g.add_argument("-H", "--header", action="append", metavar="'K: V'", help="自定义请求头，可多次")
    g.add_argument("--cookie", help="Cookie 请求头")
    g.add_argument("--ua", help="User-Agent")
    g.add_argument("--referer", help="Referer")
    g.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验")
    g.add_argument("--no-resume", action="store_true", help="不读取断点状态，重新下载")
    g.add_argument("--checksum", choices=["md5", "sha256"], help="下载完成后计算校验和")
    g.add_argument("-q", "--quiet", action="store_true")
    g.set_defaults(func=cmd_get)

    pr = sub.add_parser("probe", help="只探测，不下载")
    pr.add_argument("url")
    pr.add_argument("--proxy")
    pr.add_argument("-H", "--header", action="append", metavar="'K: V'")
    pr.add_argument("--insecure", action="store_true")
    pr.set_defaults(func=cmd_probe)

    ls = sub.add_parser("list", help="列出目录中未完成的下载")
    ls.add_argument("--dir", default=".")
    ls.set_defaults(func=cmd_list)

    h = sub.add_parser("hls", help="下载 HLS(m3u8) 并合并为单文件")
    h.add_argument("url")
    h.add_argument("-o", "--output", help="输出文件（默认按清单名推断 .ts）")
    h.add_argument("-n", "--connections", type=int, default=DEFAULT_CONNECTIONS,
                   help=f"分片并发数（默认 {DEFAULT_CONNECTIONS}）")
    h.add_argument("--quality", default="best", help="best / worst / 码率索引（0 起）")
    h.add_argument("--proxy")
    h.add_argument("-H", "--header", action="append", metavar="'K: V'")
    h.add_argument("--cookie")
    h.add_argument("--ua")
    h.add_argument("--referer")
    h.add_argument("--insecure", action="store_true")
    h.add_argument("--keep-segments", action="store_true", help="保留下载的分片目录")
    h.add_argument("--restart", action="store_true", help="丢弃已下分片重新开始")
    h.add_argument("-q", "--quiet", action="store_true")
    h.set_defaults(func=cmd_hls)

    m = sub.add_parser("mcp", help="以 MCP stdio 服务器运行（供 WorkBuddy/Codex/Claude 调用）")
    m.set_defaults(func=cmd_mcp)

    cp = sub.add_parser("config", help="查看/修改配置（默认值、按站点并发）")
    csub = cp.add_subparsers(dest="action", required=True)
    c1 = csub.add_parser("show", help="显示当前配置")
    c1.set_defaults(func=cmd_config, action="show")
    c2 = csub.add_parser("set", help="设置配置项，如 connections=16")
    c2.add_argument("pairs", nargs="+", metavar="k=v")
    c2.set_defaults(func=cmd_config, action="set")
    c3 = csub.add_parser("set-site", help="为某站点固定并发数")
    c3.add_argument("host")
    c3.add_argument("number", type=int)
    c3.set_defaults(func=cmd_config, action="set-site")
    c4 = csub.add_parser("unset-site", help="移除站点覆盖")
    c4.add_argument("host")
    c4.set_defaults(func=cmd_config, action="unset-site")
    c5 = csub.add_parser("schedule", help="分时段限速")
    ssub = c5.add_subparsers(dest="sched_action", required=True)
    s1 = ssub.add_parser("show", help="显示规则与当前生效值")
    s1.set_defaults(func=cmd_config, action="schedule-show")
    s2 = ssub.add_parser("add", help="新增规则，如 09:00-18:00=1M")
    s2.add_argument("spec")
    s2.add_argument("--days", help="限定星期，如 mon,tue,fri（省略=每天）")
    s2.set_defaults(func=cmd_config, action="schedule-add")
    s3 = ssub.add_parser("clear", help="清空所有规则")
    s3.set_defaults(func=cmd_config, action="schedule-clear")

    # ---- queue ----
    qp = sub.add_parser("queue", help="任务队列（SQLite 持久化，可跨进程续传）")
    qsub = qp.add_subparsers(dest="qcmd", required=True)

    qa = qsub.add_parser("add", help="加一个下载任务")
    qa.add_argument("url")
    qa.add_argument("-o", "--output")
    qa.add_argument("-n", "--connections", type=int, default=None)
    qa.add_argument("--site-connections", action="append", metavar="HOST=N")
    qa.add_argument("--limit")
    qa.add_argument("--proxy", help="http://… | socks5://… | socks5h://… | auto")
    qa.add_argument("-H", "--header", action="append", metavar="'K: V'")
    qa.add_argument("--cookie")
    qa.add_argument("--ua")
    qa.add_argument("--priority", type=int, default=0)
    qa.add_argument("--db")
    qa.set_defaults(func=cmd_queue_add)

    ql = qsub.add_parser("list", help="列出队列")
    ql.add_argument("--status", choices=["pending", "running", "done", "error", "cancelled"])
    ql.add_argument("--db")
    ql.set_defaults(func=cmd_queue_list)

    qr = qsub.add_parser("run", help="消费队列直到清空")
    qr.add_argument("-p", "--parallel", type=int, default=2, help="并行任务数（默认 2）")
    qr.add_argument("-q", "--quiet", action="store_true")
    qr.add_argument("--db")
    qr.set_defaults(func=cmd_queue_run)

    qrm = qsub.add_parser("remove", help="删除任务")
    qrm.add_argument("id")
    qrm.add_argument("--db")
    qrm.set_defaults(func=cmd_queue_remove)

    qrt = qsub.add_parser("retry", help="重新排队一个失败任务")
    qrt.add_argument("id")
    qrt.add_argument("--db")
    qrt.set_defaults(func=cmd_queue_retry)
    return p


def main(argv=None):
    from .stdio import ensure_utf8_stdio
    ensure_utf8_stdio()          # Windows 非 UTF-8 代码页下中文帮助会崩
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
