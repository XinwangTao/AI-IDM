"""ai_idm.updater — 从仓库远端检查并安装更新（服务端 + 浏览器扩展）。

设计原则（对抗性审查后的最小实现）：
- AI-IDM 是可编辑安装，代码就是工作树。更新 = 让工作树前进 + 进程重载，
  只有 `pyproject.toml` 变了才需要 `pip install -e .`。
- 服务端是唯一更新真源；扩展只读 `/update` 的结果，不再自己比对 GitHub。
- 先落回滚点再动手；失败自动 `git reset --hard` 回旧提交。
- 检查失败静默退避，绝不打扰下载主流程。

流程见 docs/archify/ai_idm_auto_update_workflow.html。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config as cfgmod

CHECK_INTERVAL = 6 * 60 * 60      # 正常冷却 6h
RETRY_INTERVAL = 30 * 60          # 失败后 30min 再试
STALE_APPLY = 30 * 60             # 更新进程失联超过 30min 视为中断（pip 最长 10min，留足余量）
GIT_TIMEOUT = 60
FETCH_TIMEOUT = 20
PIP_TIMEOUT = 600

# 检查/安装互斥：watcher 与 HTTP 请求可能同时触发 git fetch
_LOCK = threading.Lock()


# ---------------------------------------------------------------- 基础

def home() -> Path:
    return Path(cfgmod.home())


def root() -> Path:
    """仓库根目录；AI_IDM_REPO 仅供测试覆盖。"""
    override = os.environ.get("AI_IDM_REPO")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[1]


def state_path() -> Path:
    return home() / "update_state.json"


def log_path() -> Path:
    return home() / "update.log"


def is_repo() -> bool:
    return (root() / ".git").exists()


def _atomic_write(path: Path, text: str) -> None:
    """进度/状态文件必须原子写，避免断电留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(**kw) -> dict:
    st = read_state()
    st.update(kw)
    try:
        _atomic_write(state_path(), json.dumps(st, ensure_ascii=False, indent=2))
    except OSError:
        pass
    return st


def log(line: str) -> None:
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > 1_000_000:
            p.write_text("", encoding="utf-8")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {line}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- git

