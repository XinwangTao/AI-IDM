"""ai_idm.proxy — 代理支持：HTTP 代理、SOCKS5、SOCKS5H、Windows 系统代理。

urllib 原生只支持 HTTP 代理，这里补一个 SOCKS5 的 connection 层：
握手完成后直接复用 http.client 的其余逻辑，HTTPS 再在隧道上套 TLS。
"""

from __future__ import annotations

import functools
import http.client
import socket
import ssl
import struct
import urllib.request


class ProxyError(RuntimeError):
    pass


class ProxySpec:
    __slots__ = ("scheme", "host", "port", "user", "password")

    def __init__(self, scheme, host, port, user=None, password=None):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.user = user
        self.password = password

    def __str__(self):
        return f"{self.scheme}://{self.host}:{self.port}"


def parse(spec):
    """'http://h:8080' / 'socks5://u:p@h:1080' / 'socks5h://h:1080' / 'auto'"""
    if not spec:
        return None
    if spec.strip().lower() == "auto":
        return system_proxy()
    s = spec.strip()
    if "://" not in s:
        s = "http://" + s
    scheme, rest = s.split("://", 1)
    scheme = scheme.lower()
    user = password = None
    if "@" in rest:
        auth, rest = rest.rsplit("@", 1)
        if ":" in auth:
            user, password = auth.split(":", 1)
        else:
            user = auth
    if ":" in rest:
        host, port = rest.rsplit(":", 1)
        port = int(port)
    else:
        host, port = rest, {"http": 8080, "https": 8080,
                            "socks5": 1080, "socks5h": 1080}.get(scheme, 8080)
    if scheme not in ("http", "https", "socks5", "socks5h"):
        raise ProxyError(f"不支持的代理协议: {scheme}")
    return ProxySpec(scheme, host, port, user, password)


def system_proxy():
    """读取 Windows 的 IE/系统代理设置；没有则返回 None。"""
    try:
        import winreg
    except ImportError:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if not enabled:
            return None
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None
    finally:
        try:
            winreg.CloseKey(key)
        except Exception:
            pass
    if not server:
        return None
    # 形如 "http=1.2.3.4:8080;https=1.2.3.4:8080"
    if "=" in server:
        parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
        server = parts.get("https") or parts.get("http") or next(iter(parts.values()))
    return parse("http://" + server.strip())


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ProxyError("SOCKS5 连接被对端关闭")
        buf += chunk
    return buf


def _socks5_tunnel(spec, host, port, timeout, source_address=None):
    sock = socket.create_connection((spec.host, spec.port), timeout, source_address)
    try:
        if spec.user:
            sock.sendall(b"\x05\x02\x00\x02")
        else:
            sock.sendall(b"\x05\x01\x00")
        ver, method = _recv_exact(sock, 2)
        if ver != 5:
            raise ProxyError("不是 SOCKS5 代理")
        if method == 0x02:
            u = (spec.user or "").encode()
            p = (spec.password or "").encode()
            sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            _, status = _recv_exact(sock, 2)
            if status != 0:
                raise ProxyError("SOCKS5 认证失败")
        elif method != 0x00:
            raise ProxyError(f"SOCKS5 不接受无认证方式: {method}")

        if spec.scheme == "socks5h":
            addr = b"\x03" + bytes([len(host)]) + host.encode("idna")
        else:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
            family, _, _, _, sa = infos[0]
            if family == socket.AF_INET:
                addr = b"\x01" + socket.inet_aton(sa[0])
            elif family == socket.AF_INET6:
                addr = b"\x04" + socket.inet_pton(socket.AF_INET6, sa[0])
            else:
                addr = b"\x03" + bytes([len(host)]) + host.encode("idna")

        sock.sendall(b"\x05\x01\x00" + addr + struct.pack(">H", port))
        head = _recv_exact(sock, 4)
        if head[0] != 5 or head[1] != 0:
            raise ProxyError(f"SOCKS5 CONNECT 失败，错误码 {head[1]}")
        atyp = head[3]
        if atyp == 0x01:
            _recv_exact(sock, 4 + 2)
        elif atyp == 0x03:
            ln = _recv_exact(sock, 1)[0]
            _recv_exact(sock, ln + 2)
        elif atyp == 0x04:
            _recv_exact(sock, 16 + 2)
        return sock
    except Exception:
        sock.close()
        raise


class _SocksHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *a, proxy=None, **kw):
        self._proxy = proxy
        super().__init__(*a, **kw)

    def connect(self):
        self.sock = _socks5_tunnel(self._proxy, self.host, self.port,
                                   self.timeout, self.source_address)


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *a, proxy=None, **kw):
        self._proxy = proxy
        super().__init__(*a, **kw)

    def connect(self):
        sock = _socks5_tunnel(self._proxy, self.host, self.port,
                              self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _SocksHandler(urllib.request.AbstractHTTPHandler):
    # 必须早于 urllib 内置的 HTTP(S)Handler（默认 500），否则请求会绕过隧道
    handler_order = 100

    def __init__(self, spec, insecure=False):
        super().__init__()
        self.spec = spec
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._ctx = ctx

    def http_open(self, req):
        return self.do_open(functools.partial(_SocksHTTPConnection, proxy=self.spec), req)

    def https_open(self, req):
        return self.do_open(
            functools.partial(_SocksHTTPSConnection, proxy=self.spec, context=self._ctx), req)


def build_opener(proxy=None, insecure=False):
    """统一入口：None / 'auto' / http(s):// / socks5(h)://"""
    spec = proxy if isinstance(proxy, ProxySpec) or proxy is None else parse(proxy)
    handlers = []
    if isinstance(spec, ProxySpec):
        if spec.scheme in ("socks5", "socks5h"):
            handlers.append(_SocksHandler(spec, insecure))
        else:
            handlers.append(urllib.request.ProxyHandler(
                {"http": str(spec), "https": str(spec)}))
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers), spec
