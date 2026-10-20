# 浏览器接管（Browser Integration）

让浏览器里的下载自动交给 AI-IDM：点一次下载，文件由本地多连接引擎接管。
实现方式与 IDM 同构 —— **浏览器扩展 → 本机回环 HTTP 服务 → 下载引擎**。

```
Chrome / Edge 扩展                 ai-idm serve                    下载引擎
┌──────────────────┐   POST /add  ┌──────────────────┐   queue   ┌──────────┐
│ downloads.onCreated│ ──────────▶ │ Token 鉴权        │ ────────▶ │ 8 连接分片│
│ 右键菜单 / 弹窗按钮 │ ◀────────── │ SQLite 队列 + Runner│ ◀──────── │ 接缝校验  │
└──────────────────┘  GET /status └──────────────────┘  事件      └──────────┘
```

## 三条入口

| 入口 | 触发方式 | 说明 |
|---|---|---|
| 自动接管 | 浏览器任何 `http/https/ftp` 下载 | 取消浏览器下载 → 转给 AI-IDM，失败自动回退浏览器 |
| 右键菜单 | 链接 / 视频 / 音频 / 图片 / 页面 | 「用 AI-IDM 下载此链接」等三项 |
| 弹窗按钮 | 点工具栏图标 | 下载当前标签页；同时看实时进度 |

## 快速开始

```bash
# 1. 装下载器（本仓库内直接可编辑安装）
pip install -e .

# 2. 启动接管服务：会打印地址、令牌、面板链接
ai-idm serve
#   AI-IDM 接管服务 v1.12.0 已启动
#     地址    : http://127.0.0.1:13391
#     令牌    : 3Qk...（同时写入 config.json 的 server_token）
#     下载目录: C:\Users\you\Downloads
#     面板    : http://127.0.0.1:13391/?token=...
```

3. 浏览器打开 `chrome://extensions`（Edge 是 `edge://extensions`）→ 打开「开发者模式」
   → 「加载已解压的扩展程序」→ 选仓库里的 `browser-extension/` 目录。
   （扩展不随 wheel 分发：用 `pip install ai-idm` 装的，从 GitHub 下载 `browser-extension/` 即可。）
4. 扩展首次安装会自动打开设置页：填入上一步的**令牌**，点「测试连接」。
5. 之后在浏览器里点下载即可。文件落在 `~/Downloads`。

> 只想手动触发：设置页把「接管模式」改成**手动**，浏览器下载就不受影响，
> 只有右键菜单和弹窗按钮会把链接交给 AI-IDM。

## 常用参数

```bash
ai-idm serve                      # 默认 127.0.0.1:13391，自动下载
ai-idm serve -p 4                 # 同时下 4 个任务
ai-idm serve -d D:\Downloads      # 指定落盘目录
ai-idm serve --port 13392         # 换端口（扩展设置里同步改）
ai-idm serve --no-run             # 只接计入队，另开 ai-idm queue run 下载
ai-idm token                      # 只打印令牌（扩展里填这个）
```

配置项也会持久化到 `config.json`：`server_host / server_port / server_token /
server_parallel / download_dir`。

## 网页面板

`ai-idm serve` 会打印带令牌的面板链接，也能直接开 `http://127.0.0.1:13391/`
再粘贴令牌。面板列出所有任务、进度条和下载目录，1.5 秒自动刷新。

## HTTP API

除了扩展，任何脚本都能直接调用（都要求 `X-AI-IDM-Token` 头，`/ping` 除外）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/ping` | 探活，返回 `{ok,name,version}`，无需令牌 |
| GET | `/status` | 全部任务 + 统计 |
| GET | `/status/<id>` | 单个任务 |
| GET | `/events` | 最近 50 条事件（added / done / error / cancelled） |
| GET | `/config` | 脱敏配置（不含令牌） |
| POST | `/add` | 入队，JSON 或表单 |
| POST | `/cancel/<id>` | 取消（运行中会中断并保留断点） |
| POST | `/remove/<id>` | 删除任务记录 |

`POST /add` 支持的字段：

```json
{
  "url": "https://example.com/big.iso",
  "filename": "big.iso",          // 可选，只取文件名部分，路径穿越会被剥掉
  "referer": "https://example.com/",
  "ua": "Mozilla/5.0 ...",
  "cookie": "sid=abc; uid=1",
  "headers": {"Accept-Language": "zh-CN"},
  "connections": 16,
  "limit": 2097152,                // 字节/秒
  "proxy": "http://127.0.0.1:7890",
  "hls": false                     // true 或 URL 以 .m3u8 结尾 → 走 HLS 合并
}
```

```bash
curl -s -X POST http://127.0.0.1:13391/add \
  -H "X-AI-IDM-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/big.iso"}'
```

## 安全模型

- 服务默认只绑 `127.0.0.1`，并额外拒绝非回环来源的连接；
- 除 `/ping` 外全部接口校验令牌（`hmac.compare_digest` 常量时间比较）；
- 令牌首次启动自动生成（`secrets.token_urlsafe(18)`），落在 `config.json`；
- 请求体上限 256 KB；URL 只允许 `http/https/ftp/ftps`，挡掉 `file:`/`javascript:`；
- 自定义请求头做 CRLF 注入过滤，文件名剥离路径、拦 Windows 保留名（`CON`/`NUL`…）；
- 扩展只在**本地服务在线**时才取消浏览器下载，任何一步失败都会回退到浏览器下载。

## 已知限制

| 场景 | 说明 |
|---|---|
| `blob:` / `data:` URL | 内容只存在于页面内存，服务端取不到；扩展会跳过，交回浏览器 |
| POST 表单触发的下载 | 浏览器不给原始 URL，无法重放请求体，交回浏览器 |
| 一次性签名链接 | 转交后若链接过期会 403；扩展此时已取消浏览器下载，需重新生成链接 |
| 需要登录的站点 | 勾选「转发 Cookie」后可用（扩展读 `chrome.cookies`）；HttpOnly 也能取到 |
| 防盗链（Referer 校验） | 扩展已自动带 `referer`，个别站点仍需 `--header` 手动补 |
| DRM 加密流 | HLS 只支持 AES-128 明文密钥，Widevine 等不支持 |

## 故障排查

| 现象 | 处理 |
|---|---|
| 扩展显示「服务未启动」 | 先 `ai-idm serve`；端口要与设置页一致 |
| 「令牌无效」 | 设置页粘贴 `ai-idm token` 的输出 |
| 下载 0 字节 / 403 | 打开「转发 Cookie」；或先 `ai-idm get <url> -H 'Referer: ...'` 验证 |
| 接管后浏览器仍在下载 | 该 URL 不是 http(s)/ftp（blob、data），或服务离线 |
| 端口被占用 | `ai-idm serve --port 13392`，扩展设置同步改 |
| 想看服务日志 | 面板 `/` 看任务；事件流见 `/events` |

## 一条命令验证链路

```bash
ai-idm serve --no-run &          # 只入队
curl -s -X POST http://127.0.0.1:13391/add \
  -H "X-AI-IDM-Token: $(ai-idm token | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')" \
  -d 'url=https://example.com/f.bin'      # 表单写法也支持
curl -s "http://127.0.0.1:13391/status?token=$TOKEN"
```
