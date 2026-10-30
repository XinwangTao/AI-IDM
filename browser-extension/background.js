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
  openFolder: true,      // 下载完成后弹出资源管理器并选中文件（由本地服务代开）
};

// 回环地址（任意端口）都不接管：服务/面板经回环触发的下载必须豁免（服务改端口后
// 否则会形成取消-重交回环），本机自建站点的下载走多连接也没有收益。
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);

function isLoopback(url) {
  try {
    return LOOPBACK_HOSTS.has(new URL(url).hostname);
  } catch (_) {
    return /\/\/(127\.0\.0\.1|localhost|\[::1\])([:/?#]|$)/i.test(url); // 解析失败退回字符串匹配
  }
}

// 浏览器自身/商店/Edge 的更新包，转交给外部下载器没有意义
const SKIP_PATTERNS = [
  /\.crx(\?|$)/i,
  /^https?:\/\/chrome\.google\.com\/update2\//i,
  /^https?:\/\/clients2\.google\.com\//i,
  /^https?:\/\/update\.googleapis\.com\//i,
  /^https?:\/\/edge\.microsoft\.com\//i,
  /^https?:\/\/msedge\.api\.cdp\.microsoft\.com\//i,
  /^https?:\/\/msedge\.b\.tlu\.dl\.delivery\.mp\.microsoft\.com\//i,
];

// 版本检查（方案 C）：扩展以「加载已解压」安装时浏览器永不自动更新，只能自己比对仓库
// 里的 latest.json 再在弹窗提示。流程见 docs/archify/extension_update_check_workflow。
// 多个源按顺序试：raw 最快，jsDelivr / gh-proxy 作 CDN 兜底（国内网络经常干扰 raw）。
const UPDATE_URLS = [
  "https://raw.githubusercontent.com/XinwangTao/AI-IDM/main/latest.json",
  "https://cdn.jsdelivr.net/gh/XinwangTao/AI-IDM@main/latest.json",
  "https://gh-proxy.com/https://raw.githubusercontent.com/XinwangTao/AI-IDM/main/latest.json",
];
const UPDATE_ALARM = "update-check";
const UPDATE_INTERVAL_MS = 6 * 60 * 60 * 1000;   // 正常冷却 6h
const UPDATE_RETRY_MS = 30 * 60 * 1000;          // 拉取失败只要 30min 后就重试
const UPDATE_TIMEOUT_MS = 3000;

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

/** 字节数 → "12.3 MB"。完成通知里用，和浮层保持同一套单位。 */
function fmtSize(n) {
  n = Number(n) || 0;
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let v = n, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? Math.round(v) : v.toFixed(1)) + u[i];
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

// ---------------------------------------------------------------- 完成后打开文件夹
//
// 扩展自己没有「打开任意文件夹」的 API（chrome.downloads.showDefaultFolder 只认浏览器
// 自己的下载目录），所以请本地服务代开：POST /reveal。服务只肯开 download_dir 内的路径。
// 纯锦上添花：开关关、服务拒绝、系统开不了，都静默跳过，不影响下载结果和 ✓ 徽标。

const REVEAL_MERGE_MS = 2000;        // 同一目录这段时间内只开一次窗口：批量完成不弹一堆资源管理器
let lastReveal = { dir: "", at: 0 };

async function maybeReveal(dest) {
  if (!dest) return;
  const c = await cfg();
  if (!c.openFolder) return;
  const dir = String(dest).replace(/\\/g, "/").split("/").slice(0, -1).join("/");
  const now = Date.now();
  if (dir && dir === lastReveal.dir && now - lastReveal.at < REVEAL_MERGE_MS) return;
  lastReveal = { dir, at: now };
  try {
    await api("/reveal", { method: "POST", body: { path: dest }, timeout: 3000 });
  } catch (_) { /* 打开失败不影响已完成的事实 */ }
}

let badgeHoldUntil = 0;          // ✓ / ! 徽标的保留截止：服务瞬断时不被立刻抹掉

async function bumpBadge(pre) {
  try {
    const d = pre || await api("/status", { timeout: 2500 });
    const active = (d.tasks || []).filter(
      (t) => t.status === "pending" || t.status === "running");
    await chrome.action.setBadgeBackgroundColor({ color: "#2563eb" });
    let text = "";
    if (active.length === 1) {
      // 单个任务直接显示百分比：不打开弹窗也能看出进度
      text = String(Math.max(0, Math.min(100, Math.round(active[0].percent || 0))));
    } else if (active.length > 1) {
      text = String(active.length);
    }
    if (!text && hasNewer()) {
      // 有新版本且没有活跃任务时才占用角标；下载进度永远优先
      await chrome.action.setBadgeBackgroundColor({ color: "#7c3aed" });
      text = "↑";
    }
    await chrome.action.setBadgeText({ text });
  } catch (_) {
    if (Date.now() > badgeHoldUntil) {
      if (hasNewer()) await chrome.action.setBadgeBackgroundColor({ color: "#7c3aed" });
      await chrome.action.setBadgeText({ text: hasNewer() ? "↑" : "" });
    }
  }
}

// ---------------------------------------------------------------- 版本检查

/** { checked_at, latest, notes, url, dismissed }；latest 为空表示当前已是最新。 */
let updateState = { checked_at: 0, latest: "", notes: "", url: "", dismissed: "" };
let updateStateLoaded = false;

function cmpVersion(a, b) {
  const pa = String(a || "0").split(".");
  const pb = String(b || "0").split(".");
  for (let i = 0; i < 3; i++) {
    const d = (parseInt(pa[i], 10) || 0) - (parseInt(pb[i], 10) || 0);
    if (d) return d > 0 ? 1 : -1;
  }
  return 0;
}

async function loadUpdateState(force = false) {
  if (updateStateLoaded && !force) return updateState;
  try {
    const got = await chrome.storage.local.get("update_state");
    if (got.update_state && typeof got.update_state === "object") {
      updateState = { ...updateState, ...got.update_state };
    }
  } catch (_) { /* 读不到就用默认值：当作没有新版本 */ }
  updateStateLoaded = true;
  return updateState;
}

/** 商店安装的扩展会被浏览器自己升级：manifest 里由商店注入的 update_url 就是判据（无需 management 权限）。 */
function fromStore() {
  try { return !!chrome.runtime.getManifest().update_url; } catch (_) { return false; }
}

/** 有比本地更新、且用户没忽略过这一版的版本号。 */
function hasNewer() {
  return !!updateState.latest
    && updateState.latest !== updateState.dismissed
    && cmpVersion(updateState.latest, chrome.runtime.getManifest().version) > 0
    && !fromStore();                    // 商店版由浏览器自动升级，不占角标
}

/** 拉 latest.json 比对版本。失败一律静默，绝不打扰下载主流程。 */
async function checkForUpdate({ force = false } = {}) {
  await loadUpdateState();
  const age = Date.now() - (updateState.checked_at || 0);
  if (!force && age < UPDATE_INTERVAL_MS) return updateState;
  if (typeof navigator !== "undefined" && navigator.onLine === false) return updateState;

  let data = null;
  for (const url of UPDATE_URLS) {
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), UPDATE_TIMEOUT_MS);
    try {
      const res = await fetch(url, { cache: "no-store", signal: ac.signal });
      if (res.ok) data = await res.json();
    } catch (_) { /* 超时 / 被墙 / 非法 JSON：换下一个源 */ } finally {
      clearTimeout(timer);
    }
    if (data) break;
  }

  const next = { ...updateState };
  if (data && typeof data.version === "string" && data.version.trim()) {
    const latest = data.version.trim();
    next.checked_at = Date.now();
    if (cmpVersion(latest, chrome.runtime.getManifest().version) > 0) {
      next.latest = latest;
      next.notes = String(data.notes || "");
      next.url = String(data.store_url || data.zip_url || "");
    } else {
      next.latest = ""; next.notes = ""; next.url = "";
    }
  } else {
    // 没拿到可用响应：只做 30min 退避，别把失败当成功缓存 6h
    next.checked_at = Date.now() - (UPDATE_INTERVAL_MS - UPDATE_RETRY_MS);
  }

  updateState = next;
  updateStateLoaded = true;
  try { await chrome.storage.local.set({ update_state: updateState }); } catch (_) { /* 存不下就只在本次会话有效 */ }
  bumpBadge();
  return updateState;
}

