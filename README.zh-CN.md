# AI-IDM

[![CI](https://github.com/XinwangTao/AI-IDM/actions/workflows/ci.yml/badge.svg)](https://github.com/XinwangTao/AI-IDM/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

**AI-IDM** 是一个**零第三方依赖**（纯 Python 标准库）的多连接下载引擎。支持 HTTP/HTTPS、HLS（`m3u8`，含 AES-128
加密分片）、FTP/FTPS，支持断点续传，并且自带 **MCP 服务器**，可以被 AI 直接调用。

> English: [README.md](README.md)

```
$ ai-idm get https://example.com/big.iso -o ~/Downloads/big.iso -n 8
  big.iso  100.0%  8 连接  12.4MB/s  6.3s 完成
```

## 能力

| 方面 | 说明 |
|---|---|
| **多连接** | `Range` 分片，默认 8 连接，最小块 1MB |
| **动态分块** | 块数约为连接数的 4 倍，由工作队列在运行时分配——一条慢连接拖不住整个下载 |
| **连接复用** | keep-alive 连接池：同一 worker 连续取多块只握一条 TCP/TLS，不重复握手 |
| **断点续传** | 进度写 `.idmpart.json`，数据写 `<dest>.idmpart`，完成后 `os.replace()` 改名——目标路径不会出现半成品 |
| **完整性** | 相邻块保留 64 字节重叠逐字节比对；可再开启下载完成后抽样校验（`--verify-sample N`） |
| **自动降级** | 服务端忽略 `Range` → 自动改单连接整取 |
| **限速** | 令牌桶；支持分时段限速，且**下载过程中**跨时段自动切换 |
| **按站点并发** | `config set-site example.com 24`，支持通配（`*.cdn.com`） |
| **代理** | HTTP/HTTPS 代理、SOCKS5、SOCKS5H（远端解析 DNS）、Windows 系统代理 |
| **HLS** | master 选码率、`EXT-X-MAP`、纯 Python AES-128-CBC、可续传 |
| **FTP / FTPS** | 多连接（`REST` 续传）、`AUTH TLS` + `PROT P` |
| **任务队列** | SQLite 持久化、原子领取、进程重启不丢、并行消费 |
| **可被 AI 调用** | MCP stdio 服务器，12 个工具（`start_download` / `wait_download` / `queue_*` / `download_hls` 等） |
| **浏览器接管** | `ai-idm serve` + Chrome/Edge 扩展：浏览器点下载即由本引擎接手，失败自动回退浏览器 |

## 安装

```bash
git clone https://github.com/XinwangTao/AI-IDM.git
cd AI-IDM
pip install -e .

# 或者不安装，直接用模块方式运行
python -m ai_idm get https://example.com/file.zip
```

要求 Python 3.10+，无运行时依赖。

## 命令行

```bash
# 8 连接下载，完成后算 sha256
ai-idm get https://example.com/file.zip -o file.zip -n 8 --checksum sha256

# 只看不动：大小 / 是否支持 Range / ETag / 文件名
ai-idm probe https://example.com/file.zip

# 中断了就再跑一遍同样的命令——会续传
ai-idm get https://example.com/file.zip -o file.zip

# 限速 2MB/s，或走代理
ai-idm get https://example.com/file.zip --limit 2M --proxy socks5h://127.0.0.1:1080

# HLS：选最高码率，下完分片合并
ai-idm hls https://example.com/master.m3u8 -o video.ts --quality best

# FTP / FTPS
ai-idm get ftp://user:pass@host/path/file.iso -o file.iso -n 4

# 持久化队列
ai-idm queue add https://example.com/a.zip -o a.zip -n 8 --priority 5
ai-idm queue run --parallel 3
ai-idm queue list

# 配置
ai-idm config show
ai-idm config set connections=16 timeout=45
ai-idm config set-site example.com 24
ai-idm config schedule add "09:00-18:00=1M" --days mon,tue,wed,thu,fri
```

每条命令都输出 JSON，方便和 `jq`、脚本组合。

## MCP 服务器（给 AI 用）

`ai-idm mcp` 通过 stdio 讲 MCP。客户端里这样注册：

```json
{
  "mcpServers": {
    "idm": {
      "command": "python",
      "args": ["-m", "ai_idm", "mcp"],
      "env": { "PYTHONPATH": "/path/to/AI-IDM" }
    }
  }
}
```

工具清单：`probe_url`、`start_download`、`download_status`、`wait_download`、`cancel_download`、
`list_downloads`、`queue_add`、`queue_run`、`queue_list`、`queue_remove`、`queue_retry`、
`download_hls`。

之后就可以直接说「把这几个链接下到 D:\dl，4 个并行，下完告诉我哪个失败」，由模型自己调工具。

## 浏览器接管（点下载就用本引擎）

跟 IDM 同构：浏览器扩展 → 本机回环 HTTP 服务 → 下载引擎。

```bash
ai-idm serve
#   地址    : http://127.0.0.1:13391
#   令牌    : 3Qk...（自动生成并存进 config.json）
#   下载目录: C:\Users\you\Downloads
#   面板    : http://127.0.0.1:13391/?token=...
```

1. `chrome://extensions` → 开发者模式 → 「加载已解压的扩展程序」→ 选 `browser-extension/`
2. 首次安装自动弹出设置页，粘贴上面的令牌，点「测试连接」
3. 之后浏览器里点下载就由 AI-IDM 多连接接管；文件落到 `~/Downloads`

三条入口：**自动接管**（取消浏览器下载后转交，失败回退浏览器）、**右键菜单**（链接/媒体/页面）、
**弹窗按钮**（当前标签页 + 实时进度）。也支持把 Cookie / Referer 一起带过去，登录站才下得动。
下载完成后默认自动弹出资源管理器并选中该文件（扩展 1.9.0 起；同一目录 2 秒内只开一次，
设置页可关）。

除扩展外，任何脚本都能直接调本地 API：

```bash
curl -X POST http://127.0.0.1:13391/add \
  -H "X-AI-IDM-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/big.iso","connections":16}'
```

接口清单、安全模型、限制与排查见 [docs/BROWSER-INTEGRATION.md](docs/BROWSER-INTEGRATION.md)。

## 工作原理

```
探测   HEAD → Range: bytes=0-0        → 总长、ETag、是否支持 Range
规划   块数 ≈ 连接数 × 4
工作线程  从队列领块，复用 keep-alive 连接
          Range: bytes=N-(M+63)       → 多的 64 字节是接缝样本（不写盘）
  ├─ 按偏移写进 <dest>.idmpart
  ├─ 用接缝样本和相邻块已落盘的数据逐字节比对
  └─ 失败：释放该块、退避，交给别的连接
收尾  接缝比对 → 可选抽样校验 → fsync → os.replace(<dest>.idmpart, <dest>)
```

模块划分与并发模型见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 设计来源（重要）

这个项目源于一个练习：先弄清某款商业下载软件**为什么**这么设计，再用 Python 从头实现一遍。
其中若干设计（64 字节接缝、动态分块、连接复用、块移交）来自对它的**运行时观测**；但代码全部是
为本项目新写的，没有复制任何产品的代码，也不分发任何二进制。哪些有观测依据、哪些是我自己的工程
判断，逐条列在 [docs/BEHAVIOR-NOTES.md](docs/BEHAVIOR-NOTES.md)。

本项目**不含**任何授权校验绕过、DRM 破解、付费墙绕过逻辑，也不接受这类贡献。

## 测试

```bash
python tests/run_all.py           # 全部
python tests/run_all.py -k seam   # 单个套件
```

13 个套件 / 55 项断言，全部自包含：本地 HTTP 服务器、手写的 SOCKS5 服务端、最小 FTP 服务端，
不需要连外网。CI 在 Linux 与 Windows、Python 3.10 / 3.12 / 3.13 上跑。

值得一提的是断言方式：比如「连接复用」不是看下载是否成功，而是**数服务端实际收到几条 TCP 连接**。
每个测试文件头部都写了它到底在验证什么。

## 已知边界

| 未实现 | 说明 |
|---|---|
| GUI | 只有 CLI + MCP + 网页面板（`ai-idm serve` 自带） |
| `blob:` / `data:` / POST 表单下载 | 浏览器不给可重放的 URL，扩展会交回浏览器 |
| DASH（`.mpd`） | 只做了 HLS |
| DRM（Widevine 等） | 设计上不做 |
| PAC 脚本 | 需要 JS 引擎；可用显式代理或 `auto` |
| HTTP/2、BT、Metalink | 暂不做 |

## 许可

MIT —— 见 [LICENSE](LICENSE)。
