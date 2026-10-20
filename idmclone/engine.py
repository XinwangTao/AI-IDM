"""idmclone.engine — 分片多连接下载引擎（stdlib only）。

设计要点：
- 先探测（HEAD，必要时回退 GET Range: 0-0），拿到总长 / ETag / Last-Modified / 是否支持 Range
- 按 Range 切成 N 段并发下载；首段不带 Range、其余用开区间 `bytes=N-`（与 IDM 动态分析一致）
- 接缝校验：每段多读 SEAM=64 字节（只比对不写盘），与相邻段已落盘的数据逐字节比对
- 断点续传：旁路 state 文件记录每段进度，恢复前校验 ETag/大小/URL
- 分片失败/未开始即被移交：回池并延时重取（对应 IDM 的 chunk reassigned）
- 服务端忽略 Range（返回 200）时自动降级为单连接整取
- 令牌桶限速，跨线程共享
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request

CHUNK = 64 * 1024
MIN_SEG = 1 << 20           # 小于 2MB 的文件不分片
SEAM = 64                   # 接缝重叠字节数（对应 IDM 日志里的 posInCmpBuff3=64）
CHUNK_FACTOR = 4            # 动态分块：块数 ≈ 连接数 × 4（块越小，尾延迟越低）
MAX_CHUNKS = 256            # 块数上限，避免状态文件与线程调度开销失控
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) idmclone/1.0"
DEFAULT_CONNECTIONS = 8


class DownloadError(RuntimeError):
    pass


class _UrllibStream:
    """把 urllib 的响应包装成和 pool.Stream 一致的最小接口。"""

    __slots__ = ("_resp", "status", "headers")

    def __init__(self, resp):
        self._resp = resp
        self.status = resp.status
        self.headers = resp.headers

    def read(self, n=-1):
        return self._resp.read(n)

    def geturl(self):
        return self._resp.geturl()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._resp.close()
        except Exception:
            pass
        return False


# ---------------------------------------------------------------- 探测

class Probe:
    def __init__(self, url, final_url, total, ranges, etag, last_modified,
                 filename, content_type, status):
        self.url = url
        self.final_url = final_url
        self.total = total
        self.ranges = ranges
        self.etag = etag
        self.last_modified = last_modified
        self.filename = filename
        self.content_type = content_type
        self.status = status

    def as_dict(self):
        return {
            "url": self.url,
            "final_url": self.final_url,
            "total": self.total,
            "accept_ranges": self.ranges,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "filename": self.filename,
            "content_type": self.content_type,
            "status": self.status,
        }


def _make_opener(proxy=None, insecure=False):
    """代理统一走 proxy.build_opener：支持 http / socks5 / socks5h / auto。"""
    from .proxy import build_opener
    return build_opener(proxy, insecure)[0]


def _parse_content_range(value):
    """'bytes 0-99/1234' -> (0, 99, 1234)"""
    if not value:
        return None
    m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", value.strip(), re.I)
    if not m:
        return None
    total = None if m.group(3) == "*" else int(m.group(3))
    return int(m.group(1)), int(m.group(2)), total


def _filename_from_headers(headers, url):
    cd = headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", cd, re.I)
    if m:
        from urllib.parse import unquote
        name = unquote(m.group(1).strip().strip('"'))
        if name:
            return os.path.basename(name)
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    if m:
        name = os.path.basename(m.group(1).strip())
        if name:
            return name
    from urllib.parse import urlparse, unquote
    path = urlparse(url).path
    name = os.path.basename(unquote(path))
    return name or "download.bin"


def probe(url, headers=None, proxy=None, insecure=False, timeout=30):
    hdrs = {"User-Agent": DEFAULT_UA}
    if headers:
        hdrs.update(headers)
    opener = _make_opener(proxy, insecure)

    def do(method, extra=None):
        h = dict(hdrs)
        if extra:
            h.update(extra)
        req = urllib.request.Request(url, headers=h, method=method)
        return opener.open(req, timeout=timeout)

    resp = None
    try:
        resp = do("HEAD")
    except urllib.error.HTTPError as e:
        if e.code not in (403, 405, 501):
            raise DownloadError(f"HEAD 失败: HTTP {e.code}") from e
    except Exception:
        pass

    total = None
    ranges = False
    etag = last_modified = ctype = None
    final_url = url
    status = 200
    if resp is not None:
        total = int(resp.headers.get("Content-Length") or 0) or None
        ranges = "bytes" in (resp.headers.get("Accept-Ranges") or "").lower()
        etag = resp.headers.get("ETag")
        last_modified = resp.headers.get("Last-Modified")
        ctype = resp.headers.get("Content-Type")
        final_url = resp.geturl()
        status = resp.status
        resp.close()

    if total is None or not ranges:
        # 用 Range: bytes=0-0 再确认一次（有的服务器不响应 HEAD）
        try:
            r2 = do("GET", {"Range": "bytes=0-0"})
            cr = _parse_content_range(r2.headers.get("Content-Range"))
            if r2.status == 206 and cr:
                ranges = True
                if cr[2]:
                    total = cr[2]
            elif total is None:
                total = int(r2.headers.get("Content-Length") or 0) or None
            etag = etag or r2.headers.get("ETag")
            last_modified = last_modified or r2.headers.get("Last-Modified")
            ctype = ctype or r2.headers.get("Content-Type")
            final_url = r2.geturl()
            status = r2.status
            r2.close()
        except urllib.error.HTTPError as e:
            if total is None:
                raise DownloadError(f"无法探测文件大小: HTTP {e.code}") from e
        except Exception as e:
            if total is None:
                raise DownloadError(f"无法探测: {e}") from e

    return Probe(url, final_url, total, ranges, etag, last_modified,
                 _filename_from_headers({} if resp is None else resp.headers, final_url),
                 ctype, status)


# ---------------------------------------------------------------- 限速

class RateLimiter:
    """共享令牌桶，单位 bytes/sec。"""

    def __init__(self, bps=None):
        self.bps = float(bps) if bps else None
        self._lock = threading.Lock()
        self._allowance = 0.0
        self._last = time.monotonic()

    def set_bps(self, bps):
        """运行中调整限速（分时段限速用）。"""
        with self._lock:
            self.bps = float(bps) if bps else None
            if self.bps is not None:
                self._allowance = min(self._allowance, self.bps)

    def consume(self, n):
        if not self.bps:
            return
        remaining = float(n)
        while remaining > 0:
            with self._lock:
                now = time.monotonic()
                self._allowance += (now - self._last) * self.bps
                self._last = now
                if self._allowance > self.bps:
                    self._allowance = self.bps
                take = min(remaining, self._allowance)
                self._allowance -= take
                remaining -= take
                wait = 0.0 if remaining <= 0 else min(remaining / self.bps, 0.2)
            if wait:
                time.sleep(wait)


# ---------------------------------------------------------------- 分段

class Segment:
    __slots__ = ("index", "start", "end", "done", "claimed", "fails",
                 "retry_at", "tail", "seam_ok")

    def __init__(self, index, start, end, done=0):
        self.index = index
        self.start = start      # 绝对起始偏移（含）
        self.end = end          # 绝对结束偏移（含）
        self.done = done        # 已写入字节数
        self.claimed = False    # 已被某个线程认领
        self.fails = 0          # 被放弃/重分配的次数
        self.retry_at = 0.0     # 移交后允许再次认领的时刻（monotonic）
        self.tail = b""         # 接缝多读的 64 字节（只用于比对，不写盘）
        self.seam_ok = None     # True/False/None（None=尚未比对）

    @property
    def size(self):
        return self.end - self.start + 1

    @property
    def remaining(self):
        return self.size - self.done

    def as_dict(self):
        return {"i": self.index, "s": self.start, "e": self.end, "d": self.done}


def plan_segments(total, connections, min_seg=MIN_SEG, dynamic=True):
    """规划下载分块。

    dynamic=True（默认，对齐 IDM 的动态分块）：切出比连接数更多的块
    （约 connections×4，受 min_seg 与 256 上限约束），块与连接的归属由
    工作队列在运行时决定，谁先空出来谁取下一块 —— 而不是开跑前把文件
    平均分给固定几条连接。这样某条连接变慢时，其余连接会自然吃掉它
    剩下的块，不会出现"最后 1/8 数据卡住整单"的尾延迟。

    dynamic=False：旧行为，每个连接一段。
    """
    if not total or total <= 0:
        return [Segment(0, 0, 0)]
    if not connections or connections <= 1 or total < min_seg * 2:
        return [Segment(0, 0, total - 1)]

    if not dynamic:
        n = min(connections, max(1, total // min_seg))
        base = total // n
        segs = []
        start = 0
        for i in range(n):
            end = total - 1 if i == n - 1 else start + base - 1
            segs.append(Segment(i, start, end))
            start = end + 1
        return segs

    target = min(MAX_CHUNKS, max(connections, connections * CHUNK_FACTOR))
    size = max(min_seg, total // target)
    n = min(MAX_CHUNKS, max(1, (total + size - 1) // size))
    size = (total + n - 1) // n          # 均分，最后一块吃掉余数
    segs = []
    start = 0
    for i in range(n):
        end = total - 1 if i == n - 1 else start + size - 1
        segs.append(Segment(i, start, end))
        start = end + 1
    return segs


# ---------------------------------------------------------------- 下载

class Download:
    def __init__(self, url, dest=None, connections=DEFAULT_CONNECTIONS,
                 min_seg=MIN_SEG, limit_bps=None, headers=None, proxy=None,
                 insecure=False, resume=True, verify=None, progress=None,
                 timeout=30, max_retries=4, state_dir=None, schedule=None,
                 verify_sample=0, verify_seam=None, open_range=None,
                 dynamic_chunks=None, keepalive=None):
        self.url = url
        self.connections = connections
        self.min_seg = min_seg
        self.limit_bps = limit_bps
        self.headers = dict(headers or {})
        self.proxy = proxy
        self.insecure = insecure
        self.resume = resume
        self.verify = verify            # "md5" | "sha256" | None
        self.progress_cb = progress
        self.timeout = timeout
        self.max_retries = max_retries
        self.state_dir = state_dir
        self.schedule = schedule or []
        self.verify_sample = int(verify_sample or 0)
        # 接缝校验默认开：IDM 的实际机制是相邻块保留 64 字节重叠逐字节比对
        self.verify_seam = True if verify_seam is None else bool(verify_seam)
        # Range 默认用闭区间 bytes=N-(M+63)：只多要 64 字节，不浪费带宽。
        # 置 True 则完全照 IDM 的 `bytes=N-` 写法，代价是服务器会继续推剩余整段。
        self.open_range = False if open_range is None else bool(open_range)
        # 动态分块：块数 ≈ 连接数×4，归属由工作队列运行时决定（对齐 IDM）
        self.dynamic_chunks = True if dynamic_chunks is None else bool(dynamic_chunks)
        # 连接复用：同一 worker 连续取块时复用同一条 TCP（IDM 的 Reusing connection.）
        self.keepalive = True if keepalive is None else bool(keepalive)
        self._pool = None
        self._fetch_url = None
        self.mismatch_detected = False

        self.id = hashlib.md5(f"{url}-{time.time()}".encode()).hexdigest()[:12]
        self.dest = dest
        self.probe_info = None
        self.segments = []
        self.total = 0
        self.done = 0
        self.status = "pending"         # pending|running|done|error|cancelled
        self.error = None
        self.speed = 0.0
        self.started_at = None
        self.finished_at = None

        self._stop = threading.Event()
        self._user_cancelled = False
        self._lock = threading.Lock()
        self._fh = None
        self._io_lock = threading.Lock()
        self._state_path = None
        self._temp_path = None
        self._limiter = RateLimiter(limit_bps)
        self._fallback = False
        self.reassigned = 0     # 分片被移交给其他连接的次数（总数）
        self.handoffs = 0       # 其中"还没开始下"就被移交的次数（IDM: downloaded=0）
        self.seams_checked = 0
        self.seams_mismatched = 0
        self._last_save = 0.0

    # ---------------- 状态文件 ----------------

    def _state_file(self):
        return self.dest + ".idmpart.json"

    def _save_state(self, force=False):
        if not self._state_path:
            return
        now = time.monotonic()
        if not force and now - self._last_save < 1.0:
            return
        self._last_save = now
        with self._lock:
            data = {
                "url": self.url,
                "dest": self.dest,
                "total": self.total,
                "etag": self.probe_info.etag if self.probe_info else None,
                "last_modified": self.probe_info.last_modified if self.probe_info else None,
                "segments": [s.as_dict() for s in self.segments],
                "saved_at": time.time(),
            }
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._state_path)
        except OSError:
            pass

    def _load_state(self):
        if not (self.resume and self._state_path and os.path.exists(self._state_path)):
            return False
        try:
            with open(self._state_path, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            return False
        p = self.probe_info
        if st.get("url") != self.url:
            return False
        if p and p.total and st.get("total") and st["total"] != p.total:
            return False
        if p and p.etag and st.get("etag") and st["etag"] != p.etag:
            return False
        if p and p.last_modified and st.get("last_modified") and st["last_modified"] != p.last_modified:
            return False
        self.total = st.get("total") or self.total
        self.segments = [Segment(s["i"], s["s"], s["e"], s.get("d", 0)) for s in st["segments"]]
        return True

    # ---------------- 进度 ----------------

    def snapshot(self):
        pct = (self.done / self.total * 100) if self.total else 0.0
        eta = None
        if self.speed and self.total and self.done < self.total:
            eta = (self.total - self.done) / self.speed
        return {
            "id": self.id,
            "url": self.url,
            "dest": self.dest,
            "status": self.status,
            "error": self.error,
            "total": self.total,
            "downloaded": self.done,
            "percent": round(pct, 2),
            "speed_bps": round(self.speed, 1),
            "eta_sec": round(eta, 1) if eta is not None else None,
            "connections": sum(1 for s in self.segments if s.remaining > 0) if self.status == "running" else 0,
            "segments": len(self.segments),
            "reassigned": self.reassigned,
            "handoffs": self.handoffs,
            "seams_checked": self.seams_checked,
            "seams_mismatched": self.seams_mismatched,
            "mismatch_detected": self.mismatch_detected,
            "pool": self._pool.stats if self._pool is not None else None,
        }

    def _emit(self):
        if self.progress_cb:
            try:
                self.progress_cb(self.snapshot())
            except Exception:
                pass

    def cancel(self):
        self._user_cancelled = True
        self._stop.set()

    def _write_at(self, pos, data):
        """跨平台定位写入（Windows 无 os.pwrite）。"""
        with self._io_lock:
            self._fh.seek(pos)
            self._fh.write(data)

    # ---------------- 单段拉取 ----------------

    def _seam_len(self, seg):
        """本段需要多读多少字节用于接缝比对（0 = 不比）。

        与 IDM 一致：除最后一块外，每块向后再取 64 字节，读到的内容不写盘，
        只用来和相邻块已落盘的数据逐字节比对（日志 posInCmpBuff3=64）。
        """
        if not self.verify_seam or not self.total or len(self.segments) < 2:
            return 0
        if seg.end >= self.total - 1:
            return 0                      # 最后一块没有后继，posInCmpBuff3=0
        return min(SEAM, self.total - 1 - seg.end)

    def _add_if_range(self, h):
        p = self.probe_info
        if not p:
            return
        if p.etag:
            h["If-Range"] = p.etag
        elif p.last_modified:
            h["If-Range"] = p.last_modified

    def _request_headers(self, seg):
        h = {"User-Agent": DEFAULT_UA}
        h.update(self.headers)
        start = seg.start + seg.done
        # 单段整取（覆盖整个文件）时不发 Range：兼容不支持 Range 的服务器，也少一次往返
        full_file_single = (start == 0 and self.total
                            and seg.end == self.total - 1 and len(self.segments) == 1)
        if full_file_single:
            return h

        if self.open_range:
            # 严格模拟 IDM 的写法：首段完全不带 Range（对应 C0），其余用开区间 bytes=N-
            # 注意：这样服务器会把剩余整段都推过来，客户端够数即断，属额外开销
            if start > 0:
                h["Range"] = f"bytes={start}-"
                self._add_if_range(h)
        else:
            end = seg.end + self._seam_len(seg)
            if self.total:
                end = min(end, self.total - 1)
            h["Range"] = f"bytes={start}-{end}"
            self._add_if_range(h)
        return h

    def _open_stream(self, headers):
        """统一取流：keepalive 走连接池，否则走 urllib（每次新建连接）。"""
        if self._pool is not None:
            return self._pool.stream(self._fetch_url or self.url, headers)
        req = urllib.request.Request(self._fetch_url or self.url,
                                     headers=headers, method="GET")
        return _UrllibStream(_make_opener(self.proxy, self.insecure).open(
            req, timeout=self.timeout))

    def _fetch_segment(self, seg):
        attempt = 0
        while seg.remaining > 0 and not self._stop.is_set():
            hdrs = self._request_headers(seg)
            try:
                with self._open_stream(hdrs) as resp:
                    if resp.status == 200 and "Range" in hdrs:
                        # 带了 Range 却返回 200 → 服务端忽略 Range，整包返回
                        raise DownloadError("服务端忽略了 Range 请求（返回 200）")
                    if resp.status == 206:
                        cr = _parse_content_range(resp.headers.get("Content-Range"))
                        expect = seg.start + seg.done
                        if cr and cr[0] != expect:
                            raise DownloadError(f"区间不匹配: 期望 {expect}, 返回 {cr[0]}")
                    pos = seg.start + seg.done
                    seam = self._seam_len(seg)
                    tail = bytearray()
                    while not self._stop.is_set():
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        self._limiter.consume(len(chunk))
                        if pos <= seg.end:
                            n = min(len(chunk), seg.end + 1 - pos)
                            self._write_at(pos, chunk[:n])
                            pos += n
                            with self._lock:
                                seg.done += n
                                self.done += n
                            rest = chunk[n:]
                        else:
                            rest = chunk
                        if rest and len(tail) < seam:
                            # 接缝重叠读：只留作比对，不写盘（边界外由邻居负责写）
                            tail.extend(rest[:seam - len(tail)])
                        if pos > seg.end and len(tail) >= seam:
                            break
                        self._save_state()
                        self._emit()
                    if tail and len(tail) > len(seg.tail):
                        seg.tail = bytes(tail)      # 保留最长的一次接缝样本
                if seg.remaining == 0:
                    self._check_seam(seg)
                    return
                # 提前断开：重试续传剩余部分
                raise DownloadError("连接提前结束")
            except DownloadError as e:
                if "Range" in str(e) or "区间不匹配" in str(e):
                    raise
                attempt += 1
                if attempt > self.max_retries:
                    raise
                time.sleep(min(2 ** attempt * 0.5, 8))
            except urllib.error.HTTPError as e:
                if e.code == 416 and seg.remaining == 0:
                    return
                attempt += 1
                if attempt > self.max_retries:
                    raise DownloadError(f"HTTP {e.code}") from e
                time.sleep(min(2 ** attempt * 0.5, 8))
            except Exception as e:
                attempt += 1
                if attempt > self.max_retries:
                    raise DownloadError(str(e)) from e
                time.sleep(min(2 ** attempt * 0.5, 8))

    def _claim(self):
        """认领一个可下载的分片；没有可认领的返回 None。

        被移交的块带 retry_at，到时间之前不认领（避免同一条坏连接立刻重抢）。
        """
        while not self._stop.is_set():
            now = time.monotonic()
            soonest = None
            with self._lock:
                for s in self.segments:
                    if s.remaining <= 0 or s.claimed:
                        continue
                    if s.retry_at > now:
                        soonest = s.retry_at if soonest is None else min(soonest, s.retry_at)
                        continue
                    s.claimed = True      # 认领，避免多线程抢同一段
                    return s
                waiting = any(s.remaining > 0 for s in self.segments)
            if not waiting:
                return None
            if soonest is None:
                return None               # 剩余块都在别的连接手里
            self._stop.wait(min(0.3, max(0.05, soonest - now)))
        return None

    def _worker(self):
        while not self._stop.is_set():
            seg = self._claim()
            if seg is None:
                return
            try:
                self._fetch_segment(seg)
            except DownloadError as e:
                if self._fallback:
                    return
                if "服务端忽略了 Range" in str(e):
                    self._fallback = True
                    self._stop.set()
                    return
                if not self._reassign(seg, str(e)):
                    self.error = str(e)
                    self._stop.set()
                    return
            except Exception as e:
                if self._fallback:
                    return
                if not self._reassign(seg, str(e)):
                    self.error = str(e)
                    self._stop.set()
                    return

    # ---------------- 主流程 ----------------

    def _sample_verify(self):
        """抽样回读：重取几段数据与本地比对。

        对应 IDM 的行为（"newly received data did not match the same data
        downloaded earlier"）。注意：IDM 的具体检测算法未逆出，这里是等价实现。
        """
        if not self.total or not self.verify_sample:
            return False
        size = min(65536, max(512, self.total // 4))
        if size >= self.total:
            positions = [0]
        else:
            positions = [0, max(0, self.total // 2),
                         max(0, self.total - size)][: max(1, self.verify_sample)]
        opener = _make_opener(self.proxy, self.insecure)
        for pos in positions:
            hdrs = {"User-Agent": DEFAULT_UA}
            hdrs.update(self.headers)
            hdrs["Range"] = f"bytes={pos}-{pos + size - 1}"
            try:
                with self._open_stream(hdrs) as resp:
                    if resp.status != 206:
                        continue
                    remote = resp.read(size)
            except Exception:
                continue
            with self._io_lock:
                self._fh.seek(pos)
                local = self._fh.read(len(remote))
            if local and remote and local != remote:
                return True
        return False

    def _apply_schedule(self):
        if not self.schedule:
            return
        from .config import schedule_limit
        lim = schedule_limit(self.schedule)
        self._limiter.set_bps(lim if lim is not None else self.limit_bps)

    def _schedule_loop(self, interval=5):
        while self.status == "running" and not self._stop.is_set():
            try:
                self._apply_schedule()
            except Exception:
                pass
            self._stop.wait(interval)

    def _reassign(self, seg, err):
        """把没下完（或还没开始）的分片放回池子，交给其他连接。

        对应 IDM 的 "The file chunk of connection %d is reassigned to connection %d."
        —— 日志里 downloaded=0 表示块还没开始就被移交，所以这里单独计 handoffs。

        返回 False 表示该分片已被放弃太多次，应当终止整个下载。
        """
        with self._lock:
            seg.claimed = False
            seg.fails += 1
            seg.retry_at = time.monotonic() + min(2 ** seg.fails * 0.5, 8)
            self.reassigned += 1
            if seg.done == 0:
                self.handoffs += 1
            exhausted = seg.fails > max(2, self.max_retries)
        if exhausted:
            return False
        return True

    def _check_seam(self, seg, final=False):
        """把本段多读的 64 字节与相邻段已落盘的数据逐字节比对。

        返回 True/False/None（None = 邻居还没写到，稍后再比）。
        """
        if not seg.tail:
            return None
        nxt = None
        for s in self.segments:
            if s.start == seg.end + 1:
                nxt = s
                break
        if nxt is None:
            return None
        need = len(seg.tail)
        if nxt.done < need and not final:
            return None                  # 邻居还没写到这里，留到最后再比
        with self._io_lock:
            try:
                self._fh.seek(seg.end + 1)
                local = self._fh.read(need)
            except (ValueError, OSError):
                return None
        if len(local) < need:
            return None
        seg.seam_ok = (local == seg.tail)
        with self._lock:
            self.seams_checked += 1
            if not seg.seam_ok:
                self.seams_mismatched += 1
        return seg.seam_ok

    def _verify_seams(self):
        """收尾时补做还没比对的接缝，返回不一致的接缝数（含下载过程中已判定的）。"""
        bad = 0
        for seg in self.segments:
            if seg.seam_ok is None and seg.tail:
                self._check_seam(seg, final=True)
            if seg.seam_ok is False:
                bad += 1
        return bad

    def _monitor(self):
        last_done, last_t = self.done, time.monotonic()
        while not self._stop.is_set() and self.status == "running":
            time.sleep(0.5)
            now = time.monotonic()
            dt = now - last_t
            if dt <= 0:
                continue
            cur = self.done
            inst = (cur - last_done) / dt
            self.speed = inst if self.speed == 0 else (self.speed * 0.6 + inst * 0.4)
            last_done, last_t = cur, now
            self._emit()

    def run(self):
        self.status = "running"
        self.started_at = time.time()
        self._apply_schedule()
        if self.schedule:
            threading.Thread(target=self._schedule_loop, daemon=True).start()
        try:
            self.probe_info = probe(self.url, self.headers, self.proxy,
                                    self.insecure, self.timeout)
            self.total = self.probe_info.total or 0
            # 探测时已跟随过重定向，取数直接用最终地址，避免每块都重定向一次
            self._fetch_url = self.probe_info.final_url or self.url
            if self.keepalive:
                from .pool import Pool
                self._pool = Pool(self.proxy, self.insecure, self.timeout)

            if not self.dest:
                self.dest = self.probe_info.filename
            self.dest = os.path.abspath(self.dest)
            parent = os.path.dirname(self.dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._state_path = self._state_file()

            if not self._load_state():
                self.segments = plan_segments(self.total, self.connections,
                                              self.min_seg, self.dynamic_chunks)

            # IDM 的模型：分片先写临时文件，完成后才搬运到目标路径
            self._temp_path = self.dest + ".idmpart"
            self._fh = (open(self._temp_path, "r+b") if os.path.exists(self._temp_path)
                        else open(self._temp_path, "w+b"))
            if self.total:
                self._fh.truncate(self.total)
            self.done = sum(s.done for s in self.segments)

            mon = threading.Thread(target=self._monitor, daemon=True)
            mon.start()

            if not self.probe_info.ranges:
                # 服务端不支持 Range：直接单连接整取
                self._single_stream()
            else:
                self._run_workers()
                if self._fallback:
                    self._stop.clear()
                    self._single_stream()

            mon.join(timeout=1)
            if self.error:
                raise DownloadError(self.error)

            # 接缝比对：相邻块重叠的 64 字节必须一致（IDM 的原生机制）
            if (self.verify_seam and self.total and len(self.segments) > 1
                    and self._verify_seams()):
                self.mismatch_detected = True
                self._restart_single()

            if self.verify_sample and self.total and self._sample_verify():
                self.mismatch_detected = True
                self._restart_single()

            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None

            size = os.path.getsize(self._temp_path)
            written = self.done
            if self.total and written < self.total:
                # 文件是预分配的，不能用文件大小判断完成度
                raise DownloadError(f"未完成: {written}/{self.total} 字节")
            if self.total and size < self.total:
                raise DownloadError(f"大小不符: {size} != {self.total}")

            if self._pool is not None:
                self._pool.close()
            os.replace(self._temp_path, self.dest)   # 原子搬运
            self._temp_path = None
            self.status = "done"
            self.finished_at = time.time()
            self.speed = 0.0
            self.done = written or size
            self._emit()
            if self._state_path and os.path.exists(self._state_path):
                os.remove(self._state_path)

            result = self.snapshot()
            result["elapsed_sec"] = round(self.finished_at - self.started_at, 2)
            if self.verify:
                h = hashlib.new(self.verify)
                with open(self.dest, "rb") as f:
                    for blk in iter(lambda: f.read(1 << 20), b""):
                        h.update(blk)
                result["checksum"] = {self.verify: h.hexdigest()}
            return result
        except Exception as e:
            # 内部失败也会置 _stop，必须区分"用户取消"与"出错"
            self.status = "cancelled" if self._user_cancelled else "error"
            if self.status == "cancelled" and self.done and self.total and self.done >= self.total:
                self.status = "done"
            self.error = self.error or str(e)
            self.speed = 0.0
            self._save_state(force=True)
            self._emit()
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
            if self._pool is not None:
                self._pool.close()
            if self.status == "error":
                raise DownloadError(self.error) from e
            return self.snapshot()

    def _run_workers(self):
        n = min(self.connections, max(1, len(self.segments)))
        threads = [threading.Thread(target=self._worker, daemon=True) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _restart_single(self):
        """发现数据不一致后，丢弃已下内容，用单连接整包重下。"""
        self._stop.clear()
        self.segments = [Segment(0, 0, max(0, self.total - 1))]
        self.done = 0
        if self.total:
            self._fh.truncate(0)
        self._single_stream()

    def _single_stream(self):
        """服务端不支持 Range：单连接整取。"""
        self.segments = [Segment(0, 0, max(0, self.total - 1))]
        self.done = 0
        if self.total:
            self._fh.truncate(0)
        self._fetch_segment_single()

    def _fetch_segment_single(self):
        opener = _make_opener(self.proxy, self.insecure)
        h = {"User-Agent": DEFAULT_UA}
        h.update(self.headers)
        req = urllib.request.Request(self.url, headers=h, method="GET")
        pos = 0
        with opener.open(req, timeout=self.timeout) as resp:
            while not self._stop.is_set():
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                self._limiter.consume(len(chunk))
                self._write_at(pos, chunk)
                pos += len(chunk)
                with self._lock:
                    self.done = pos
                self._emit()
        if self.total and pos != self.total:
            raise DownloadError(f"大小不符: {pos} != {self.total}")
