"""idmclone.ftp — FTP / FTPS 下载（多连接 + 断点续传）。

命令集对齐 IDM 的实际用法（逆向自 FTPSession 相关字符串）：
    USER / PASS / CWD / TYPE I / NOOP / SIZE / REST <n> / RETR <path> / PASV
IDM 只用 PASV（"The server does not support PASV."），续传依赖 REST；
服务器不支持 REST 时 IDM 会提示"只能从头下载"。

实现上复用标准库 ftplib：每个连接一条控制链路，用 transfercmd(RETR, rest=offset)
拿到数据 socket，只读到本段边界为止。
"""

from __future__ import annotations

import ftplib
import os
import ssl
import threading
import time
from urllib.parse import unquote, urlsplit

from .engine import DownloadError, plan_segments


class FtpError(DownloadError):
    pass


class FtpDownload:
    def __init__(self, url, dest=None, connections=8, min_seg=1 << 20,
                 insecure=False, resume=True, progress=None, timeout=30,
                 max_retries=4, verify=None):
        parts = urlsplit(url)
        if parts.scheme not in ("ftp", "ftps"):
            raise FtpError(f"不是 FTP 地址: {url}")
        self.url = url
        self.tls = parts.scheme == "ftps"
        self.host = parts.hostname or ""
        self.port = parts.port or (990 if self.tls else 21)
        self.user = unquote(parts.username) if parts.username else "anonymous"
        self.password = unquote(parts.password) if parts.password else "anonymous@"
        self.path = unquote(parts.path or "/")
        self.dest = dest
        self.connections = connections
        self.min_seg = min_seg
        self.insecure = insecure
        self.resume = resume
        self.progress_cb = progress
        self.timeout = timeout
        self.max_retries = max_retries
        self.verify = verify

        self.total = 0
        self.done = 0
        self.segments = []
        self.status = "pending"
        self.error = None
        self.speed = 0.0
        self.reassigned = 0
        self.started_at = None
        self.finished_at = None

        self._stop = threading.Event()
        self._user_cancelled = False
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._fh = None
        self._temp_path = None
        self._state_path = None

    # ---------------- 连接 ----------------

    def _connect(self):
        cls = ftplib.FTP_TLS if self.tls else ftplib.FTP
        ftp = cls(timeout=self.timeout)
        if self.tls:
            ctx = ssl.create_default_context()
            if self.insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            ftp.context = ctx
        ftp.connect(self.host, self.port, timeout=self.timeout)
        ftp.login(self.user, self.password)
        if self.tls:
            ftp.prot_p()
        ftp.voidcmd("TYPE I")          # 二进制，IDM 用 "TYPE I"
        return ftp

    # ---------------- 状态 ----------------

    def snapshot(self):
        pct = (self.done / self.total * 100) if self.total else 0.0
        eta = (self.total - self.done) / self.speed if (self.speed and self.total
                                                         and self.done < self.total) else None
        return {
            "id": f"ftp-{abs(hash(self.url)) % 10**10}",
            "url": self.url,
            "dest": self.dest,
            "status": self.status,
            "error": self.error,
            "total": self.total,
            "downloaded": self.done,
            "percent": round(pct, 2),
            "speed_bps": round(self.speed, 1),
            "eta_sec": round(eta, 1) if eta else None,
            "segments": len(self.segments),
            "reassigned": self.reassigned,
            "protocol": "ftps" if self.tls else "ftp",
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

    def _save_state(self):
        if not self._state_path:
            return
        import json
        with self._lock:
            data = {"url": self.url, "dest": self.dest, "total": self.total,
                    "segments": [{"i": s.index, "s": s.start, "e": s.end, "d": s.done}
                                 for s in self.segments]}
        try:
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._state_path)
        except OSError:
            pass

    def _load_state(self):
        import json
        if not (self.resume and self._state_path and os.path.exists(self._state_path)):
            return False
        try:
            st = json.load(open(self._state_path, encoding="utf-8"))
        except Exception:
            return False
        if st.get("url") != self.url:
            return False
        if self.total and st.get("total") and st["total"] != self.total:
            return False
        self.segments = [plan_segments(self.total, 1)[0].__class__(
            s["i"], s["s"], s["e"], s.get("d", 0)) for s in st["segments"]]
        return True

    def _write_at(self, pos, data):
        with self._io_lock:
            self._fh.seek(pos)
            self._fh.write(data)

    # ---------------- 单段 ----------------

    def _fetch(self, ftp, seg):
        start = seg.start + seg.done
        left = seg.remaining
        data = ftp.transfercmd(f"RETR {self.path}", rest=start if start else None)
        try:
            while left > 0 and not self._stop.is_set():
                chunk = data.recv(min(65536, left))
                if not chunk:
                    break
                self._write_at(start, chunk)
                start += len(chunk)
                left -= len(chunk)
                with self._lock:
                    seg.done += len(chunk)
                    self.done += len(chunk)
                self._emit()
        finally:
            try:
                data.close()
            except Exception:
                pass
            try:
                ftp.voidresp()
            except Exception:
                pass
        if left > 0:
            raise FtpError(f"连接提前结束，剩余 {left} 字节")

    def _worker(self):
        ftp = None
        while not self._stop.is_set():
            seg = None
            with self._lock:
                for s in self.segments:
                    if not s.claimed and s.remaining > 0:
                        s.claimed = True
                        seg = s
                        break
            if seg is None:
                break
            attempt = 0
            while seg.remaining > 0 and not self._stop.is_set():
                try:
                    if ftp is None:
                        ftp = self._connect()
                    self._fetch(ftp, seg)
                    break
                except Exception as e:
                    attempt += 1
                    with self._lock:
                        self.reassigned += 1
                    try:
                        if ftp:
                            ftp.close()
                    except Exception:
                        pass
                    ftp = None
                    if attempt > self.max_retries:
                        with self._lock:
                            seg.claimed = False
                        self.error = str(e)
                        self._stop.set()
                        return
                    time.sleep(min(2 ** attempt * 0.5, 5))
        if ftp:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass

    def _monitor(self):
        last, t0 = self.done, time.monotonic()
        while self.status == "running":
            time.sleep(0.5)
            now = time.monotonic()
            dt = now - t0
            if dt <= 0:
                continue
            inst = (self.done - last) / dt
            self.speed = inst if self.speed == 0 else self.speed * 0.6 + inst * 0.4
            last, t0 = self.done, now

    # ---------------- 主流程 ----------------

    def run(self):
        self.status = "running"
        self.started_at = time.time()
        try:
            probe = ftplib.FTP(timeout=self.timeout) if not self.tls else ftplib.FTP_TLS(timeout=self.timeout)
            ftp = self._connect()
            try:
                size = None
                try:
                    size = ftp.size(self.path)
                except Exception:
                    size = None
                self.total = int(size) if size else 0
            finally:
                try:
                    ftp.quit()
                except Exception:
                    ftp.close()

            if not self.dest:
                self.dest = os.path.basename(self.path.rstrip("/")) or "download.bin"
            self.dest = os.path.abspath(self.dest)
            parent = os.path.dirname(self.dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._state_path = self.dest + ".idmpart.json"
            self._temp_path = self.dest + ".idmpart"

            if not self._load_state():
                # SIZE 拿不到就单连接顺序读（无法分段）
                self.segments = (plan_segments(self.total, self.connections, self.min_seg, dynamic=False)
                                 if self.total else [])
            self._fh = (open(self._temp_path, "r+b") if os.path.exists(self._temp_path)
                        else open(self._temp_path, "w+b"))
            if self.total:
                self._fh.truncate(self.total)
            self.done = sum(s.done for s in self.segments)

            threading.Thread(target=self._monitor, daemon=True).start()

            if not self.total or not self.segments:
                self._single_stream()
            else:
                n = min(self.connections, max(1, len(self.segments)))
                ts = [threading.Thread(target=self._worker, daemon=True) for _ in range(n)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()

            if self.error:
                raise FtpError(self.error)
            if self.total and self.done < self.total:
                raise FtpError(f"未完成: {self.done}/{self.total}")

            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None
            os.replace(self._temp_path, self.dest)
            self._temp_path = None
            self.status = "done"
            self.finished_at = time.time()
            self.speed = 0.0
            if self._state_path and os.path.exists(self._state_path):
                os.remove(self._state_path)
            result = self.snapshot()
            result["elapsed_sec"] = round(self.finished_at - self.started_at, 2)
            return result
        except Exception as e:
            self.status = "cancelled" if self._user_cancelled else "error"
            self.error = self.error or str(e)
            self.speed = 0.0
            self._save_state()
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
            if self.status == "error":
                raise FtpError(self.error) from e
            return self.snapshot()

    def _single_stream(self):
        """SIZE 不可用：顺序读到 EOF。"""
        ftp = self._connect()
        try:
            pos = 0
            data = ftp.transfercmd(f"RETR {self.path}")
            try:
                while not self._stop.is_set():
                    chunk = data.recv(65536)
                    if not chunk:
                        break
                    self._write_at(pos, chunk)
                    pos += len(chunk)
                    self.done = pos
                    self.total = pos
                    self._emit()
            finally:
                data.close()
                try:
                    ftp.voidresp()
                except Exception:
                    pass
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()