/** 正在跟踪的服务端任务：tid -> {url, tabId, name}。用于页面浮层 + 完成通知。 */
const PENDING = new Map();
const LAST = new Map();          // tid -> {t, d}，用来算瞬时速度
const CARD_TABS = new Map();     // key -> Set<tabId>：哪些标签页上已经有这张卡
const POLL_MS = 500;             // 跟踪轮询：0.5s，配合页面端插值才不会一跳一跳
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
      if (PENDING.size && !pollTimer) pollTimer = setInterval(pollOnce, POLL_MS);
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

function hud(key, tabId, payload) {
  if (!tabId || !key) return;
  let holders = CARD_TABS.get(key);
  if (!holders) CARD_TABS.set(key, holders = new Set());
  holders.add(tabId);
  const push = () => chrome.tabs.sendMessage(
    tabId, { type: "hud", key, payload }, () => void chrome.runtime.lastError);

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
    chrome.tabs.sendMessage(tabId, { type: "hud", key, payload }, () => {
      if (chrome.runtime.lastError) ensure();
    });
  } catch (_) { ensure(); }
}

/** 让某个标签页移除这张卡（不加入 dismissed）：切走标签页时用。 */
function hudGone(key, tabId) {
  try {
    chrome.tabs.sendMessage(tabId, { type: "hud", key, payload: { phase: "gone" } },
                            () => void chrome.runtime.lastError);
  } catch (_) { /* 标签页已关 */ }
  const holders = CARD_TABS.get(key);
  if (holders) {
    holders.delete(tabId);
    if (!holders.size) CARD_TABS.delete(key);
  }
}

