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
  minSize: 0,            // < 该字节数不接管（0=不限）。浏览器常在下载完成后才知道大小，故仅作噪声过滤
};

const LOCAL_HOSTS = ["127.0.0.1:13391", "localhost:13391"];

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

/** 把 URL 交给本地服务；失败抛异常由调用方决定是否回退浏览器。 */
async function send(url, extra = {}) {
  const c = await cfg();
  const payload = { ...extra, url };
  if (c.forwardCookies && !payload.cookie) {
    const ck = await cookieHeader(url, c);
    if (ck) payload.cookie = ck;
  }
  const data = await api("/add", { method: "POST", body: payload, timeout: 8000 });
  await bumpBadge();
  if (c.notify) {
    notify("已交给 AI-IDM", basename(payload.filename) || data.dest || url);
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
  if (a.name === "idm-tick") bumpBadge();
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
