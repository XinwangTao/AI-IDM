"""控制台编码兜底。

Windows 的控制台代码页不一定是 UTF-8（英文系统常见 cp1252，中文系统常见 cp936）。
当输出里含中文时，`print` / `argparse.print_help` 会直接抛 UnicodeEncodeError，
进程带 traceback 退出——对使用者来说就是"跑不起来"。

这里在入口处把 stdout/stderr 重设成 UTF-8；重设失败也不致命，退化成
`errors="replace"` 保证不因为编码问题中断。
"""

from __future__ import annotations

import sys


def ensure_utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # 老版本 / 被替换过的流：至少别让编码错误把进程打死
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass
