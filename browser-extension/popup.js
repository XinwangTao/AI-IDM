/* 弹窗：显示服务状态 + 当前标签页 + 最近任务（0.8s 轮询，增量更新）
 *
 * 1.9.1 起不再每轮全量重写 innerHTML：行按任务 id 复用，只改变化的文本/宽度，
 * 否则每 1.5s 重建会把正在点的按钮、悬停态和滚动位置一起打断。
 */

const $ = (s) => document.querySelector(s);
const DEFAULTS = { port: 13391, token: "", mode: "auto" };
const REFRESH_MS = 800;

let cfg = { ...DEFAULTS };
let currentUrl = "";

const UPDATE_KEY = "update_state";
const RELEASE_PAGE = "https://github.com/XinwangTao/AI-IDM/releases";

function cmpVersion(a, b) {
  const pa = String(a || "0").split(".");
  const pb = String(b || "0").split(".");
  for (let i = 0; i < 3; i++) {
    const d = (parseInt(pa[i], 10) || 0) - (parseInt(pb[i], 10) || 0);
    if (d) return d > 0 ? 1 : -1;
  }
  return 0;
}

async function readUpdate() {
  const got = await chrome.storage.local.get(UPDATE_KEY);
  return got[UPDATE_KEY] || null;
}

/** 只画横幅，不做网络；状态由 background 在检查后写进 storage。 */
function renderUpdate(st) {
  const box = $("#upd");
  const cur = chrome.runtime.getManifest().version;
  const newer = !!(st && st.latest
    && st.latest !== st.dismissed
    && cmpVersion(st.latest, cur) > 0);
  if (!newer) { box.classList.remove("show"); return; }
  // 商店安装的由浏览器自动升级：只告知，不给手动入口
  const store = !!chrome.runtime.getManifest().update_url;
  $("#upd-txt").textContent = store
    ? "新版本 v" + st.latest + " 将由浏览器自动更新"
    : "发现新版本 v" + st.latest + (st.notes ? " · " + st.notes : "");
  $("#upd-go").style.display = store ? "none" : "";
  $("#upd-sep").style.display = store ? "none" : "";
  box.classList.add("show");
}

const rowMap = new Map();   // id -> { el, nm, bar, pct, btn, st }
const busy = new Set();     // 已点「取消/移除」、等结果的 id

async function loadCfg() {
  const got = await chrome.storage.local.get(Object.keys(DEFAULTS));
  cfg = { ...DEFAULTS, ...got };
}

function basename(p) {
  if (!p) return "";
  return String(p).replace(/\\/g, "/").split("/").filter(Boolean).pop() || p;
}

async function api(path, opt = {}) {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), opt.timeout || 4000);
  try {
    const res = await fetch(`http://127.0.0.1:${cfg.port}${path}`, {
      method: opt.method || "GET",
      headers: { "Content-Type": "application/json", "X-AI-IDM-Token": cfg.token },
      body: opt.body ? JSON.stringify(opt.body) : undefined,
      signal: ac.signal,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(res.status === 401 ? "令牌无效" : (data.error || `HTTP ${res.status}`));
    }
    return data;
  } finally {
    clearTimeout(t);
  }
}

async function ping() {
  try {
    const d = await api("/ping", { timeout: 1200 });
    $("#dot").className = "dot on";
    const ext = chrome.runtime.getManifest().version;
    $("#foot").textContent = `服务在线 · 扩展 v${ext} · 服务 v${d.version || "?"}`;
    return true;
  } catch (e) {
    $("#dot").className = "dot off";
    $("#foot").textContent = "服务未启动：运行 ai-idm serve";
    return false;
  }
}

function makeRow(id) {
  const el = document.createElement("div");
  el.className = "row";
  el.dataset.id = id;
  el.innerHTML = `<span class="nm"></span><span class="bar"><i></i></span>`
    + `<span class="pct"></span><button class="act"></button>`;
  return {
    el,
    nm: el.querySelector(".nm"),
    bar: el.querySelector(".bar > i"),
    pct: el.querySelector(".pct"),
    btn: el.querySelector(".act"),
    st: "",
  };
}

function updateRow(r, t) {
  const st = t.status || "";
  const name = basename(t.dest) || basename(t.url) || t.url || "";
  const pct = Math.max(0, Math.min(100, t.percent || 0));
  const active = st === "pending" || st === "running";
  const id = String(t.id);

  if (r.nm.textContent !== name) r.nm.textContent = name;
  const w = pct.toFixed(0) + "%";
  if (r.bar.style.width !== w) r.bar.style.width = w;
  const pctText = pct.toFixed(0) + "%";
  if (r.pct.textContent !== pctText) r.pct.textContent = pctText;
  const copy = t.dest || t.url || "";
  if (r.el.dataset.copy !== copy) r.el.dataset.copy = copy;
  const title = t.error || t.url || "";
  if (r.el.title !== title) r.el.title = title;
  // 只在状态变化时改 class，别把行内「已复制」反馈抹掉
  if (r.st !== st) { r.el.className = "row " + st; r.st = st; }

  const act = active ? "cancel" : "remove";
  const label = active ? "取消" : "移除";
  if (r.btn.dataset.act !== act) r.btn.dataset.act = act;
  if (r.btn.textContent !== label) r.btn.textContent = label;
  r.btn.disabled = busy.has(id);
}

