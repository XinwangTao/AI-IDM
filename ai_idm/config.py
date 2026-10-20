"""ai_idm.config — 用户配置：默认值 + 按站点覆盖。

对应逆向发现：IDM 用 `HKCU\\Software\\DownloadManager\\MCN\\<site>\\number`
覆盖单个站点的并发数。这里用 JSON 文件做等价能力，并支持通配后缀。
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse

DEFAULTS = {
    "connections": 8,
    "limit_bps": None,
    "proxy": None,
    "timeout": 30,
    "max_retries": 4,
    "site_connections": {},       # {"example.com": 16, "*.cdn.com": 32}
    # 分时段限速：命中第一条即生效；days 省略表示每天（0=周一）
    "verify_sample": 0,           # 完成后抽样校验次数（0=关闭）
    "seam_verify": True,          # 接缝校验：相邻块重叠 64 字节比对（IDM 的机制）
    "open_range": False,          # 用开区间 Range bytes=N-（严格模拟 IDM，但会浪费带宽）
    "dynamic_chunks": True,       # 动态分块：块数≈连接数×4，归属运行时决定（IDM 的做法）
    "keepalive": True,            # 连接复用：同一 worker 连续取块复用同一条 TCP（IDM 的 Reusing connection.）
    "speed_schedule": [],         # [{"days":[0,1,2,3,4],"start":"09:00","end":"18:00","limit_bps":1048576}]
}


def home():
    h = os.environ.get("AI_IDM_HOME") or os.environ.get("IDMCLONE_HOME")
    if not h:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        h = os.path.join(base, "ai-idm")
    return h


def config_path():
    return os.path.join(home(), "config.json")


BOOL_KEYS = ("seam_verify", "open_range", "dynamic_chunks", "keepalive")


def _coerce_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() not in ("0", "false", "no", "off", "none", "")


def load(path=None):
    p = path or config_path()
    cfg = dict(DEFAULTS)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
            cfg["site_connections"] = dict(data.get("site_connections") or {})
    except (OSError, json.JSONDecodeError):
        pass
    # `config set seam_verify=false` 写进来是字符串，这里统一成 bool
    for k in BOOL_KEYS:
        cfg[k] = _coerce_bool(cfg.get(k, DEFAULTS[k]))
    return cfg


def save(cfg, path=None):
    p = path or config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return p


def parse_hm(text):
    hh, _, mm = str(text).partition(":")
    return int(hh) * 60 + int(mm or 0)


def schedule_limit(schedule, now=None):
    """返回当前时段应生效的限速（bytes/s）；没有命中返回 None。

    支持跨天区间（start > end），例如 23:00-07:00。
    """
    if not schedule:
        return None
    from datetime import datetime
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    for rule in schedule:
        days = rule.get("days")
        if days is not None and now.weekday() not in days:
            continue
        try:
            start = parse_hm(rule["start"])
            end = parse_hm(rule["end"])
        except (KeyError, ValueError):
            continue
        hit = (start <= cur < end) if start <= end else (cur >= start or cur < end)
        if hit:
            return rule.get("limit_bps")
    return None


def host_of(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def match_site(host, mapping):
    """精确匹配优先，其次是最长的通配后缀（*.example.com）。"""
    if not host:
        return None
    if host in mapping:
        return mapping[host]
    best = None
    for pattern, value in mapping.items():
        if pattern.startswith("*."):
            suffix = pattern[1:]           # ".example.com"
            if host.endswith(suffix) and (best is None or len(pattern) > len(best[0])):
                best = (pattern, value)
    return best[1] if best else None


def resolve(url, cfg, connections=None, limit_bps=None, proxy=None,
            timeout=None, max_retries=None, now=None):
    """CLI 显式值 > 时段限速 > 站点覆盖 > 全局默认。"""
    site = match_site(host_of(url), cfg.get("site_connections") or {})
    sched = schedule_limit(cfg.get("speed_schedule") or [], now)
    out = {
        "connections": connections if connections is not None
        else (site if site is not None else cfg.get("connections", 8)),
        "limit_bps": (limit_bps if limit_bps is not None
                      else (sched if sched is not None else cfg.get("limit_bps"))),
        "proxy": proxy if proxy is not None else cfg.get("proxy"),
        "timeout": timeout if timeout is not None else cfg.get("timeout", 30),
        "max_retries": max_retries if max_retries is not None else cfg.get("max_retries", 4),
    }
    try:
        out["connections"] = max(1, int(out["connections"]))
    except (TypeError, ValueError):
        out["connections"] = 8
    return out
