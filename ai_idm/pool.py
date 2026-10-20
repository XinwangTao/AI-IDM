"""ai_idm.pool — keep-alive 连接池（对齐 IDM 的 `Reusing connection.`）。

为什么需要它：动态分块会让同一条连接连续取好几块，如果每块都重新
TCP+TLS 握手（走代理时还要再 CONNECT 一次），开销会吃掉分块带来的收益。
IDM 日志里的 `next %d` + `Reusing connection.` 就是复用同一条 TCP 连续取块。

约束：只有把响应体**读完**才能把连接放回池子。所以这个池子只适合
闭区间 Range（本项目默认）——开区间模式下我们够数就断，连接只能丢弃。

支持：直连 http/https、http(s) 代理（CONNECT 隧道）、SOCKS5/SOCKS5H。
"""

from __future__ import annotations

import base64
import http.client
import ssl
import threading
from urllib.parse import urljoin, urlsplit

from . import proxy as proxymod

REDIRECT_CODES = (301, 302, 303, 307, 308)


class PoolError(RuntimeError):
    pass


class Stream:
    """一次请求的响应流；退出时可决定连接是否回池。"""

    __slots__ = ("status", "headers", "_pool", "_key", "_conn", "_resp", "_url", "_done")

    def __init__(self, pool, key, conn, resp, url):
        self._pool = pool
        self._key = key
        self._conn = conn
        self._resp = resp
        self._url = url
        self.status = resp.status
        self.headers = resp.headers
        self._done = False

    def read(self, n=-1):
        data = self._resp.read(n)
        if not data:
            self._done = True
        return data

    def geturl(self):
        return self._url

    @property
    def reused(self):
        return getattr(self._conn, "_idm_reused", False)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # 只有"读完 + 没有异常 + 服务端不要求关闭"才回池
        complete = self._done or self._resp.isclosed() or getattr(self._resp, "length", None) == 0
        self._pool._release(self._key, self._conn, self._resp,
                            ok=(exc_type is None and complete))
        return False


class Pool:
    def __init__(self, proxy=None, insecure=False, timeout=30, max_idle=16):
        self.timeout = timeout
        self.max_idle = max_idle
        self.spec = proxy if isinstance(proxy, proxymod.ProxySpec) or proxy is None \
            else proxymod.parse(proxy)
        self._ctx = ssl.create_default_context()
        if insecure:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE
        self._lock = threading.Lock()
        self._idle = {}          # key -> http.client.HTTPConnection
        self.opened = 0          # 累计新建的连接数（诊断用）
        self.reused = 0          # 累计复用次数（诊断用）

    # ---------------- 建链 ----------------

    def _proxy_auth(self):
        spec = self.spec
        if not spec or not spec.user:
            return None
        token = base64.b64encode(
            f"{spec.user}:{spec.password or ''}".encode()).decode()
        return "Basic " + token

    def _new_conn(self, scheme, host, port):
        t = self.timeout
        spec = self.spec
        if spec is None:
            if scheme == "https":
                return http.client.HTTPSConnection(host, port, timeout=t, context=self._ctx)
            return http.client.HTTPConnection(host, port, timeout=t)

        if spec.scheme in ("socks5", "socks5h"):
            if scheme == "https":
                return proxymod._SocksHTTPSConnection(
                    host, port, timeout=t, context=self._ctx, proxy=spec)
            return proxymod._SocksHTTPConnection(host, port, timeout=t, proxy=spec)

        # http(s) 代理
        if scheme == "https":
            conn = http.client.HTTPSConnection(spec.host, spec.port, timeout=t,
                                               context=self._ctx)
            tunnel_headers = {}
            auth = self._proxy_auth()
            if auth:
                tunnel_headers["Proxy-Authorization"] = auth
            conn.set_tunnel(host, port, headers=tunnel_headers or None)
            return conn
        return http.client.HTTPConnection(spec.host, spec.port, timeout=t)

    def _route(self, url):
        """返回 (scheme, host, port, request_target, extra_headers)"""
        u = urlsplit(url)
        scheme = (u.scheme or "http").lower()
        host = u.hostname
        port = u.port or (443 if scheme == "https" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        extra = {}
        spec = self.spec
        if spec is not None and spec.scheme in ("http", "https") and scheme == "http":
            # 走 http 代理访问 http 目标：请求行用绝对 URI
            path = f"http://{host}{'' if port == 80 else ':' + str(port)}{path}"
            auth = self._proxy_auth()
            if auth:
                extra["Proxy-Authorization"] = auth
        return scheme, host, port, path, extra

    def _get_conn(self, key, scheme, host, port):
        # 一个 key 对应一组空闲连接：多个 worker 同时归还时不能互相覆盖
        with self._lock:
            bucket = self._idle.get(key) or []
            while bucket:
                conn = bucket.pop()
                if conn.sock is not None:
                    conn._idm_reused = True
                    self.reused += 1
                    return conn
                try:
                    conn.close()
                except Exception:
                    pass
        conn = self._new_conn(scheme, host, port)
        conn._idm_reused = False
        with self._lock:
            self.opened += 1
        return conn

    def _release(self, key, conn, resp, ok):
        reusable = ok
        if reusable and getattr(conn, "will_close", False):
            reusable = False
        if reusable:
            with self._lock:
                bucket = self._idle.setdefault(key, [])
                room = len(bucket) < self.max_idle
                if room:
                    conn._idm_reused = False
                    bucket.append(conn)
            if room:
                return
        try:
            resp.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    @staticmethod
    def _send(conn, method, target, hdrs):
        conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        for k, v in hdrs.items():
            conn.putheader(k, v)
        conn.endheaders()
        return conn.getresponse()

    # ---------------- 请求 ----------------

    def stream(self, url, headers, method="GET", max_redirects=4):
        """发起请求并返回 Stream；重定向自动跟随。"""
        for _ in range(max_redirects + 1):
            scheme, host, port, target, extra = self._route(url)
            key = (scheme, host, port)
            conn = self._get_conn(key, scheme, host, port)
            hdrs = dict(headers)
            hdrs.update(extra)
            hdrs.setdefault("Host", host if port in (80, 443) else f"{host}:{port}")
            hdrs.setdefault("Accept-Encoding", "identity")

            try:
                resp = self._send(conn, method, target, hdrs)
            except (http.client.RemoteDisconnected, http.client.BadStatusLine,
                    BrokenPipeError, ConnectionResetError):
                # 池子里的连接被服务端空闲超时关掉了：换新连接立刻重试一次
                try:
                    conn.close()
                except Exception:
                    pass
                if not getattr(conn, "_idm_reused", False):
                    raise
                conn = self._get_conn(key, scheme, host, port)
                try:
                    resp = self._send(conn, method, target, hdrs)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    raise
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                raise

            if resp.status in REDIRECT_CODES and resp.getheader("Location"):
                loc = urljoin(url, resp.getheader("Location"))
                self._release(key, conn, resp, ok=False)   # 重定向响应体不读，直接弃链
                url = loc
                continue
            return Stream(self, key, conn, resp, url)
        raise PoolError("重定向次数过多")

    def close(self):
        with self._lock:
            conns = [c for bucket in self._idle.values() for c in bucket]
            self._idle.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass

    @property
    def stats(self):
        with self._lock:
            return {"opened": self.opened, "reused": self.reused,
                    "idle": sum(len(b) for b in self._idle.values())}
