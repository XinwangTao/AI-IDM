"""ai_idm.queue — SQLite 任务队列，跨进程持久化。

一个任务一行，状态机：pending → running → done / error / cancelled。
重启后 pending 与 running（被视为中断）都可继续；running 会被回收为 pending。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid

from .engine import Download, DownloadError

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    dest         TEXT,
    connections  INTEGER DEFAULT 8,
    limit_bps    INTEGER,
    proxy        TEXT,
    headers      TEXT,
    status       TEXT DEFAULT 'pending',
    priority     INTEGER DEFAULT 0,
    attempts     INTEGER DEFAULT 0,
    total        INTEGER DEFAULT 0,
    downloaded   INTEGER DEFAULT 0,
    error        TEXT,
    added_at     REAL,
    started_at   REAL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_pick ON tasks(status, priority DESC, added_at);
CREATE INDEX IF NOT EXISTS idx_tasks_url ON tasks(url);
"""


def default_db_path():
    home = os.environ.get("AI_IDM_HOME") or os.environ.get("IDMCLONE_HOME")
    if not home:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        home = os.path.join(base, "ai-idm")
    return os.path.join(home, "queue.db")


class Queue:
    def __init__(self, path=None):
        self.path = path or default_db_path()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------------- 写 ----------------

    def add(self, url, dest=None, connections=8, limit_bps=None, proxy=None,
            headers=None, priority=0):
        tid = uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (id,url,dest,connections,limit_bps,proxy,headers,"
                "status,priority,attempts,added_at) VALUES (?,?,?,?,?,?,?,?,?,0,?)",
                (tid, url, dest, int(connections or 8), limit_bps,
                 proxy, json.dumps(headers or {}, ensure_ascii=False),
                 "pending", int(priority or 0), time.time()))
            self._conn.commit()
        return tid

    def update(self, tid, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE tasks SET {cols} WHERE id=?",
                               (*fields.values(), tid))
            self._conn.commit()

    def remove(self, tid):
        with self._lock:
            cur = self._conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
            self._conn.commit()
        return cur.rowcount > 0

    def _match_active(self, url, dest, proxy, headers):
        """在已持有 self._lock 的前提下，查找与入参等价的活跃任务行。"""
        want_h = json.dumps(headers or {}, ensure_ascii=False, sort_keys=True)
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE url=? AND status IN ('pending','running')",
            (url,)).fetchall()
        for r in rows:
            if (r["dest"] or None) != (dest or None):
                continue
            if (r["proxy"] or None) != (proxy or None):
                continue
            try:
                got_h = json.dumps(json.loads(r["headers"] or "{}"),
                                   ensure_ascii=False, sort_keys=True)
            except Exception:                                  # noqa: BLE001
                got_h = r["headers"] or "{}"
            if got_h == want_h:
                return r
        return None

    def find_active(self, url, dest=None, proxy=None, headers=None):
        """查找与入参等价的活跃任务（pending/running），命中返回 dict，否则 None。"""
        with self._lock:
            r = self._match_active(url, dest, proxy, headers)
            return self._row_to_dict(r) if r is not None else None

    def find_or_add(self, url, dest=None, connections=8, limit_bps=None, proxy=None,
                    headers=None, priority=0):
        """原子化的"查重 + 入队"：返回 (任务行, 是否命中已有)。

        「查」和「插」在同一把锁内完成，避免并发 /add 的 TOCTOU 竞态
        （否则两个请求都查不到、都插入，还是会产生重复）。
        """
        with self._lock:
            r = self._match_active(url, dest, proxy, headers)
            if r is not None:
                return self._row_to_dict(r), True
            tid = uuid.uuid4().hex[:12]
            self._conn.execute(
                "INSERT INTO tasks (id,url,dest,connections,limit_bps,proxy,headers,"
                "status,priority,attempts,added_at) VALUES (?,?,?,?,?,?,?,?,?,0,?)",
                (tid, url, dest, int(connections or 8), limit_bps,
                 proxy, json.dumps(headers or {}, ensure_ascii=False),
                 "pending", int(priority or 0), time.time()))
            self._conn.commit()
        return ({"id": tid, "status": "pending", "dest": dest,
                 "connections": int(connections or 8)}, False)

    def purge(self, statuses=("done", "error", "cancelled"), older_than_days=None):
        """删除指定状态的记录，返回删除行数。older_than_days 给定时只删更早的。"""
        statuses = tuple(statuses)
        if not statuses:
            return 0
        qmarks = ",".join("?" * len(statuses))
        sql = f"DELETE FROM tasks WHERE status IN ({qmarks})"
        args = list(statuses)
        if older_than_days is not None:
            cutoff = time.time() - float(older_than_days) * 86400
            sql += " AND COALESCE(finished_at, added_at, 0) < ?"
            args.append(cutoff)
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
        return cur.rowcount

    def retry(self, tid):
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET status='pending', error=NULL, attempts=0,"
                "started_at=NULL, finished_at=NULL WHERE id=?", (tid,))
            self._conn.commit()
        return cur.rowcount > 0

    def reset_running(self):
        """把上次进程遗留的 running 任务回收成 pending（断点续传会接上）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET status='pending' WHERE status='running'")
            self._conn.commit()
        return cur.rowcount

    # ---------------- 读 ----------------

    def list(self, status=None, limit=200):
        sql = "SELECT * FROM tasks"
        args = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY priority DESC, added_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get(self, tid):
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return self._row_to_dict(row) if row else None

    def stats(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) c, SUM(total) t, SUM(downloaded) d "
                "FROM tasks GROUP BY status").fetchall()
        return {r["status"]: {"count": r["c"], "total": r["t"] or 0,
                              "downloaded": r["d"] or 0} for r in rows}

    def claim_next(self):
        """原子取一个 pending 任务并置为 running。"""
        with self._lock:
            row = self._conn.execute(
                "UPDATE tasks SET status='running', started_at=? "
                "WHERE id=(SELECT id FROM tasks WHERE status='pending' "
                "ORDER BY priority DESC, added_at LIMIT 1) RETURNING *",
                (time.time(),)).fetchone()
            self._conn.commit()
        return self._row_to_dict(row) if row else None

    @staticmethod
    def _row_to_dict(row):
        d = dict(row)
        try:
            d["headers"] = json.loads(d.get("headers") or "{}")
        except Exception:
            d["headers"] = {}
        return d


class Runner:
    """按并行度消费队列。"""

    def __init__(self, queue, parallel=2, max_attempts=3, on_event=None,
                 dest_dir=None):
        self.q = queue
        self.parallel = max(1, int(parallel))
        self.max_attempts = max_attempts
        self.on_event = on_event or (lambda evt: None)
        # dest 为空时落到这个目录（服务端接管时用，CLI 队列运行默认 None=当前目录）
        self.dest_dir = dest_dir
        self.active = {}                     # tid -> Download（供取消）
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads = []

    def stop(self):
        self._stop.set()

    def cancel(self, tid):
        """取消正在跑的任务；没在跑返回 False（调用方自行改库）。"""
        with self._lock:
            dl = self.active.get(tid)
        if dl is None:
            return False
        dl.cancel()
        return True

    def stop(self):
        self._stop.set()

    def run(self, idle_exit=True):
        self.q.reset_running()
        threads = [threading.Thread(target=self._worker, args=(idle_exit,), daemon=True)
                   for _ in range(self.parallel)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return self.q.stats()

    def _worker(self, idle_exit):
        while not self._stop.is_set():
            task = self.q.claim_next()
            if not task:
                if idle_exit:
                    return
                time.sleep(0.5)
                continue
            self._run_task(task)

    def _run_task(self, task):
        tid = task["id"]
        self.on_event({"event": "start", "id": tid, "url": task["url"]})
        from . import config as cfgmod
        cfg = cfgmod.load()
        r = cfgmod.resolve(task["url"], cfg, task.get("connections"),
                           task.get("limit_bps"), task.get("proxy"))
        is_ftp = task["url"].lower().startswith(("ftp://", "ftps://"))
        if is_ftp:
            from .ftp import FtpDownload
            dl = FtpDownload(task["url"], dest=task.get("dest"),
                             connections=r["connections"])
            try:
                result = dl.run()
                self.q.update(tid, status="done", dest=result.get("dest"),
                              total=result.get("total") or 0,
                              downloaded=result.get("downloaded") or 0,
                              error=None, finished_at=time.time())
                self.on_event({"event": "done", "id": tid, "result": result})
            except Exception as e:
                attempts = (task.get("attempts") or 0) + 1
                status = "pending" if attempts < self.max_attempts else "error"
                self.q.update(tid, status=status, attempts=attempts, error=str(e))
                self.on_event({"event": "error", "id": tid, "error": str(e)})
            return

        dl = Download(
            task["url"],
            dest=task.get("dest"),
            connections=r["connections"],
            limit_bps=r["limit_bps"],
            headers=task.get("headers") or {},
            proxy=r["proxy"],
            schedule=cfg.get("speed_schedule") or [],
            dynamic_chunks=bool(cfg.get("dynamic_chunks", True)),
            keepalive=bool(cfg.get("keepalive", True)),
            dest_dir=self.dest_dir,
        )
        last_save = 0.0

        def progress(snap):
            nonlocal last_save
            now = time.monotonic()
            if now - last_save < 1.0:
                return
            last_save = now
            self.q.update(tid, downloaded=snap["downloaded"], total=snap["total"])

        dl.progress_cb = progress
        with self._lock:
            self.active[tid] = dl
        try:
            result = dl.run()
            if result.get("status") == "cancelled":
                self.q.update(tid, status="cancelled", error=None,
                              downloaded=result.get("downloaded") or 0,
                              total=result.get("total") or 0,
                              finished_at=time.time())
                self.on_event({"event": "cancelled", "id": tid})
                return
            self.q.update(tid, status="done", dest=result.get("dest"),
                          total=result.get("total") or 0,
                          downloaded=result.get("downloaded") or 0,
                          error=None, finished_at=time.time())
            self.on_event({"event": "done", "id": tid, "result": result})
        except DownloadError as e:
            attempts = (task.get("attempts") or 0) + 1
            status = "pending" if attempts < self.max_attempts else "error"
            self.q.update(tid, status=status, attempts=attempts, error=str(e),
                          started_at=None,
                          finished_at=None if status == "pending" else time.time())
            self.on_event({"event": "error" if status == "error" else "retry",
                           "id": tid, "error": str(e), "attempts": attempts})
        finally:
            with self._lock:
                self.active.pop(tid, None)