/** 进度只发给「发起页 + 当前活动页」，其余持有旧卡的页发 gone 收掉。 */
function pushProgress(key, meta, payload, activeId) {
  const targets = new Set();
  if (meta.tabId) targets.add(meta.tabId);
  if (activeId) targets.add(activeId);
  for (const tabId of targets) hud(key, tabId, payload);
  const holders = CARD_TABS.get(key);
  if (!holders) return;
  for (const tabId of [...holders]) {
    if (!targets.has(tabId)) hudGone(key, tabId);
  }
}

/** 终态发给所有持有该卡的标签页；一张都没有就发给当前活动页。 */
function broadcastFinal(key, activeId, payload) {
  const holders = CARD_TABS.get(key);
  const targets = holders && holders.size ? [...holders] : (activeId ? [activeId] : []);
  for (const tabId of targets) hud(key, tabId, payload);
  CARD_TABS.delete(key);
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

async function pollOnceNow() {
  await ensureRestored();   // SW 重启后的第一次轮询：先把上一个实例留下的任务并回来
  if (!PENDING.size) {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    return;
  }
  try {
    const d = await api("/status", { timeout: 4000 });
    const tasks = d.tasks || [];
    // /status 默认 limit=100：只有列表没被截断时，"看不到的任务"才能断定是真的没了
    const truncated = tasks.length >= 100;
    const byId = new Map(tasks.map((t) => [t.id, t]));
    let activeId = null;
    try {
      const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      activeId = tab && tab.id;
    } catch (_) { /* 取不到活动页就只发发起页 */ }
    let dirty = false;
    for (const [tid, meta] of [...PENDING.entries()]) {
      const key = meta.key || tid;
      const t = byId.get(tid);
      if (!t) {
        // 任务被弹窗「移除」或保留期清理掉了：不清的话 PENDING 永不为空，0.5s 轮询停不下来
        if (!truncated) {
          meta.miss = (meta.miss || 0) + 1;
          dirty = true;
          if (meta.miss >= 2) {          // 连续两轮都看不到才收尾，容忍一次抖动
            forget(tid);
            broadcastFinal(key, activeId, { phase: "error", error: "任务已被移除" });
          }
        }
        continue;
      }
      if (meta.miss) { delete meta.miss; dirty = true; }
      if (t.status === "pending" || t.status === "running") {
        pushProgress(key, meta, {
          phase: "progress",
          queued: t.status === "pending",              // 排队中（还没轮到）
          connecting: t.status === "running" && !t.total, // 已开跑但还没探到大小
          percent: t.percent,
          total: t.total,
          speed: speedOf(tid, t.downloaded),
          connections: t.connections,                  // /status 行里带连接数
          name: meta.name || basename(t.dest) || t.url,
        }, activeId);
      } else if (t.status === "done") {
        forget(tid);
        broadcastFinal(key, activeId, { phase: "done", dest: t.dest, total: t.total });
        notify("AI-IDM 下载完成",
               [fmtSize(t.total), t.dest || t.url].filter(Boolean).join(" · "));
        maybeReveal(t.dest);          // 弹出资源管理器并选中文件（失败静默）
        chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
        chrome.action.setBadgeText({ text: "✓" });
        badgeHoldUntil = Date.now() + 6000;
        setTimeout(() => bumpBadge(), 6000);
      } else if (t.status === "error" || t.status === "cancelled") {
        forget(tid);
        broadcastFinal(key, activeId, { phase: "error", error: t.error || t.status });
        notify("AI-IDM 下载失败", `${t.error || t.status}\n${t.url || ""}`);
        chrome.action.setBadgeBackgroundColor({ color: "#dc2626" });
        chrome.action.setBadgeText({ text: "!" });
        badgeHoldUntil = Date.now() + 6000;
        setTimeout(() => bumpBadge(), 6000);
      }
    }
    // miss 计数变更要写回快照，否则 SW 重启后又从 0 数起
    if (dirty) persistPending();
    // 只有还有未完成任务时才刷新计数，否则会把刚设的 ✓ / ! 覆盖掉
    // 复用这一轮已经拿到的 /status，别再发一次请求（0.5s 轮询下很浪费）
    if (PENDING.size) await bumpBadge(d);
    if (!PENDING.size && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  } catch (_) { /* 服务暂时不可达，下一轮再试 */ }
}

let pollBusy = false;
/** 防重入：0.5s 定时器 + 30s 告警叠加时，慢 /status 的旧响应可能后到并覆盖新进度。 */
async function pollOnce() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    await pollOnceNow();
  } finally {
    pollBusy = false;
  }
}

