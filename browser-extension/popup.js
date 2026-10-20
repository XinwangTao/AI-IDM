/* 弹窗：显示服务状态 + 当前标签页 + 最近任务（1.5s 轮询） */

const $ = (s) => document.querySelector(s);
const DEFAULTS = { port: 13391, token: "", mode: "auto" };

let cfg = { ...DEFAULTS };
let currentUrl = "";

async function loadCfg() {
  const got = await chrome.storage.local.get(Object.keys(DEFAULTS));
  cfg = { ...DEFAULTS, ...got };
}

function fmt(n) {
  if (!n) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let v = n, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
  return (i ? v.toFixed(1) : Math.round(v)) + u[i];
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
    $("#foot").textContent = `服务在线 · v${d.version || "?"}`;
    return true;
  } catch (e) {
    $("#dot").className = "dot off";
    $("#foot").textContent = "服务未启动：运行 ai-idm serve";
    return false;
  }
}

function render(tasks) {
  const rows = $("#rows");
  if (!tasks || !tasks.length) {
    rows.innerHTML = '<div class="empty">暂无任务</div>';
    return;
  }
  rows.innerHTML = tasks.slice(0, 8).map((t) => {
    const st = t.status || "";
    const name = basename(t.dest) || basename(t.url) || t.url;
    const pct = Math.max(0, Math.min(100, t.percent || 0));
    return `<div class="row ${st}" title="${(t.error || t.url || "").replace(/"/g, "&quot;")}">
      <span class="nm">${name}</span>
      <span class="bar"><i style="width:${pct}%"></i></span>
      <span class="pct">${pct.toFixed(0)}%</span>
    </div>`;
  }).join("");
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

(async () => {
  await loadCfg();
  await initTab();
  await refresh();
  setInterval(refresh, 1500);
})();
