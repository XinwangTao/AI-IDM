/* AI-IDM 页面内下载浮层（content script）
 *
 * 为什么要有它：接管下载后浏览器自己的下载项被取消，系统通知又可能被
 * Windows 专注助手/权限拦掉，用户会以为"没下成"。这个浮层直接贴在页面
 * 右下角，显示进度/速度/结果，不依赖任何系统通知能力。
 *
 * 1.5.0 起按任务 id 建卡：同一标签页里多个被接管任务各自一张卡，纵向
 * 堆叠（新卡贴近右下角、向上生长），终态停留几秒后从 DOM 移除。
 * 1.7.0 起 × 关闭 = 把任务加入 dismissed 屏蔽集，后续推送不再重建卡片。
 */

(() => {
  if (window.__aiIdmHudInstalled) return;
  window.__aiIdmHudInstalled = true;

  let host = null;
  let root = null;
  let stack = null;

  const cards = new Map();     // tid -> { card, hideTimer }
  const dismissed = new Set(); // 用户点过 × 的任务：本页面会话内不再显示，后续消息一律忽略

  const CSS = `
    :host { all: initial; }
    .stack {
      position: fixed; right: 18px; bottom: 18px; z-index: 2147483647;
      display: flex; flex-direction: column; align-items: flex-end; gap: 10px;
    }
    .card {
      width: 330px;
      font: 13px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
      background: #1f2430; color: #e8ecf3; border-radius: 12px;
      box-shadow: 0 10px 30px rgba(0,0,0,.35); overflow: hidden;
      border: 1px solid rgba(255,255,255,.08);
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
    root.innerHTML = `<style>${CSS}</style><div class="stack"></div>`;
    stack = root.querySelector(".stack");
    (document.body || document.documentElement).appendChild(host);
  }

  function fmt(n) {
    if (!n && n !== 0) return "";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let v = Number(n), i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(1)) + u[i];
  }

  function cardFor(tid) {
    ensure();
    let c = cards.get(tid);
    if (c) return c;
    const card = document.createElement("div");
    card.className = "card hide";
    card.innerHTML = `
      <div class="head">
        <span class="dot"></span>
        <span class="title">AI-IDM 下载中</span>
        <button class="close" title="关闭">×</button>
      </div>
      <div class="name"></div>
      <div class="bar"><div class="fill"></div></div>
      <div class="meta"><span class="left">0%</span><span class="right"></span></div>`;
    stack.appendChild(card);          // 追加到末尾 = 视觉上最贴近右下角，向上生长
    card.querySelector(".close").addEventListener("click", () => dismiss(tid));
    c = { card, hideTimer: null };
    cards.set(tid, c);
    return c;
  }

  function show(tid, p) {
    if (dismissed.has(tid)) return;   // f3：已屏蔽，进度推送不重建卡
    const c = cardFor(tid);
    c.card.classList.remove("hide", "done", "err");
    clearTimeout(c.hideTimer);

    const pct = Math.max(0, Math.min(100, Number(p.percent) || 0));
    const title = c.card.querySelector(".title");
    const left = c.card.querySelector(".left");
    const right = c.card.querySelector(".right");
    const fill = c.card.querySelector(".fill");

    // 三种状态分开显示，避免"排队中"被误读成"卡在 0%"（1.2.1 行为保持）
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
    c.card.querySelector(".name").textContent = p.name || p.url || "";
  }

  function finish(tid, p, ok) {
    if (dismissed.has(tid)) return;   // f3：已屏蔽，终态也不重建卡
    const c = cardFor(tid);
    c.card.classList.remove("hide");
    c.card.classList.toggle("done", ok);
    c.card.classList.toggle("err", !ok);
    clearTimeout(c.hideTimer);
    c.card.querySelector(".title").textContent = ok ? "AI-IDM 下载完成" : "AI-IDM 下载失败";
    c.card.querySelector(".name").textContent = ok ? (p.dest || p.name || p.url || "") : (p.error || "未知错误");
    c.card.querySelector(".fill").style.width = "100%";
    c.card.querySelector(".left").textContent = ok ? "100%" : "";
    c.card.querySelector(".right").textContent = ok ? fmt(p.total) : "";
    // 终态停留后从 DOM 移除（旧版只是隐藏，节点会一直留着）
    c.hideTimer = setTimeout(() => removeCard(tid), ok ? 6000 : 12000);
  }

  function removeCard(tid) {
    const c = cards.get(tid);
    if (!c) return;
    clearTimeout(c.hideTimer);
    c.card.remove();
    cards.delete(tid);
  }

  /** n9：用户点 × —— 屏蔽该任务并立即移除卡片；该任务后续消息走 f3 被忽略。 */
  function dismiss(tid) {
    dismissed.add(tid);
    removeCard(tid);
  }

  chrome.runtime.onMessage.addListener((msg) => {
    // 防御：缺任务 id 或 phase 未知的消息不建卡
    if (!msg || msg.type !== "hud" || !msg.tid) return;
    const p = msg.payload || {};
    if (p.phase === "progress") show(msg.tid, p);
    else if (p.phase === "done") finish(msg.tid, p, true);
    else if (p.phase === "error") finish(msg.tid, p, false);
  });
})();
