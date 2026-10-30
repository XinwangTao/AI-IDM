# AI-IDM 浏览器扩展隐私政策 / Privacy Policy

最后更新：2026-10-30

## 中文

AI-IDM 浏览器扩展（下称"本扩展"）是一个本地下载器 `ai-idm` 的配套组件。

**本扩展不收集、不上传、不共享任何个人数据。** 具体而言：

- **不采集浏览记录。** 只有在下载被接管时，才会把该次下载的 URL、Referer 与 Cookie
  发送到**你自己电脑上的** `127.0.0.1` 本地服务（默认端口 13391）；这些数据不离开你的设备。
- **不访问本机以外的服务。** 唯一的对外网络请求是拉取本项目的公开版本清单
  `latest.json`（用于提示有新版本），该请求不携带任何标识、Cookie 或浏览信息。
- **不包含分析、广告或追踪代码。** 没有第三方 SDK，没有遥测。
- **不读取页面内容。** 内容脚本仅用于显示下载进度浮层，不抓取、不外传页面数据。
- **本地存储**只保存扩展设置（服务端口、令牌、接管模式等）与版本检查时间，位于浏览器本地。

权限用途说明：

| 权限 | 用途 |
|---|---|
| `downloads` | 识别并接管浏览器下载，失败时交还浏览器 |
| `storage` | 保存本机设置与版本检查时间 |
| `contextMenus` | 右键"交给 AI-IDM 下载" |
| `notifications` | 下载完成/失败提示 |
| `cookies` | 接管时把 Cookie 转发给**本机**服务，使得会员/登录站能下载 |
| `alarms` | 定时刷新角标、定时检查版本 |
| `scripting` | 按需注入进度浮层 |
| `<all_urls>` | 接管任意站点触发的下载；不会读取页面数据 |

## English

The AI-IDM browser extension is a companion to the local downloader `ai-idm`.

**It collects, uploads and shares no personal data.**

- No browsing history is collected. When a download is taken over, its URL, Referer and Cookie
  are sent to the **local** service on `127.0.0.1` (default port 13391) on your own machine.
- The only outbound request is fetching this project's public `latest.json` version manifest to
  show an "update available" hint. It carries no identifiers, cookies or browsing data.
- No analytics, ads, tracking, or third-party SDKs.
- The content script only renders a progress HUD; it does not read or transmit page data.
- Local storage holds extension settings and the last update-check timestamp only.

Contact: open an issue at https://github.com/XinwangTao/AI-IDM/issues
