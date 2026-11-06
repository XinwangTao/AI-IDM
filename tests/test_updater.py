"""更新器测试：版本比较、脏工作区拒绝、成功更新、回滚。

用临时 bare 仓库模拟 origin，不碰真实安装。
"""

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    from ai_idm.stdio import ensure_utf8_stdio
    ensure_utf8_stdio()
except Exception:
    pass

HOME = tempfile.mkdtemp(prefix="idmupd-home-")
WORK = tempfile.mkdtemp(prefix="idmupd-work-")
ORIGIN = tempfile.mkdtemp(prefix="idmupd-origin-")
CLONE = tempfile.mkdtemp(prefix="idmupd-clone-")

results = []


def git(*args, cwd=None):
    r = subprocess.run(["git", *args], cwd=cwd or WORK, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{r.stderr}")
    return r.stdout


def write(path, text):
    full = os.path.join(WORK, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(text)


def version_file(ver):
    return f'"""AI-IDM test fixture."""\n\n__version__ = "{ver}"\n'


def main():
    subprocess.run(["git", "init", "--bare", "-q", ORIGIN], check=True)
    subprocess.run(["git", "init", "-q", WORK], check=True)
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("checkout", "-q", "-b", "main")

    write("ai_idm/__init__.py", version_file("1.0.0"))
    # 冒烟会 import 这些模块，fixture 里必须有
    for name in ("cli.py", "server.py", "updater.py"):
        write(f"ai_idm/{name}", "# fixture\n")
    write("browser-extension/manifest.json", '{\n  "version": "1.0.0"\n}\n')
    write("pyproject.toml", '[project]\nname = "ai-idm"\nversion = "1.0.0"\n')
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    git("remote", "add", "origin", ORIGIN)
    git("push", "-q", "-u", "origin", "main")

    os.environ["AI_IDM_HOME"] = HOME
    os.environ["AI_IDM_REPO"] = WORK

    from ai_idm import updater

    st = updater.check(force=True)
    results.append(("check-clean", st.get("available") is False and not st.get("error"), st))

    # 远端前进一个提交
    subprocess.run(["git", "clone", "-q", "-b", "main", ORIGIN, CLONE], check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=CLONE, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=CLONE, check=True)
    with open(os.path.join(CLONE, "ai_idm", "__init__.py"), "w", encoding="utf-8") as f:
        f.write(version_file("1.1.0"))
    subprocess.run(["git", "add", "-A"], cwd=CLONE, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "bump 1.1.0"], cwd=CLONE, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=CLONE, check=True)

    st = updater.check(force=True)
    results.append(("check-available",
                    st.get("available") is True and st.get("behind") == 1
                    and st.get("remote_version") == "1.1.0", st))

    # 脏工作区拒绝更新
    with open(os.path.join(WORK, "pyproject.toml"), "a", encoding="utf-8") as f:
        f.write("\n# dirty\n")
    res = updater.apply()
    results.append(("apply-refuse-dirty", res.get("ok") is False, res))
    git("checkout", "--", "pyproject.toml")

    res = updater.apply()
    head = git("rev-parse", "HEAD").strip()
    remote = git("rev-parse", "origin/main").strip()
    new_ver = open(os.path.join(WORK, "ai_idm", "__init__.py"), encoding="utf-8").read()
    results.append(("apply-ok", res.get("ok") is True and head == remote
                    and "1.1.0" in new_ver, res))

    res = updater.rollback()
    old_ver = open(os.path.join(WORK, "ai_idm", "__init__.py"), encoding="utf-8").read()
    results.append(("rollback-ok", res.get("ok") is True and "1.0.0" in old_ver, res))

    # 远方推一个导入就会炸的版本：必须回滚，不能把起不来的代码当成功
    with open(os.path.join(CLONE, "ai_idm", "__init__.py"), "w", encoding="utf-8") as f:
        f.write('__version__ = "9.9.9"\ndef broken(:\n    pass\n')
    subprocess.run(["git", "add", "-A"], cwd=CLONE, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "broken"], cwd=CLONE, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=CLONE, check=True)
    st = updater.check(force=True)
    res = updater.apply()
    now_ver = open(os.path.join(WORK, "ai_idm", "__init__.py"), encoding="utf-8").read()
    results.append(("apply-broken-rolls-back",
                    res.get("ok") is False and "1.0.0" in now_ver
                    and updater.read_state().get("applying") is False, res))

    # 更新进程被杀留下的 applying 锁必须能在冷却后自动解除
    updater.write_state(applying=True, apply_started_at=1.0)
    st = updater.check(force=True)
    results.append(("stale-apply-cleared",
                    st.get("applying") is False, st.get("error")))

    # 另一个更新进程持锁时必须拒绝（跨进程锁）
    lock = updater.home() / "update.lock"
    lock.write_text("999999", encoding="utf-8")
    res = updater.apply()
    results.append(("apply-refuse-locked",
                    res.get("ok") is False and "另一个更新进程" in str(res.get("error")), res))
    lock.unlink()

    print("\n=== 结果 ===")
    allok = True
    for name, ok, detail in results:
        allok &= bool(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name:20} {detail}")
    print("\nALL PASS" if allok else "\nSOME FAILED")
    return 0 if allok else 1


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        for d in (HOME, WORK, ORIGIN, CLONE):
            shutil.rmtree(d, ignore_errors=True)
    raise SystemExit(rc)