function render(tasks) {
  const rows = $("#rows");
  const list = tasks || [];
  if (!list.length) {
    if (rowMap.size) { rows.innerHTML = '<div class="empty">暂无任务</div>'; rowMap.clear(); }
    return;
  }
  const empty = rows.querySelector(".empty");
  if (empty) empty.remove();

  const seen = new Set();
  list.forEach((t, i) => {
    const id = String(t.id);
    seen.add(id);
    let r = rowMap.get(id);
    if (!r) { r = makeRow(id); rowMap.set(id, r); }
    updateRow(r, t);
    // 只在该行不在目标位置时移动它，避免每轮都触发重排
    if (rows.children[i] !== r.el) rows.insertBefore(r.el, rows.children[i] || null);
  });
  for (const [id, r] of [...rowMap.entries()]) {
    if (!seen.has(id)) { r.el.remove(); rowMap.delete(id); }
  }
}

async function refresh() {
  const ok = await ping();
  if (!ok) { render([]); return; }
  try {
    const d = await api("/status");
    render(d.tasks || []);
  } catch (e) {
    $("#foot").textContent = String(e.message || e);
  }
}

async function initTab() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    currentUrl = (tab && tab.url) || "";
    const show = currentUrl.length > 90 ? currentUrl.slice(0, 90) + "…" : currentUrl;
    $("#taburl").textContent = show || "（无法读取当前页面）";
    $("#dl").disabled = !/^https?:/i.test(currentUrl);
  } catch (_) {
    $("#taburl").textContent = "（无法读取当前页面）";
  }
}

$("#dl").addEventListener("click", async () => {
  if (!currentUrl) return;
  $("#dl").disabled = true;
  $("#dl").textContent = "发送中…";
  const r = await chrome.runtime.sendMessage({ type: "add", url: currentUrl })
    .catch((e) => ({ ok: false, error: String((e && e.message) || e) }));
  if (r && r.ok) {
    $("#dl").textContent = "已加入队列 ✓";
  } else {
    $("#dl").textContent = "失败：" + ((r && r.error) || "未知错误");
  }
  setTimeout(() => {
    $("#dl").textContent = "下载当前页面";
    $("#dl").disabled = false;
    refresh();
  }, 1200);
});

$("#opts").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});

$("#upd-go").addEventListener("click", async (e) => {
  e.preventDefault();
  const st = await readUpdate();
  chrome.tabs.create({ url: (st && st.url) || RELEASE_PAGE });
});

$("#upd-x").addEventListener("click", async (e) => {
  e.preventDefault();
  const st = await readUpdate();
  if (st && st.latest) {
    // 记下被忽略的版本号：同一个版本不再提示，更高版本会重新出现
    await chrome.storage.local.set({ [UPDATE_KEY]: { ...st, dismissed: st.latest } });
  }
  renderUpdate(null);
  chrome.runtime.sendMessage({ type: "refreshBadge" }).catch(() => {});
});

// 列表用事件委托：行会复用，监听器必须挂在容器上
$("#rows").addEventListener("click", async (e) => {
  const btn = e.target.closest("button.act");
  if (btn) {
    const id = btn.closest(".row") && btn.closest(".row").dataset.id;
    if (!id || busy.has(id)) return;
    busy.add(id);
    btn.disabled = true;
    const verb = btn.dataset.act === "cancel" ? "取消" : "移除";
    try {
      await api(`/${btn.dataset.act}/${encodeURIComponent(id)}`, { method: "POST" });
    } catch (err) {
      $("#foot").textContent = `${verb}失败：${err.message || err}`;
    }
    busy.delete(id);
    refresh();
    return;
  }
  // 点行复制：优先落盘路径，未落盘复制 URL
  const row = e.target.closest(".row[data-copy]");
  if (!row || !row.dataset.copy) return;
  try {
    await navigator.clipboard.writeText(row.dataset.copy);
    $("#foot").textContent = "已复制路径";
    row.classList.add("copied");          // 行内反馈：footer 提示会被下一轮刷新覆盖
    setTimeout(() => row.classList.remove("copied"), 1200);
  } catch (_) {
    $("#foot").textContent = "复制失败（弹窗失焦时剪贴板可能不可用）";
  }
});

(async () => {
  await loadCfg();
  await initTab();
  renderUpdate(await readUpdate().catch(() => null));   // 先画上次检查的结果，别等网络
  chrome.runtime.sendMessage({ type: "checkUpdate" })
    .then((st) => { if (st) renderUpdate(st); })
    .catch(() => { /* worker 没响应：保留上次状态 */ });
  await refresh();
  setInterval(refresh, REFRESH_MS);
})();