async function track(tid, url, tabId, name, key) {
  if (!tid) return;
  await ensureRestored();    // 先并回上个 SW 实例留下的任务，快照才完整
  if (!tabId) {
    try {
      const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      tabId = t && t.id;
    } catch (_) { /* ignore */ }
  }
  // 服务端去重（dedup_add）会把同一 URL 返回成既有 tid；若该 tid 已在跟踪，
  // 先给旧卡一个终态再接管 key，否则旧卡永远收不到 done/error，页面 200ms 插值也停不下来。
  const prev = PENDING.get(tid);
  if (prev && prev.key && prev.key !== key) {
    hud(prev.key, prev.tabId, { phase: "error", error: "该链接已在下载队列中，进度见新卡片" });
  }
  PENDING.set(tid, { url, tabId, name, key });
  persistPending();
  hud(key || tid, tabId, { phase: "progress", queued: true, percent: 0, total: 0, speed: 0, name: name || url });
  if (!pollTimer) pollTimer = setInterval(pollOnce, POLL_MS);
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
  if (!tabId) {
    try {
      const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      tabId = t && t.id;
    } catch (_) { /* ignore */ }
  }
  // 浏览器自己的下载项已被取消/即将消失，先把卡推出去，别留空档。
  // key 在提交前生成，/add 回来后沿用同一张卡——不换卡、不闪跳。
  const key = `k${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  const name = basename(payload.filename) || url;
  hud(key, tabId, { phase: "progress", handoff: true, percent: 0, total: 0, speed: 0, name });
  try {
    const data = await api("/add", { method: "POST", body: payload, timeout: 8000 });
    await bumpBadge();
    const realName = basename(payload.filename) || basename(data.dest) || url;
    await track(data.id, url, tabId, realName, key);
    // 接管阶段不再单独弹通知：浮层已经从「正在接管」走到「排队中」，
    // 再弹一条「已交给」是同一个事件说两遍（1.10.1 合并通知）。
    return data;
  } catch (e) {
    // 卡片不能停在「正在接管」：把 key 交给调用方，按失败方式收尾
    if (e && typeof e === "object") { e.hudKey = key; e.hudTab = tabId; }
    throw e;
  }
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
  chrome.alarms.create(UPDATE_ALARM, { periodInMinutes: 360 });
  loadUpdateState(true);
  checkForUpdate({ force: true });      // 装好/升级后立刻看一眼有没有更新
  if (details.reason === "install") {
    chrome.tabs.create({ url: chrome.runtime.getURL("options.html") });
  }
});

chrome.runtime.onStartup.addListener(() => {
  buildMenus();
  chrome.alarms.create("idm-tick", { periodInMinutes: 0.5 });
  chrome.alarms.create(UPDATE_ALARM, { periodInMinutes: 360 });
  checkForUpdate();
});

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "idm-tick") {
    bumpBadge();
    pollOnce();          // 兜底：worker 被休眠时靠它续上完成通知
    return;
  }
  if (a.name === UPDATE_ALARM) {
    checkForUpdate();
  }
});

// 标签页关掉后，别让 CARD_TABS 里留着死页签（否则每次轮询都白发一次 gone）
chrome.tabs.onRemoved.addListener((tabId) => {
  for (const [key, holders] of [...CARD_TABS.entries()]) {
    holders.delete(tabId);
    if (!holders.size) CARD_TABS.delete(key);
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
    if (e && e.hudKey) {
      hud(e.hudKey, e.hudTab, { phase: "error", error: `加入失败：${e.message || e}` });
    }
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
  if (isLoopback(item.url)) return;                          // 回环任意端口：服务/本机来源不接管
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
    if (e && e.hudKey) {
      hud(e.hudKey, e.hudTab, { phase: "error", error: "接管失败，已交回浏览器" });
    }
    notify("AI-IDM 接管失败，已交回浏览器", String(e.message || e));
  }
});

// ---------------------------------------------------------------- 消息（弹窗用）

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (!msg || !msg.type) return;
  if (msg.type === "add") {
    send(msg.url, { referer: msg.referer, filename: msg.filename, source: "popup" })
      .then((r) => reply({ ok: true, ...r }))
      .catch((e) => {
        if (e && e.hudKey) {
          hud(e.hudKey, e.hudTab, { phase: "error", error: `加入失败：${e.message || e}` });
        }
        reply({ ok: false, error: String(e.message || e) });
      });
    return true;
  }
  if (msg.type === "ping") {
    online().then((ok) => reply({ ok }));
    return true;
  }
  if (msg.type === "refreshBadge") {
    loadUpdateState(true).then(() => bumpBadge()).then(() => reply({ ok: true }));
    return true;
  }
  if (msg.type === "checkUpdate") {
    checkForUpdate({ force: !!msg.force }).then((st) => reply(st)).catch(() => reply(null));
    return true;
  }
});
