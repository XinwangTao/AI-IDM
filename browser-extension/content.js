/* AI-IDM 页面内下载浮层（content script）
 *
 * 为什么要有它：接管下载后浏览器自己的下载项被取消，系统通知又可能被
 * Windows 专注助手/权限拦掉，用户会以为"没下成"。这个浮层直接贴在页面
 * 右下角，显示进度/速度/结果，不依赖任何系统通知能力。
 */

(() => {
  if (window.__aiIdmHudInstalled) return;
  window.__aiIdmHudInstalled = true;

  let host = null;
  let root = null;
  let hideTimer = null;

  const CSS = `
    :host { all: initial; }
    .card {
      position: fixed; right: 18px; bottom: 18px; width: 330px;
      font: 13px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
      background: #1f2430; color: #e8ecf3; border-radius: 12px;
      box-shadow: 0 10px 30px rgba(0,0,0,.35); overflow: hidden;
      border: 1px solid rgba(255,255,255,.08); z-index: 2147483647;
      transition: opacity .18s ease, transform .18s ease;
    }
    .card.hide { opacity: 0; transform: translateY(8px); pointer-events: none; }
    .head { display: flex; align-items: center; gap: 8px; padding: 10px 12px 6px; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #3b82f6; flex: none; }
    .card.done .dot { background: #16a34a; }
    .card.err .dot { background: #dc2626; }
    .title { font-weight: 600; font-size: 12px; letter-spacing: .2px; opacity: .95; }
    .close {
      margin-left: auto; cursor: pointer; color: #9aa4b2; font-size: 15px;
      line-height: 1; padding: 2px 4px; border-radius: 4px; background: none; border: 0;
    }
    .close:hover { color: #fff; background: rgba(255,255,255,.08); }
    .name { padding: 0 12px; color: #cfd6e2; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .bar { height: 5px; margin: 9px 12px 0; background: rgba(255,255,255,.10); border-radius: 99px; overflow: hidden; }
    .fill { height: 100%; width: 0%; background: linear-gradient(90deg,#3b82f6,#22d3ee); transition: width .25s ease; }
    .card.done .fill { background: #16a34a; }
    .card.err .fill { background: #dc2626; }
    .meta { display: flex; justify-content: space-between; padding: 6px 12px 10px; color: #9aa4b2; font-size: 11.5px; }
    .meta span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  `;

  function ensure() {
    if (root) return;
    host = document.createElement("div");
    host.id = "ai-idm-hud-host";
    root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>${CSS}</style>
      <div class="card hide" part="card">
        <div class="head">
          <span class="dot"></span>
          <span class="title">AI-IDM 下载中</span>
          <button class="close" title="关闭">×</button>
        </div>
        <div class="name"></div>
        <div class="bar"><div class="fill"></div></div>
        <div class="meta"><span class="left">0%</span><span class="right"></span></div>
      </div>`;
    (document.body || document.documentElement).appendChild(host);
    root.querySelector(".close").addEventListener("click", hide);
  }

  function fmt(n) {
    if (!n && n !== 0) return "";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let v = Number(n), i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(1)) + u[i];
  }

  function show(p) {
    ensure();
    const card = root.querySelector(".card");
    card.classList.remove("hide", "done", "err");
    clearTimeout(hideTimer);

    const pct = Math.max(0, Math.min(100, Number(p.percent) || 0));
    const title = root.querySelector(".title");
    const left = root.querySelector(".left");
    const right = root.querySelector(".right");
    const fill = root.querySelector(".fill");

    // 三种状态分开显示，避免"排队中"被误读成"卡在 0%"
    if (p.queued) {
      title.textContent = "AI-IDM 排队中…";
      left.textContent = "排队中";
      right.textContent = "等待空闲连接";
      fill.style.width = "0%";
    } else if (p.connecting) {
      title.textContent = "AI-IDM 连接中…";
      left.textContent = "连接中";
      right.textContent = "";
      fill.style.width = "0%";
    } else {
      title.textContent = "AI-IDM 下载中";
      fill.style.width = pct + "%";
      left.textContent = pct.toFixed(1) + "%";
      right.textContent =
        [p.speed ? fmt(p.speed) + "/s" : "", p.total ? fmt(p.total) : ""]
          .filter(Boolean).join(" · ");
    }
    root.querySelector(".name").textContent = p.name || p.url || "";
  }

  function finish(p, ok) {
    ensure();
    const card = root.querySelector(".card");
    card.classList.remove("hide");
    card.classList.toggle("done", ok);
    card.classList.toggle("err", !ok);
    clearTimeout(hideTimer);
    root.querySelector(".title").textContent = ok ? "AI-IDM 下载完成" : "AI-IDM 下载失败";
    root.querySelector(".name").textContent = ok ? (p.dest || p.name || p.url || "") : (p.error || "未知错误");
    root.querySelector(".fill").style.width = "100%";
    root.querySelector(".left").textContent = ok ? "100%" : "";
    root.querySelector(".right").textContent = ok ? fmt(p.total) : "";
    hideTimer = setTimeout(hide, ok ? 6000 : 12000);
  }

  function hide() {
    if (!root) return;
    root.querySelector(".card").classList.add("hide");
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || msg.type !== "hud") return;
    const p = msg.payload || {};
    if (p.phase === "progress") show(p);
    else if (p.phase === "done") finish(p, true);
    else if (p.phase === "error") finish(p, false);
  });
})();
