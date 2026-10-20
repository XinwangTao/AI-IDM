"""idmclone — 分片多连接下载器（CLI + MCP）。"""

__version__ = "1.10.0"

from .engine import Download, DownloadError, probe  # noqa: F401
