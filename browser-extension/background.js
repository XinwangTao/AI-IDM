/* AI-IDM 浏览器接管 — MV3 service worker
 *
 * 三条入口：
 *   1) 自动接管：监听 chrome.downloads.onCreated，取消浏览器下载后转给本地服务
 *   2) 右键菜单：链接 / 媒体 / 当前页面
 *   3) 弹窗按钮：下载当前标签页
 *
 * 设计原则：服务没开、或接管过程中任何一步失败，都回退到浏览器下载，绝不丢文件。
 */

const DEFAULTS = {
  port: 13391,
  token: "",
  mode: "auto",          // auto=自动接管 | manual=只用右键/按钮 | off=完全关闭
  forwardCookies: true,  // 带上 Cookie，会员站/登录站才下得动
  notify: true,
};

const LOCAL_HOSTS = ["127.0.0.1:13391", "localhost:13391"];

// 浏览器自身/商店的更新包，转交给外部下载器没有意义
const SKIP_PATTERNS = [
  /\.crx(\?|$)/i,
  /^https?:\/\/chrome\.google\.com\/update2\//i,
  /^https?:\/\/clients2\.google\.com\//i,
  /^https?:\/\/update\.googleapis\.com\//i,
];

// ---------------------------------------------------------------- 配置 / 请求

async function cfg() {
  const got = await chrome.storage.local.get(Object.keys(DEFAULTS));
  return { ...DEFAULTS, ...got };
}

function baseOf(c) {
  return `http://127.0.0.1:${c.port || DEFAULTS.port}`;
}

async function api(path, { method = "GET", body, timeout = 6000, token } = {}) {
  const c = await cfg();
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeout);
  try {
    const res = await fetch(baseOf(c) + path, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-AI-IDM-Token": token ?? c.token,
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: ac.signal,
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* 可能是 HTML */ }
    if (!res.ok) {
      const msg = data.error || `HTTP ${res.status}`;
      throw new Error(res.status === 401 ? "令牌无效（在扩展选项里填入 ai-idm serve 打印的令牌）" : msg);
    }
    return data;
  } finally {
    clearTimeout(timer);
  }
}

async function online() {
  try {
    await api("/ping", { timeout: 900 });
    return true;
  } catch (_) {
    return false;
  }
}

// ---------------------------------------------------------------- 工具

function basename(p) {
  if (!p) return undefined;
  const s = String(p).replace(/\\/g, "/").split("/").filter(Boolean).pop();
  return s || undefined;
}

async function cookieHeader(url, c) {
  if (!c.forwardCookies) return "";
  try {
    const list = await chrome.cookies.getAll({ url });
    return list.map((x) => `${x.name}=${x.value}`).join("; ");
  } catch (_) {
    return "";
  }
}

function notify(title, message) {
  try {
    chrome.notifications.create({
      type: "basic",
      iconUrl: chrome.runtime.getURL("icons/icon128.png"),
      title,
      message: message || "",
    });
  } catch (_) { /* 通知权限被拒也不影响主流程 */ }
}

async function bumpBadge() {
  try {
    const d = await api("/status", { timeout: 2500 });
    const active = (d.tasks || []).filter(
      (t) => t.status === "pending" || t.status === "running").length;
    await chrome.action.setBadgeBackgroundColor({ color: "#2563eb" });
    await chrome.action.setBadgeText({ text: active ? String(active) : "" });
  } catch (_) {
    await chrome.action.setBadgeText({ text: "" });
  }
}

/** 正在跟踪的服务端任务：tid -> {url}。用于完成后弹通知。 */
const PENDING = new Map();
let pollTimer = null;

