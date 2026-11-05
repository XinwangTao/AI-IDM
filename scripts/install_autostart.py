#!/usr/bin/env python3
"""登记 / 移除 AI-IDM 接管服务的开机自启（Windows 启动文件夹 VBS）。

用法：
    python scripts/install_autostart.py           # 安装（已存在则覆盖）
    python scripts/install_autostart.py --remove  # 移除
    python scripts/install_autostart.py --status  # 查看状态

说明：
- 写一个 VBS 到当前用户的启动文件夹，用 `wscript //b` 静默拉起 `ai-idm serve`；
- 服务自身有单实例保护：自启和手动启动同时发生时，第二个实例会安静退出。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "AI-IDM-Serve.vbs"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def launcher() -> list[str]:
    exe = ROOT / ".venv" / "Scripts" / "ai-idm.exe"
    if exe.is_file():
        return [str(exe), "serve"]
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    if py.is_file():
        return [str(py), "-m", "ai_idm", "serve"]
    return [sys.executable, "-m", "ai_idm", "serve"]


def vbs_text() -> str:
    args = launcher()
    cmd = " ".join(f'"{a}"' if " " in a else a for a in args)
    quoted = cmd.replace('"', '""')          # VBS 字符串里的字面引号要写成两个
    return (
        '\' AI-IDM 接管服务开机自启（由 scripts/install_autostart.py 生成）\r\n'
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Run "{quoted}", 0, False\r\n'
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="AI-IDM 开机自启")
    ap.add_argument("--remove", action="store_true", help="移除自启")
    ap.add_argument("--status", action="store_true", help="查看状态")
    args = ap.parse_args()

    target = startup_dir() / NAME
    if args.status:
        print(f"自启项：{target}")
        print("状态：已安装" if target.is_file() else "状态：未安装")
        return 0

    if args.remove:
        if target.is_file():
            target.unlink()
            print(f"已移除：{target}")
        else:
            print("未安装，无需移除")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(vbs_text(), encoding="utf-8")
    print(f"已安装自启：{target}")
    print(f"启动命令：{' '.join(launcher())}")
    print("下次登录自动运行；现在想立即启动可执行：ai-idm serve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
