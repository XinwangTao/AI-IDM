"""ai_idm.batch — 批量小文件下载。

设计参考（公开设计，未抓取）：
- aria2：小文件不切段，并发放在文件之间；
- wget2：连接复用是小文件吞吐的最大杠杆；
- rclone：清单化、失败隔离、可续跑。

做法：一个共享 keep-alive 连接池 + 全局/每站并发上限 + 单请求完成探测与下载。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlsplit

from .engine import Download
from .pool import Pool

MAX_URLS = 100_000


def read_urls(source):
    """读取 URL 清单：一行一个，# 开头为注释，保持首次出现顺序并去重。"""
    if source in ("-", "", None):
        lines = sys.stdin.read().splitlines()
    else:
        with open(source, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    seen, out = set(), []
    for line in lines:
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= MAX_URLS:
            break
    return out


def _safe_segment(name):
    name = name.replace("\\", "_").replace("/", "_").strip()
    if name in ("", ".", ".."):
        return "_"
    return name


def dest_for(url, out_dir, dirs=True):
    """URL → 本地路径。dirs=True 保留 <host>/<path>，避免不同站同名文件互撞。"""
    parts = urlsplit(url)
    host = _safe_segment((parts.hostname or "unknown").replace(":", "_"))
    path = unquote(parts.path or "/")
    path = path.lstrip("/")
    if not path or path.endswith("/"):
        path += "index.bin"
    segments = [_safe_segment(s) for s in path.split("/") if s]
    name = segments[-1] if segments else "index.bin"
    if parts.query:
        stem, ext = os.path.splitext(name)
        name = f"{stem}_{hashlib.md5(parts.query.encode()).hexdigest()[:6]}{ext}"
        segments[-1] = name
    if dirs:
        return os.path.join(out_dir, host, *segments)
    return os.path.join(out_dir, name)


def _atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def run_batch(urls, out_dir, concurrency=16, per_host=6, resume=False,
              dirs=True, overwrite=False, proxy=None, insecure=False,
              timeout=30, max_retries=4, progress=None, stop_event=None,
              write_manifest=True, quiet=False):
    """并发下载清单里的 URL，返回汇总 dict。

    失败不影响其他文件；结束时写 batch.json 与 failed.txt，--resume 只补缺失/失败。
    """
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    total = len(urls)
    results = []
    lock = threading.Lock()
    stop_event = stop_event or threading.Event()
    host_sems = {}
    host_lock = threading.Lock()
    pool = Pool(proxy, insecure, timeout)
    started = time.time()
    bytes_done = [0]

    def host_sem(host):
        with host_lock:
            sem = host_sems.get(host)
            if sem is None:
                sem = threading.BoundedSemaphore(max(1, per_host))
                host_sems[host] = sem
            return sem

    def one(url):
        dest = dest_for(url, out_dir, dirs)
        if resume and not os.path.exists(dest + ".idmpart.json"):
            try:
                if os.path.getsize(dest) > 0:
                    return {"url": url, "dest": dest, "status": "skipped",
                            "bytes": os.path.getsize(dest)}
            except OSError:
                pass
        sem = host_sem(urlsplit(url).hostname or "")
        sem.acquire()
        try:
            if stop_event.is_set():
                return {"url": url, "dest": dest, "status": "cancelled", "error": "已停止"}
            if url.lower().split("?")[0].endswith(".m3u8"):
                from .hls import download_hls
                res = download_hls(url, dest, connections=concurrency, proxy=proxy,
                                   insecure=insecure, unique=not overwrite)
                return {"url": url, "dest": res.get("dest", dest), "status": "done",
                        "bytes": res.get("size") or 0}
            dl = Download(url, dest=dest, connections=1, timeout=timeout,
                          max_retries=max_retries, proxy=proxy, insecure=insecure,
                          unique=not overwrite, pool=pool, single_request=True)
            r = dl.run()
            return {"url": url, "dest": r.get("dest") or dest, "status": "done",
                    "bytes": r.get("downloaded") or 0}
        except Exception as e:                                 # noqa: BLE001
            return {"url": url, "dest": dest, "status": "failed", "error": str(e)}
        finally:
            sem.release()

    def wrapped(url):
        res = one(url)
        with lock:
            results.append(res)
            if res["status"] == "done":
                bytes_done[0] += res.get("bytes") or 0
            done = len(results)
        if progress:
            try:
                progress(done, total, bytes_done[0], res)
            except Exception:                                  # noqa: BLE001
                pass
        return res

    try:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
            list(ex.map(wrapped, urls))
    finally:
        pool.close()

    elapsed = max(0.001, time.time() - started)
    failed = [r for r in results if r["status"] == "failed"]
    summary = {
        "ok": not failed,
        "out_dir": out_dir,
        "total": total,
        "done": sum(1 for r in results if r["status"] == "done"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "failed": len(failed),
        "cancelled": sum(1 for r in results if r["status"] == "cancelled"),
        "bytes": bytes_done[0],
        "elapsed_sec": round(elapsed, 2),
        "files_per_sec": round((total - len(failed)) / elapsed, 1),
    }
    if write_manifest:
        try:
            _atomic_json(os.path.join(out_dir, "batch.json"),
                         {"generated_at": time.time(), "summary": summary,
                          "items": results})
            failed_path = os.path.join(out_dir, "failed.txt")
            tmp = failed_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("".join(r["url"] + "\n" for r in failed))
            os.replace(tmp, failed_path)
        except OSError:
            pass
    return summary