async function pollOnce() {
  if (!PENDING.size) {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    return;
  }
  try {
    const d = await api("/status", { timeout: 4000 });
    const byId = new Map((d.tasks || []).map((t) => [t.id, t]));
    for (const tid of [...PENDING.keys()]) {
      const t = byId.get(tid);
      if (!t) continue;
      if (t.status === "done") {
        PENDING.delete(tid);
        notify("AI-IDM 下载完成", t.dest || t.url);
        // 系统通知被关掉时，图标上的绿勾也能说明"下完了"
        chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
        chrome.action.setBadgeText({ text: "✓" });
        setTimeout(() => bumpBadge(), 6000);
      } else if (t.status === "error" || t.status === "cancelled") {
        PENDING.delete(tid);
        notify("AI-IDM 下载失败", `${t.error || t.status}\n${t.url || ""}`);
        chrome.action.setBadgeBackgroundColor({ color: "#dc2626" });
        chrome.action.setBadgeText({ text: "!" });
        setTimeout(() => bumpBadge(), 6000);
      }
    }
    // 只有还有未完成任务时才刷新计数，否则会把刚设的 ✓ / ! 覆盖掉
    if (PENDING.size) await bumpBadge();
    if (!PENDING.size && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  } catch (_) { /* 服务暂时不可达，下一轮再试 */ }
}

function track(tid, url) {
  if (!tid) return;
  PENDING.set(tid, { url });
  if (!pollTimer) pollTimer = setInterval(pollOnce, 2000);
}

/** 把 URL 交给本地服务；失败抛异常由调用方决定是否回退浏览器。 */
async function send(url, extra = {}) {
  const c = await cfg();
  // 镜像站链接原样转交：引擎识别到镜像域名会自动降为单连接，避免 429。
  const payload = { ...extra, url };
  if (c.forwardCookies && !payload.cookie) {
    const ck = await cookieHeader(url, c);
    if (ck) payload.cookie = ck;
  }
  const data = await api("/add", { method: "POST", body: payload, timeout: 8000 });
  await bumpBadge();
  track(data.id, url);
  if (c.notify) {
    const name = basename(payload.filename) || data.dest || url;
    const conns = data.connections ? `${data.connections} 连接` : "多连接";
    notify("已交给 AI-IDM 下载", `${name}\n${conns}，完成后会再提示`);
  }
  return data;
}

// ---------------------------------------------------------------- 右键菜单

const MENUS = [
  { id: "idm-link", title: "用 AI-IDM 下载此链接", contexts: ["link"] },
  { id: "idm-media", title: "用 AI-IDM 下载此媒体", contexts: ["video", "audio", "image"] },
  { id: "idm-page", title: "用 AI-IDM 下载当前页面", contexts: ["page"] },
];

function buildMenus() {
  chrome.contextMenus.removeAll(() => {
    for (const m of MENUS) chrome.contextMenus.create(m, () => void chrome.runtime.lastError);
  });
}

chrome.runtime.onInstalled.addListener((details) => {
  buildMenus();
  chrome.alarms.create("idm-tick", { periodInMinutes: 0.5 });
  if (details.reason === "install") {
    chrome.tabs.create({ url: chrome.runtime.getURL("options.html") });
  }
});

chrome.runtime.onStartup.addListener(() => {
  buildMenus();
  chrome.alarms.create("idm-tick", { periodInMinutes: 0.5 });
});

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "idm-tick") {
    bumpBadge();
    pollOnce();          // 兜底：worker 被休眠时靠它续上完成通知
  }
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const c = await cfg();
  if (c.mode === "off") {
    notify("AI-IDM 已关闭", "在扩展选项里把接管模式改为「自动」或「手动」");
    return;
  }
  const url = info.linkUrl || info.srcUrl || info.pageUrl;
  if (!url) return;
  try {
    await send(url, { referer: tab && tab.url, source: "menu" });
  } catch (e) {
    notify("加入 AI-IDM 失败", String(e.message || e));
  }
});

// ---------------------------------------------------------------- 自动接管

chrome.downloads.onCreated.addListener(async (item) => {
  const c = await cfg();
  if (c.mode !== "auto") return;
  if (!item || !item.url) return;
  if (!/^(https?|ftps?):/i.test(item.url)) return;          // blob:/data:/chrome: 交给浏览器
  if (item.byExtensionId === chrome.runtime.id) return;      // 我们自己的回退下载，别再抓一次
  if (LOCAL_HOSTS.some((h) => item.url.includes(h))) return;
  if (SKIP_PATTERNS.some((re) => re.test(item.url))) return; // 浏览器/商店的更新包
  if (!(await online())) return;                             // 服务没开：不动浏览器的下载

  const filename = basename(item.filename);
  try {
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id }).catch(() => {});
  } catch (e) {
    // 已经下完/取消不掉，就不要抢
    return;
  }

  try {
    await send(item.url, {
      referer: item.referrer || undefined,
      filename,
      source: "auto",
    });
  } catch (e) {
    // 接管失败 → 还给浏览器，避免文件丢失
    try {
      await chrome.downloads.download({
        url: item.url,
        filename,
        conflictAction: "uniquify",
      });
    } catch (_) { /* 浏览器也不再接受，只能提示 */ }
    notify("AI-IDM 接管失败，已交回浏览器", String(e.message || e));
  }
});

// ---------------------------------------------------------------- 消息（弹窗用）

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (!msg || !msg.type) return;
  if (msg.type === "add") {
    send(msg.url, { referer: msg.referer, filename: msg.filename, source: "popup" })
      .then((r) => reply({ ok: true, ...r }))
      .catch((e) => reply({ ok: false, error: String(e.message || e) }));
    return true;
  }
  if (msg.type === "ping") {
    online().then((ok) => reply({ ok }));
    return true;
  }
  if (msg.type === "refreshBadge") {
    bumpBadge().then(() => reply({ ok: true }));
    return true;
  }
});
