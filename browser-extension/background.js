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

/** 正在跟踪的服务端任务：tid -> {url, tabId, name}。用于页面浮层 + 完成通知。 */
const PENDING = new Map();
const LAST = new Map();          // tid -> {t, d}，用来算瞬时速度
let pollTimer = null;

// ---------------------------------------------------------------- 跟踪状态持久化（跨 SW 重启）
//
// MV3 service worker 会被休眠或杀掉重启，内存里的 PENDING 随之清空；idm-tick 闹钟
// 唤醒后 pollOnce() 见 PENDING 为空就直接返回，长下载的完成/失败通知因此丢失。
// 对策：PENDING 每次变更都写进 chrome.storage.session（浏览器会话内存活），SW 重启后
// 恢复并续上轮询。storage 不可用时降级为原来的内存跟踪（重启即丢，与旧行为一致）。

const SESSION_KEY = "pending_tasks";
let restorePromise = null;

async function persistPending() {
  try {
    await chrome.storage.session.set({ [SESSION_KEY]: [...PENDING.entries()] });
  } catch (_) { /* 降级为内存跟踪 */ }
}

/** 每个 SW 实例只恢复一次；有未完成任务且无定时器时续上轮询（不重发初始「排队中」HUD）。 */
function ensureRestored() {
  if (!restorePromise) {
    restorePromise = (async () => {
      try {
        const got = await chrome.storage.session.get(SESSION_KEY);
        const entries = got[SESSION_KEY];
        if (Array.isArray(entries)) {
          for (const [tid, meta] of entries) PENDING.set(tid, meta);
          await persistPending();     // 合并后全量写回，收敛 storage
        }
      } catch (_) { /* 读不到就当没有 */ }
      if (PENDING.size && !pollTimer) pollTimer = setInterval(pollOnce, 1500);
    })();
  }
  return restorePromise;
}

/** 收尾：移出跟踪表、清速度采样，并写回快照。 */
function forget(tid) {
  PENDING.delete(tid);
  LAST.delete(tid);
  persistPending();
}

function hud(tabId, payload) {
  if (!tabId) return;
  const push = () => chrome.tabs.sendMessage(
    tabId, { type: "hud", payload }, () => void chrome.runtime.lastError);

  // 扩展重载前就开着的标签页没有 content script：按需注入一次（脚本自身有幂等保护）
  let injected = false;
  const ensure = () => {
    if (injected) return;
    injected = true;
    try {
      chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] }, () => {
        void chrome.runtime.lastError;
        push();
      });
    } catch (_) { /* chrome:// 之类不允许注入 */ }
  };

  try {
    chrome.tabs.sendMessage(tabId, { type: "hud", payload }, () => {
      if (chrome.runtime.lastError) ensure();
    });
  } catch (_) { ensure(); }
}

function speedOf(tid, downloaded) {
  const now = Date.now();
  const prev = LAST.get(tid);
  LAST.set(tid, { t: now, d: downloaded || 0 });
  if (!prev) return 0;
  const dt = (now - prev.t) / 1000;
  if (dt <= 0) return 0;
  return Math.max(0, ((downloaded || 0) - prev.d) / dt);
}

async function pollOnce() {
  await ensureRestored();   // SW 重启后的第一次轮询：先把上一个实例留下的任务并回来
  if (!PENDING.size) {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    return;
  }
  try {
    const d = await api("/status", { timeout: 4000 });
    const byId = new Map((d.tasks || []).map((t) => [t.id, t]));
    for (const [tid, meta] of [...PENDING.entries()]) {
      const t = byId.get(tid);
      if (!t) continue;
      if (t.status === "pending" || t.status === "running") {
        hud(meta.tabId, {
          phase: "progress",
          queued: t.status === "pending",              // 排队中（还没轮到）
          connecting: t.status === "running" && !t.total, // 已开跑但还没探到大小
          percent: t.percent,
          total: t.total,
          speed: speedOf(tid, t.downloaded),
          name: meta.name || basename(t.dest) || t.url,
        });
      } else if (t.status === "done") {
        forget(tid);
        hud(meta.tabId, { phase: "done", dest: t.dest, total: t.total });
        notify("AI-IDM 下载完成", t.dest || t.url);
        chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
        chrome.action.setBadgeText({ text: "✓" });
        setTimeout(() => bumpBadge(), 6000);
      } else if (t.status === "error" || t.status === "cancelled") {
        forget(tid);
        hud(meta.tabId, { phase: "error", error: t.error || t.status });
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

async function track(tid, url, tabId, name) {
  if (!tid) return;
  await ensureRestored();    // 先并回上个 SW 实例留下的任务，快照才完整
  if (!tabId) {
    try {
      const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      tabId = t && t.id;
    } catch (_) { /* ignore */ }
  }
  PENDING.set(tid, { url, tabId, name });
  persistPending();
  hud(tabId, { phase: "progress", queued: true, percent: 0, total: 0, speed: 0, name: name || url });
  if (!pollTimer) pollTimer = setInterval(pollOnce, 1500);
}

/** 把 URL 交给本地服务；失败抛异常由调用方决定是否回退浏览器。 */
async function send(url, extra = {}, tabId = null) {
  const c = await cfg();
  // 镜像站链接原样转交：引擎识别到镜像域名会自动降为单连接，避免 429。
  const payload = { ...extra, url };
  if (c.forwardCookies && !payload.cookie) {
    const ck = await cookieHeader(url, c);
    if (ck) payload.cookie = ck;
  }
  const data = await api("/add", { method: "POST", body: payload, timeout: 8000 });
  await bumpBadge();
  const name = basename(payload.filename) || data.dest || url;
  await track(data.id, url, tabId, name);
  if (c.notify) {
    const conns = data.connections ? `${data.connections} 连接` : "多连接";
    notify("已交给 AI-IDM 下载", `${conns}，完成后会再提示`);
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
    await send(url, { referer: tab && tab.url, source: "menu" }, tab && tab.id);
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
  } catch (e) {
    // 已经下完/取消不掉，就不要抢
    return;
  }

  // 取消后必须给浏览器一点时间删掉自己的临时文件（未确认 NNNNNN.crdownload），
  // 否则立刻 erase 会让那个 0 字节的空壳变成孤儿文件，用户看到就以为下崩了。
  setTimeout(() => {
    chrome.downloads.erase({ id: item.id }, () => {
      void chrome.runtime.lastError;
      // 再做一次，覆盖 cancel→interrupt 的时序差
      setTimeout(() => chrome.downloads.erase({ id: item.id }, () => void chrome.runtime.lastError), 600);
    });
  }, 900);

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
