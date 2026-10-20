"""控制台编码回归：非 UTF-8 代码页（Windows 英文系统常见 cp1252）下不能崩。

这是 CI 在 windows-latest 上抓到的真实 bug：中文帮助文本在 cp1252 控制台
抛 UnicodeEncodeError，进程带 traceback 退出。修复后再用这里锁住。
"""

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    from ai_idm.stdio import ensure_utf8_stdio
    ensure_utf8_stdio()          # 输出含中文，非 UTF-8 控制台会崩
except Exception:
    pass



def run(args, encoding="cp1252", timeout=60):
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING=encoding)
    # 子进程会把自己切到 UTF-8，所以按 utf-8 解码；PYTHONIOENCODING 只用来模拟"非 UTF-8 控制台"
    return subprocess.run([sys.executable, "-m", "ai_idm"] + args,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=ROOT, timeout=timeout)


def main():
    results = []

    # ---- 1. --help 在 cp1252 下不能崩 ----
    for args in (["--help"], ["get", "--help"], ["config", "--help"]):
        p = run(args)
        ok = p.returncode == 0 and "UnicodeEncodeError" not in (p.stderr or "")
        results.append(("help-" + args[0].strip("-"), ok,
                        {"rc": p.returncode, "err": (p.stderr or "")[:60]}))

    # ---- 2. MCP 在 cp1252 下能正常握手（JSON 里含中文）----
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="cp1252")
    err = ""
    proc = subprocess.Popen([sys.executable, "-m", "ai_idm", "mcp"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            env=env, cwd=ROOT)
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}},
        }) + "\n")
        proc.stdin.flush()
        info = json.loads(proc.stdout.readline())["result"]["serverInfo"]
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2,
                                     "method": "tools/list", "params": {}}) + "\n")
        proc.stdin.flush()
        tools = json.loads(proc.stdout.readline())["result"]["tools"]
        ok = info["name"] == "ai-idm" and len(tools) >= 10
        results.append(("mcp-cp1252", ok, {"tools": len(tools)}))
    finally:
        proc.stdin.close()
        err = proc.stderr.read()
        proc.wait(timeout=10)

    print("\n=== 结果 ===")
    allok = True
    for name, ok, detail in results:
        allok &= bool(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name:18} {detail}")
    if err.strip():
        print("mcp stderr:", err.strip()[:300])
    print("\nALL PASS" if allok else "\nSOME FAILED")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