def _git(*args, timeout=GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(root()), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def _remote_ref() -> str:
    r = _git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    for name in ("origin/main", "origin/master"):
        if _git("rev-parse", "--verify", "--quiet", name).returncode == 0:
            return name
    return "origin/main"


def _head() -> str:
    r = _git("rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else ""


def _branch() -> str:
    r = _git("rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else ""


def _show_version(ref: str, path: str) -> str:
    r = _git("show", f"{ref}:{path}", timeout=GIT_TIMEOUT)
    if r.returncode != 0:
        return ""
    m = re.search(r'(?:__version__\s*=\s*|"version"\s*:\s*)"([^"]+)"', r.stdout)
    return m.group(1) if m else ""


def _counts(ref: str):
    """返回 (领先远端, 落后远端) 的提交数。"""
    r = _git("rev-list", "--left-right", "--count", f"HEAD...{ref}")
    if r.returncode != 0:
        return 0, 0
    parts = r.stdout.split()
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return 0, 0


def _subjects(ref: str, limit=8):
    r = _git("log", "--oneline", "--no-decorate", f"-n{limit}", f"HEAD..{ref}")
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()][:limit]


def _rebase_in_progress() -> bool:
    g = root() / ".git"
    return any((g / name).exists()
               for name in ("rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD"))


def _conflicted_files():
    r = _git("diff", "--name-only", "--diff-filter=U")
    return [x.strip() for x in r.stdout.splitlines() if x.strip()]


def _smoke() -> tuple[bool, str]:
    """新代码必须能被导入，否则回滚——不能把起不来的版本当成功。

    只 import ai_idm 不够：真正要重启的是 server/cli，这两个的顶层导入也得过。
    """
    code = ("import ai_idm, ai_idm.cli, ai_idm.server, ai_idm.updater; "
            "print(ai_idm.__version__)")
    try:
        r = _run([sys.executable, "-c", code], 90)
    except subprocess.TimeoutExpired:
        return False, "新版本导入检查超时"
    if r.returncode != 0:
        out = (r.stderr or r.stdout or "").strip().splitlines()
        return False, f"新版本导入失败：{out[-1] if out else r.returncode}"
    return True, ""


# ---------------------------------------------------------------- 跨进程锁

class UpdateLock:
    """进程内 _LOCK 管不住 CLI / 服务 / 更新子进程三方，用独占文件再锁一层。"""

    def __init__(self, stale=STALE_APPLY):
        self.path = home() / "update.lock"
        self.stale = stale
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + 3
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, str(os.getpid()).encode())
                finally:
                    os.close(fd)
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0
                if age > self.stale:
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.time() >= deadline:
                    return self
                time.sleep(0.2)
            except OSError:
                return self

    def __exit__(self, *exc):
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
        return False


def _serve_addr():
    cfg = cfgmod.load()
    host = cfg.get("server_host") or "127.0.0.1"
    port = int(cfg.get("server_port") or 13391)
    return host, port


def _serve_alive(host, port) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/ping", timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return data.get("name") == "ai-idm"
    except Exception:                                        # noqa: BLE001
        return False


def _wait_port_free(host, port, timeout=25) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not _serve_alive(host, port):
            return True
        time.sleep(0.3)
    return False


def _kill_port_owner(port) -> bool:
    """兜底：旧版本没有 /shutdown 时按端口杀服务进程（先确认 /ping 是 ai-idm）。"""
    if os.name != "nt":
        return False
    try:
        out = _run(["netstat", "-ano"], 10).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
                pid = parts[4]
                _run(["taskkill", "/PID", pid, "/F"], 10)
                return True
    except (subprocess.TimeoutExpired, OSError):
        pass
    return False


def stop_server(timeout=8) -> bool:
    """让正在运行的 serve 退出（更新完成后收尾用）。"""
    import urllib.request
    host, port = _serve_addr()
    if not _serve_alive(host, port):
        return True
    cfg = cfgmod.load()
    token = cfg.get("server_token") or ""
    body = json.dumps({"token": token}).encode("utf-8")
    req = urllib.request.Request(
        f"http://{host}:{port}/shutdown", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-AI-IDM-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status == 200:
                return _wait_port_free(host, port, 10)
    except Exception as e:                                   # noqa: BLE001
        log(f"/shutdown 不可用（{e}），尝试按端口停止")
    if _serve_alive(host, port) and _kill_port_owner(port):
        return _wait_port_free(host, port, 10)
    log("停止旧服务失败：端口仍被占用")
    return False


# ---------------------------------------------------------------- 检查

def _due(st: dict, now: float) -> bool:
    checked = float(st.get("checked_at") or 0)
    if not checked:
        return True
    interval = RETRY_INTERVAL if st.get("error") else CHECK_INTERVAL
    return now - checked >= interval


def _clear_stale_apply(st: dict, now: float) -> dict:
    """更新进程被杀后会留下 applying=true，不清掉就永远更新不了。"""
    if not st.get("applying"):
        return st
    if _lock_fresh():
        return st
    started = float(st.get("apply_started_at") or 0)
    if started and now - started < STALE_APPLY:
        return st
    log("检测到上次更新中断，解除 applying 锁")
    return write_state(applying=False, error="上次更新中断（进程失联），已解除锁定；"
                                             "如有残留 rebase 会在下次更新时自动清理")


def _lock_fresh() -> bool:
    try:
        return time.time() - (home() / "update.lock").stat().st_mtime <= STALE_APPLY
    except OSError:
        return False


def check(force: bool = False) -> dict:
    """拉取远端并比较。失败只记录，不抛异常。"""
    with _LOCK:
        return _check_locked(force)


def _check_locked(force: bool) -> dict:
    now = time.time()
    st = _clear_stale_apply(read_state(), now)
    if st.get("applying"):
        return st
    if not force and not _due(st, now):
        return st
    if not is_repo():
        return write_state(checked_at=now, error="当前安装不是 git 仓库，无法自动更新",
                           available=False)

    ref = _remote_ref()
    try:
        r = _git("fetch", "--prune", "origin", timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return write_state(checked_at=now, error="拉取远端超时", available=False)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "git fetch 失败").strip().splitlines()
        return write_state(checked_at=now, error=msg[-1] if msg else "git fetch 失败",
                           available=False)

    ahead, behind = _counts(ref)
    st = write_state(
        checked_at=now, error=None, ref=ref,
        local_commit=_head(), remote_commit=_git("rev-parse", ref).stdout.strip(),
        ahead=ahead, behind=behind, available=behind > 0,
        local_version=_show_version("HEAD", "ai_idm/__init__.py"),
        remote_version=_show_version(ref, "ai_idm/__init__.py"),
        local_ext_version=_show_version("HEAD", "browser-extension/manifest.json"),
        remote_ext_version=_show_version(ref, "browser-extension/manifest.json"),
        subjects=_subjects(ref) if behind else [],
    )
    if behind:
        log(f"发现 {behind} 个新提交（{ref}）")
    return st


# ---------------------------------------------------------------- 安装

def _run(cmd, timeout) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(root()), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _pip_install() -> tuple[bool, str]:
    try:
        r = _run([sys.executable, "-m", "pip", "install", "-e", "."], PIP_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "pip install 超时"
    if r.returncode != 0:
        out = (r.stderr or r.stdout or "").strip().splitlines()
        return False, f"pip install 失败：{out[-1] if out else r.returncode}"
    return True, ""


def _sync_extension() -> tuple[bool, str]:
    script = root() / "scripts" / "install_to_edge.py"
    if not script.is_file():
        return True, "未找到 install_to_edge.py，跳过扩展同步"
    try:
        r = _run([sys.executable, str(script)], 120)
    except subprocess.TimeoutExpired:
        return False, "扩展同步超时"
    if r.returncode != 0:
        out = (r.stdout or r.stderr or "").strip().splitlines()
        return False, f"扩展同步失败：{out[-1] if out else r.returncode}"
    return True, "扩展已同步到 Edge 快照，需要重新加载扩展"


def _pyproject_changed(old: str, new: str) -> bool:
    r = _git("diff", "--name-only", old, new)
    return any(line.strip() == "pyproject.toml" for line in r.stdout.splitlines())


def _restore(commit: str, branch: str) -> None:
    _git("rebase", "--abort")
    if branch and branch not in ("HEAD", ""):
        _git("checkout", branch)
    _git("reset", "--hard", commit)


def apply() -> dict:
    """把工作树更新到远端最新；任何一步失败都回滚到旧提交。"""
    with _LOCK, UpdateLock() as lk:
        if not lk.acquired:
            return {"ok": False, "error": "另一个更新进程正在运行，请稍后再试"}
        return _apply_locked()


def _apply_locked() -> dict:
    st = _clear_stale_apply(read_state(), time.time())
    if st.get("applying"):
        return {"ok": False, "error": "已有更新在进行中"}
    if not is_repo():
        return {"ok": False, "error": "当前安装不是 git 仓库，无法自动更新"}

    # 上次更新被强杀可能留下半截 rebase：只清理我们自己留下的（有 apply_started_at）
    if _rebase_in_progress():
        if st.get("apply_started_at"):
            log("清理上次中断留下的 rebase")
            _git("rebase", "--abort")
        else:
            return {"ok": False,
                    "error": "仓库里有未完成的 rebase/cherry-pick，请先手动处理（git rebase --abort）"}

    dirty = _git("status", "--porcelain", "--untracked-files=no")
    if dirty.stdout.strip():
        return {"ok": False, "error": "工作区有未提交改动，请先提交或还原后再更新"}

    ref = _remote_ref()
    old = _head()
    branch = _branch()
    if not old:
        return {"ok": False, "error": "读不到当前提交"}

    write_state(applying=True, rollback_commit=old, rollback_branch=branch,
                apply_started_at=time.time(), error=None)
    log(f"开始更新：{old[:8]} -> {ref}")

    try:
        r = _git("fetch", "--prune", "origin", timeout=FETCH_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError("拉取远端失败")

        r = _git("rebase", ref, timeout=180)
        if r.returncode != 0:
            files = _conflicted_files()
            _restore(old, branch)
            detail = f"：{', '.join(files[:5])}" if files else ""
            raise RuntimeError(f"本地改动与上游冲突{detail}，已回滚")

        new = _head()
        if new == old:
            write_state(applying=False, available=False, last_apply={
                "ok": True, "at": time.time(), "from": old, "to": new,
                "detail": "已是最新"})
            return {"ok": True, "detail": "已是最新", "commit": new}

        if _pyproject_changed(old, new):
            ok, err = _pip_install()
            if not ok:
                _restore(old, branch)
                _pip_install()
                raise RuntimeError(err)

        ok, err = _smoke()
        if not ok:
            _restore(old, branch)
            if _pyproject_changed(old, new):
                _pip_install()
            raise RuntimeError(err)

        ext_ok, ext_msg = _sync_extension()
        write_state(applying=False, available=False, checked_at=time.time(),
                    local_commit=new, behind=0, subjects=[],
                    local_version=_show_version("HEAD", "ai_idm/__init__.py"),
                    local_ext_version=_show_version("HEAD", "browser-extension/manifest.json"),
                    last_apply={"ok": True, "at": time.time(), "from": old, "to": new,
                                "ext_ok": ext_ok, "ext": ext_msg})
        log(f"更新完成：{old[:8]} -> {new[:8]}；{ext_msg}")
        return {"ok": True, "from": old, "to": new, "ext_ok": ext_ok, "detail": ext_msg}
    except Exception as e:                                    # noqa: BLE001
        write_state(applying=False, error=str(e), last_apply={
            "ok": False, "at": time.time(), "from": old, "error": str(e)})
        log(f"更新失败并回滚：{e}")
        return {"ok": False, "error": str(e), "rolled_back": _head() == old}


def rollback(commit: str | None = None) -> dict:
    """一键回到上一次更新前的提交（或指定提交）。"""
    with _LOCK, UpdateLock() as lk:
        if not lk.acquired:
            return {"ok": False, "error": "另一个更新进程正在运行，请稍后再试"}
        return _rollback_locked(commit)


def _rollback_locked(commit: str | None) -> dict:
    st = read_state()
    target = commit or st.get("rollback_commit") or ""
    if not target:
        return {"ok": False, "error": "没有可用的回滚点"}
    if not is_repo():
        return {"ok": False, "error": "当前安装不是 git 仓库"}
    if _rebase_in_progress():
        return {"ok": False, "error": "仓库里有未完成的 rebase，请先处理"}
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    if dirty.stdout.strip():
        return {"ok": False, "error": "工作区有未提交改动，请先提交或还原"}
    old = _head()
    r = _git("reset", "--hard", target)
    if r.returncode != 0:
        return {"ok": False, "error": "回滚失败"}
    warn = ""
    if _pyproject_changed(old, target):
        ok, err = _pip_install()
        if not ok:
            warn = err
            log(f"回滚后重装依赖失败：{err}")
    ext_ok, ext_msg = _sync_extension()
    ref = _remote_ref()
    ahead, behind = _counts(ref)
    write_state(applying=False, available=behind > 0, ahead=ahead, behind=behind,
                local_commit=_head(), local_version=_show_version("HEAD", "ai_idm/__init__.py"),
                local_ext_version=_show_version("HEAD", "browser-extension/manifest.json"),
                last_apply={"ok": True, "at": time.time(), "from": old, "to": target,
                            "rollback": True, "ext_ok": ext_ok, "pip_error": warn})
    log(f"手动回滚：{old[:8]} -> {target[:8]}")
    detail = "；".join(x for x in (ext_msg, warn) if x)
    return {"ok": True, "from": old, "to": target, "detail": detail,
            "warning": warn or None}


# ---------------------------------------------------------------- 进程

def _detached_flags():
    if os.name == "nt":
        return (subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW)
    return 0


def spawn_detached(args) -> bool:
    """启动一个脱离当前进程的子进程（更新器 / 重启服务）。"""
    try:
        subprocess.Popen(
            args, cwd=str(root()), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_detached_flags(), close_fds=True,
        )
        return True
    except OSError as e:
        log(f"子进程启动失败：{e}")
        return False


def spawn_restart() -> bool:
    """等旧服务放开端口再拉起新实例，并确认它真的起来了。

    不等的话：旧 serve 还在关闭时新客人先绑端口 → 单实例保护让新实例安静退出，
    结果服务彻底没了。
    """
    host, port = _serve_addr()
    if not _wait_port_free(host, port, timeout=25):
        log(f"重启失败：{host}:{port} 仍被占用，未启动新实例")
        return False
    if not spawn_detached([sys.executable, "-m", "ai_idm", "serve", "-q"]):
        return False
    end = time.time() + 30
    while time.time() < end:
        if _serve_alive(host, port):
            log("服务已重启")
            return True
        time.sleep(0.5)
    log("服务重启后 30s 内未响应 /ping")
    return False
