/* 设置页：读写 chrome.storage.local，测试与本地服务的连通性 */

const $ = (s) => document.querySelector(s);
const DEFAULTS = { port: 13391, token: "", mode: "auto", forwardCookies: true, notify: true, openFolder: true };

function setMsg(text, kind) {
  const el = $("#msg");
  el.textContent = text;
  el.className = kind || "";
}

async function load() {
  const got = await chrome.storage.local.get(Object.keys(DEFAULTS));
  const c = { ...DEFAULTS, ...got };
  $("#port").value = c.port;
  $("#token").value = c.token;
  $("#mode").value = c.mode;
  $("#cookies").checked = !!c.forwardCookies;
  $("#notify").checked = !!c.notify;
  $("#openFolder").checked = !!c.openFolder;
}

async function save() {
  const port = parseInt($("#port").value, 10) || DEFAULTS.port;
  const c = {
    port,
    token: $("#token").value.trim(),
    mode: $("#mode").value,
    forwardCookies: $("#cookies").checked,
    notify: $("#notify").checked,
    openFolder: $("#openFolder").checked,
  };
  await chrome.storage.local.set(c);
  setMsg("已保存", "ok");
  chrome.runtime.sendMessage({ type: "refreshBadge" }).catch(() => {});
  setTimeout(() => setMsg(""), 2200);
  return c;
}

async function test() {
  const c = await save();
  setMsg("连接中…");
  try {
    const res = await fetch(`http://127.0.0.1:${c.port}/ping`, { cache: "no-store" });
    const d = await res.json();
    if (!d.ok) throw new Error("服务返回异常");
    const st = await fetch(`http://127.0.0.1:${c.port}/status`, {
      headers: { "X-AI-IDM-Token": c.token }, cache: "no-store",
    });
    if (st.status === 401) {
      setMsg(`服务在线（v${d.version}），但令牌不对`, "err");
      return;
    }
    const s = await st.json();
    setMsg(`连接正常 · v${d.version} · 已有 ${(s.tasks || []).length} 个任务`, "ok");
  } catch (e) {
    setMsg("连不上：先运行 ai-idm serve", "err");
  }
}

$("#save").addEventListener("click", save);
$("#test").addEventListener("click", test);
load();
