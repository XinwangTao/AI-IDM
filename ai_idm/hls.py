"""ai_idm.hls — HLS(m3u8) 下载与合并。

支持：
- master playlist 选择码率（best / worst / 指定 index）
- 分片并发展开（复用 engine.Download）
- EXT-X-MAP 初始化段
- AES-128 加密分片（纯 Python AES-CBC 实现，无第三方依赖）
- 断点续传（分片文件已存在且非空则跳过）

不支持的：EXT-X-BYTERANGE、SAMPLE-AES。
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

from .engine import Download, DownloadError, _make_opener, DEFAULT_UA


class HlsError(RuntimeError):
    pass


# ---------------------------------------------------------------- AES-128

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")
_INV_SBOX = bytes(256)
_tmp = bytearray(256)
for _i, _v in enumerate(_SBOX):
    _tmp[_v] = _i
_INV_SBOX = bytes(_tmp)
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x11B) & 0xFF
    return a


def _gmul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        a = _xtime(a)
        b >>= 1
    return p


def _expand_key(key):
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[x] for x in t]
            t[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return w


def _decrypt_block(block, w):
    s = list(block)
    for i in range(16):
        s[i] ^= w[40 + i // 4][i % 4]
    for rnd in range(9, -1, -1):
        # InvShiftRows
        s = [s[i] for i in (0, 13, 10, 7, 4, 1, 14, 11, 8, 5, 2, 15, 12, 9, 6, 3)]
        # InvSubBytes
        s = [_INV_SBOX[x] for x in s]
        # AddRoundKey
        for i in range(16):
            s[i] ^= w[rnd * 4 + i // 4][i % 4]
        if rnd == 0:
            break
        # InvMixColumns
        out = [0] * 16
        for c in range(4):
            a0, a1, a2, a3 = s[c * 4:c * 4 + 4]
            out[c * 4 + 0] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
            out[c * 4 + 1] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
            out[c * 4 + 2] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
            out[c * 4 + 3] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)
        s = out
    return bytes(s)


def aes128_cbc_decrypt(key, iv, data):
    if len(key) != 16:
        raise HlsError("AES-128 需要 16 字节密钥")
    w = _expand_key(key)
    if len(data) % 16:
        raise HlsError("密文长度不是 16 的倍数")
    prev = iv
    out = bytearray()
    for i in range(0, len(data), 16):
        blk = data[i:i + 16]
        plain = _decrypt_block(blk, w)
        out += bytes(a ^ b for a, b in zip(plain, prev))
        prev = blk
    # PKCS#7 去填充
    if out and 1 <= out[-1] <= 16:
        pad = out[-1]
        if all(b == pad for b in out[-pad:]):
            del out[-pad:]
    return bytes(out)


def _encrypt_block(block, w):
    s = list(block)
    for i in range(16):
        s[i] ^= w[i // 4][i % 4]
    for rnd in range(1, 11):
        s = [_SBOX[x] for x in s]
        s = [s[i] for i in (0, 5, 10, 15, 4, 9, 14, 3, 8, 13, 2, 7, 12, 1, 6, 11)]
        if rnd != 10:
            out = [0] * 16
            for c in range(4):
                a0, a1, a2, a3 = s[c * 4:c * 4 + 4]
                out[c * 4 + 0] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
                out[c * 4 + 1] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
                out[c * 4 + 2] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
                out[c * 4 + 3] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)
            s = out
        for i in range(16):
            s[i] ^= w[rnd * 4 + i // 4][i % 4]
    return bytes(s)


def aes128_cbc_encrypt(key, iv, data, pad=True):
    """仅用于自测/生成样例，HLS 下载侧只做解密。"""
    if len(key) != 16:
        raise HlsError("AES-128 需要 16 字节密钥")
    w = _expand_key(key)
    if pad:
        n = 16 - (len(data) % 16)
        data = data + bytes([n]) * n
    prev = iv
    out = bytearray()
    for i in range(0, len(data), 16):
        blk = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
        prev = _encrypt_block(blk, w)
        out += prev
    return bytes(out)


# ---------------------------------------------------------------- 播放列表

def _attrs(s):
    """解析 KEY=VALUE,KEY="VALUE" 形式。"""
    out = {}
    for m in re.finditer(r'([A-Z0-9\-]+)=("[^"]*"|[^,]*)', s):
        out[m.group(1)] = m.group(2).strip('"')
    return out


def is_master(text):
    return "#EXT-X-STREAM-INF" in text


def parse_master(text, base_url):
    variants = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _attrs(line.split(":", 1)[1])
            uri = ""
            for nxt in lines[i + 1:]:
                if not nxt.startswith("#"):
                    uri = nxt
                    break
            if uri:
                variants.append({
                    "bandwidth": int(attrs.get("BANDWIDTH") or 0),
                    "resolution": attrs.get("RESOLUTION", ""),
                    "codecs": attrs.get("CODECS", ""),
                    "url": urljoin(base_url, uri),
                })
    return sorted(variants, key=lambda v: v["bandwidth"])


def parse_media(text, base_url):
    segs = []
    key = None
    init_map = None
    media_seq = 0
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-KEY:"):
            a = _attrs(line.split(":", 1)[1])
            if a.get("METHOD", "NONE") == "NONE":
                key = None
            else:
                key = {
                    "method": a.get("METHOD"),
                    "url": urljoin(base_url, a.get("URI", "")),
                    "iv": a.get("IV"),
                }
        elif line.startswith("#EXT-X-MAP:"):
            a = _attrs(line.split(":", 1)[1])
            init_map = urljoin(base_url, a.get("URI", ""))
        elif not line.startswith("#"):
            segs.append({
                "url": urljoin(base_url, line),
                "key": key,
                "seq": media_seq + len(segs),
            })
    if not segs:
        raise HlsError("媒体列表里没有分片")
    return {"segments": segs, "init": init_map, "encrypted": any(s["key"] for s in segs)}


# ---------------------------------------------------------------- 下载

def _fetch_text(url, headers, proxy, insecure, timeout=30):
    opener = _make_opener(proxy, insecure)
    req = __import__("urllib.request", fromlist=["Request"]).Request(
        url, headers={"User-Agent": DEFAULT_UA, **(headers or {})})
    with opener.open(req, timeout=timeout) as r:
        raw = r.read()
        return raw.decode("utf-8", "replace"), r.geturl()


def _fetch_bytes(url, headers, proxy, insecure, timeout=60):
    opener = _make_opener(proxy, insecure)
    req = __import__("urllib.request", fromlist=["Request"]).Request(
        url, headers={"User-Agent": DEFAULT_UA, **(headers or {})})
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def download_hls(url, dest, connections=8, quality="best", headers=None,
                 proxy=None, insecure=False, keep_segments=False,
                 restart=False, progress=None):
    headers = headers or {}
    text, final_url = _fetch_text(url, headers, proxy, insecure)

    variant_info = None
    if is_master(text):
        variants = parse_master(text, final_url)
        if not variants:
            raise HlsError("master playlist 里没有可用码率")
        if quality == "best":
            variant_info = variants[-1]
        elif quality == "worst":
            variant_info = variants[0]
        else:
            try:
                variant_info = variants[int(quality)]
            except (ValueError, IndexError):
                raise HlsError(f"无效的 quality: {quality}（共 {len(variants)} 个码率）")
        text, final_url = _fetch_text(variant_info["url"], headers, proxy, insecure)

    media = parse_media(text, final_url)
    segs = media["segments"]

    if not dest:
        dest = os.path.basename(final_url.split("?")[0]) or "output.ts"
        if not os.path.splitext(dest)[1]:
            dest += ".ts"
    dest = os.path.abspath(dest)
    work = dest + ".hlsseg"
    if restart and os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work, exist_ok=True)

    key_cache = {}
    seg_paths = [os.path.join(work, f"seg_{i:05d}.bin") for i in range(len(segs))]
    done_count = [0]
    lock = threading.Lock()

    def fetch_one(i):
        path = seg_paths[i]
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with lock:
                done_count[0] += 1
                if progress:
                    progress(done_count[0], len(segs))
            return
        seg = segs[i]
        if seg["key"]:
            k = seg["key"]
            if k["method"] != "AES-128":
                raise HlsError(f"不支持的分片加密方式: {k['method']}")
            if k["url"] not in key_cache:
                key_cache[k["url"]] = _fetch_bytes(k["url"], headers, proxy, insecure, 30)
            key = key_cache[k["url"]]
            if len(key) != 16:
                raise HlsError(f"密钥长度异常: {len(key)}")
            if k.get("iv"):
                iv = bytes.fromhex(k["iv"][2:] if k["iv"].lower().startswith("0x") else k["iv"])
            else:
                iv = struct.pack(">QQ", 0, seg["seq"])
            blob = _fetch_bytes(seg["url"], headers, proxy, insecure, 120)
            blob = aes128_cbc_decrypt(key, iv, blob)
            with open(path, "wb") as f:
                f.write(blob)
        else:
            # 复用分片下载器（单连接，单片内部不分段）
            Download(seg["url"], dest=path, connections=1, headers=headers,
                     proxy=proxy, insecure=insecure).run()
        with lock:
            done_count[0] += 1
            if progress:
                progress(done_count[0], len(segs))

    errors = []

    def safe_fetch(i):
        try:
            fetch_one(i)
        except Exception as e:                       # noqa: BLE001
            errors.append((i, str(e)))

    with ThreadPoolExecutor(max_workers=max(1, min(connections, len(segs)))) as ex:
        list(ex.map(safe_fetch, range(len(segs))))

    if errors:
        raise HlsError(f"{len(errors)} 个分片失败，首个: 分片 {errors[0][0]} {errors[0][1]}")

    tmp_out = dest + ".part"
    with open(tmp_out, "wb") as out:
        if media["init"]:
            out.write(_fetch_bytes(media["init"], headers, proxy, insecure, 60))
        for p in seg_paths:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out, 1 << 20)
    os.replace(tmp_out, dest)

    size = os.path.getsize(dest)
    if not keep_segments:
        shutil.rmtree(work, ignore_errors=True)

    return {
        "dest": dest,
        "size": size,
        "segments": len(segs),
        "encrypted": media["encrypted"],
        "variant": variant_info,
        "init_segment": media["init"],
    }
