# 扩展发版与商店上架

本文件对应 `docs/archify/extension_release_pipeline_workflow.html`（发版流水线图）。
目标：**装过一次的设备以后自动更新**，而不是每台机器手动换文件夹。

## 0. 为什么必须走这条路

`browser-extension/` 以「加载已解压」方式安装时，浏览器官方机制下**永远不会自动更新**
（没有 CRX、扩展 ID 由路径派生、manifest 里的 `update_url` 会被忽略）。两条官方路径：
上架商店（浏览器代劳），或自托管签名 CRX + `update_url`（见文末附录）。本仓库选前者。

## 1. 一次性准备（需要你本人操作）

1. 注册 [Microsoft Partner Center](https://partner.microsoft.com/dashboard) 并加入
   *Microsoft Edge 程序*（免费）。
2. 准备好上架资料：
   - 名称：`AI-IDM 下载接管`
   - 简介（≤128 字符）：把浏览器下载交给本地 AI-IDM 多连接下载器
   - 详细描述：见下方 §3 现成文案
   - 图标：`browser-extension/icons/icon128.png`（≥128×128）
   - 截图：1280×800 或 640×480，至少 1 张（弹窗、页面浮层各一张最好）
   - 隐私政策 URL：`https://github.com/XinwangTao/AI-IDM/blob/main/PRIVACY.md`
   - 技术支持/主页：`https://github.com/XinwangTao/AI-IDM`
3. 说明**为什么需要 `<all_urls>`、`downloads`、`cookies`**（审核重点，见 §4）。

## 2. 每次发版

```bash
# 1) 改代码后先 bump 两处版本号（必须一致，否则脚本直接失败）
#    - browser-extension/manifest.json 的 version
#    - CHANGELOG.md 新增 "## Extension <ver> - <日期>" 段
python scripts/package_extension.py --check     # 一致性 + manifest 引用文件检查
python scripts/package_extension.py             # 产出 dist/ai-idm-extension-<ver>.zip
```

2. 把 ZIP 上传到 Partner Center → 提交审核（Edge 通常几天）。
3. 过审并上架后，再发布版本源（**顺序不能反**，否则用户会点到不存在的商店页）：

```bash
git tag ext-v<ver> && git push origin ext-v<ver>
# GitHub Release 上传 dist 里的 ZIP
python scripts/package_extension.py \
  --store-url "https://microsoftedge.microsoft.com/addons/detail/<id>" \
  --zip-url   "https://github.com/XinwangTao/AI-IDM/releases/download/ext-v<ver>/ai-idm-extension-<ver>.zip" \
  --notes     "一句话说明"
git add latest.json && git commit -m "release(extension): <ver>"
```

`latest.json` 推上 main 后，所有设备会在 6 小时内（或下次开弹窗时）看到更新横幅。

## 3. 商店描述文案

**中文：**

> 把浏览器的下载任务交给本地 AI-IDM 多连接下载器：自动接管、断点续传、实时进度浮层。
> 需要先在本机运行 `ai-idm serve`。下载失败或服务未启动时自动交还浏览器，绝不丢文件。
> 不收集任何浏览数据，所有请求只发往本机 127.0.0.1。

**English:**

> Hands browser downloads to the local AI-IDM multi-connection downloader: automatic takeover,
> resumable transfers, live in-page progress. Requires `ai-idm serve` running on the same machine.
> Falls back to the browser download on any failure so no file is ever lost. No browsing data is
> collected; the extension only talks to 127.0.0.1.

## 4. 审核常见卡点

| 卡点 | 应对 |
|---|---|
| `<all_urls>` 被判权限过大 | 在提交说明里写：任何站点的下载都可能被接管，无法限定域名；内容脚本只渲染进度浮层 |
| `cookies` 被判敏感 | 说明只把 Cookie 转发到本机回环服务，不落盘、不上传 |
| 首次安装跳转 options 页 | 属于正常引导，提交时注明「首次安装需要填入本机令牌」 |
| 版本号重复 | 同一版本号不能重提交，驳回后必须 bump |

## 5. 上架后的其他设备怎么办

- **商店安装的设备**：浏览器自动升级，无需任何操作。
- **仍是「加载已解压」的设备**：打开弹窗会看到「发现新版本」横幅，按提示换成商店版即可；
  横幅只提示不代装（解压形态无法自更新）。

## 6. 解压版：Edge 加载的是副本（踩过的坑）

Edge「加载解压缩的扩展」之后，实际运行的是

```
%LOCALAPPDATA%\Microsoft\Edge\User Data\<Profile>\UnpackedExtensions\ai-idm-extension_<id>\
```

的一份**快照**，不是仓库目录。改完代码只点「重新加载」不会生效——版本号仍然是旧的。
同步方式：

```bash
python scripts/install_to_edge.py --dry-run   # 只看计划
python scripts/install_to_edge.py             # 备份 → 覆盖 → 去 edge://extensions 点「重新加载」
```

不要用「移除后重新加载解压目录」来更新：扩展 ID 会按新路径重算，令牌/端口/接管模式全部要重填。
商店版上架后应卸掉解压版，改用商店安装（浏览器会自动升级）。

## 附录：自托管 CRX + `update_url`（本仓库未采用）

Edge 官方支持给外部安装的扩展配 `"update_url"`，浏览器每几小时拉一次 gupdate XML；
但新版 CRX 必须用**同一个私钥**签名，且 Chrome 自 M33 起只接受商店扩展（企业策略例外）。
需要自管 `.pem`，密钥泄露等于任何人都能给你的设备推更新，因此本仓库不走这条路，仅作备案。
