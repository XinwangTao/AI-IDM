#!/usr/bin/env python3
"""跑全部测试套件：python tests/run_all.py [-k 关键字]

每个套件都是独立进程（有的会起子进程、改环境变量），所以这里逐个拉起，
最后汇总。CI 用的就是这一条命令。
"""

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    from ai_idm.stdio import ensure_utf8_stdio
    ensure_utf8_stdio()          # 汇总行含中文，非 UTF-8 控制台会崩
except Exception:
    pass
TESTS = [
    "test_local",        # 分片 / 续传 / 降级 / MCP 协议
    "test_queue_proxy",  # SQLite 队列 / SOCKS5 隧道
    "test_hls",          # m3u8 解析 / 纯 Python AES-128 / 合并
    "test_config",       # 配置层 / 按站点并发
    "test_steal",        # 失败分块移交
    "test_schedule",     # 分时段限速（运行中热切换）
    "test_ftp",          # FTP / FTPS 多连接 + REST 续传
    "test_verify",       # 抽样完整性校验
    "test_seam",         # 接缝 64 字节比对 / Range 写法 / 未开始块移交
    "test_dynamic",      # 动态分块 vs 静态分段
    "test_pool",         # keep-alive 连接复用
    "test_stdio",        # 非 UTF-8 控制台（cp1252）不崩
]


def main(argv):
    pattern = None
    if "-k" in argv:
        pattern = argv[argv.index("-k") + 1]
    names = [t for t in TESTS if not pattern or pattern in t]
    if not names:
        print(f"没有匹配 {pattern!r} 的测试套件")
        return 1

    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")
    results = []
    t_all = time.time()
    for name in names:
        path = os.path.join(ROOT, "tests", name + ".py")
        t0 = time.time()
        proc = subprocess.run([sys.executable, path], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env=env, cwd=ROOT)
        dt = time.time() - t0
        ok = proc.returncode == 0
        results.append((name, ok, dt, proc.stdout))
        print(f"{'PASS' if ok else 'FAIL'}  {name:18} {dt:6.1f}s")
        if not ok:
            tail = (proc.stdout or "")[-1500:]
            err = (proc.stderr or "")[-800:]
            print("---- stdout ----")
            print(tail)
            if err.strip():
                print("---- stderr ----")
                print(err)

    failed = [r[0] for r in results if not r[1]]
    print("\n%d/%d 套件通过，总耗时 %.1fs"
          % (len(results) - len(failed), len(results), time.time() - t_all))
    if failed:
        print("失败：" + ", ".join(failed))
        return 1
    print("ALL SUITES PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
