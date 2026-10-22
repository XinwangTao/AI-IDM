/* AI-IDM 页面内下载浮层（content script）
 *
 * 为什么要有它：接管下载后浏览器自己的下载项被取消，系统通知又可能被
 * Windows 专注助手/权限拦掉，用户会以为"没下成"。这个浮层直接贴在页面
 * 右下角，显示进度/速度/结果，不依赖任何系统通知能力。
 *
 * 1.5.0 起按任务建卡：同一标签页里多个被接管任务各自一张卡，纵向堆叠
 * （新卡贴近右下角、向上生长），终态停留几秒后从 DOM 移除。
 * 1.7.0 起 × 关闭 = 把任务加入 dismissed 屏蔽集，后续推送不再重建卡片。
 * 1.9.1 起按客户端 key 建卡（提交 /add 之前就先出卡，返回后沿用同一张），
 * 并用上次速度做 200ms 插值 + ETA，进度不再 1.5s 一跳一跳。
 */

(() => {
  if (window.__aiIdmHudInstalled) return;
  window.__aiIdmHudInstalled = true;

  let host = null;
  let root = null;
  let stack = null;

  const cards = new Map();     // key -> { el, title, name, fill, left, right, hideTimer, st }
  const dismissed = new Set(); // 用户点过 × 的任务：本页面会话内不再显示，后续消息一律忽略
  let ticker = null;           // 200ms 插值定时器：只在有进行中任务时运行

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
    .fill { height: 100%; width: 0%; background: linear-gradient(90deg,#3b82f6,#22d3ee); transition: width .2s linear; }
    .card.done .fill { background: #16a34a; }
    .card.err .fill { background: #dc2626; }
    .meta { display: flex; justify-content: space-between; gap: 10px; padding: 6px 12px 10px; color: #9aa4b2; font-size: 11.5px; }
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

  function etaText(st, shown) {
    if (!st.total || !st.speed || st.speed <= 0) return "";
    const remain = st.total * (1 - Math.min(shown, 100) / 100);
    const sec = remain / st.speed;
    if (!isFinite(sec) || sec <= 0) return "";
    const m = Math.floor(sec / 60), s = Math.round(sec % 60);
    return m >= 60
      ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}m`
      : `${m}:${String(Math.min(s, 59)).padStart(2, "0")}`;
  }

  function cardFor(key) {
    ensure();
    let c = cards.get(key);
    if (c) return c;
    const el = document.createElement("div");
    el.className = "card hide";
    el.innerHTML = `
      <div class="head">
        <span class="dot"></span>
        <span class="title">AI-IDM 下载中</span>
        <button class="close" title="关闭">×</button>
      </div>
      <div class="name"></div>
      <div class="bar"><div class="fill"></div></div>
      <div class="meta"><span class="left">0%</span><span class="right"></span></div>`;
    stack.appendChild(el);            // 追加到末尾 = 视觉上最贴近右下角，向上生长
    el.querySelector(".close").addEventListener("click", () => dismiss(key));
    c = {
      el,
      title: el.querySelector(".title"),
      name: el.querySelector(".name"),
      fill: el.querySelector(".fill"),
      left: el.querySelector(".left"),
      right: el.querySelector(".right"),
      hideTimer: null,
      st: { running: false, percent: 0, speed: 0, total: 0, connections: 0, at: 0 },
    };
    cards.set(key, c);
    return c;
  }

  /** 按速度外推一帧。真实值每 0.5s 到一次，中间靠它把进度条推得连续。 */
  function paint(c, now) {
    const st = c.st;
    if (!st.running) return;
    const dt = (now - st.at) / 1000;
    let shown = st.percent;
    if (st.speed > 0 && st.total > 0 && dt > 0 && dt <= 2) {
      shown = st.percent + (st.speed * dt / st.total) * 100;
      shown = Math.min(shown, st.percent + 15);   // 外推最多 15 个点，别替服务端说谎
    }
    shown = Math.min(shown, 99);                  // 100% 只由 done 给
    c.fill.style.width = shown + "%";
    c.left.textContent = shown.toFixed(0) + "%";
    c.right.textContent = [
      st.speed ? fmt(st.speed) + "/s" : "",
      etaText(st, shown) ? "剩 " + etaText(st, shown) : "",
      st.connections ? st.connections + " 连接" : "",
    ].filter(Boolean).join(" · ");
  }

  function ensureTicker() {
    if (ticker) return;
    ticker = setInterval(() => {
      const now = Date.now();
      let active = false;
      for (const c of cards.values()) {
        // 2s 没新数据后 paint 本来就会冻结在同一帧；此时不再当活跃卡，停表等下次真值。
        if (c.st.running && now - c.st.at <= 2000) { paint(c, now); active = true; }
      }
      if (!active) { clearInterval(ticker); ticker = null; }
    }, 200);
  }

  function show(key, p) {
    if (dismissed.has(key)) return;   // 已屏蔽：进度推送不重建卡
    const c = cardFor(key);
    c.el.classList.remove("hide", "done", "err");
    clearTimeout(c.hideTimer);
    c.name.textContent = p.name || p.url || "";

    const st = c.st;
    st.speed = Number(p.speed) || 0;
    st.total = Number(p.total) || 0;
    if (p.connections) st.connections = Number(p.connections) || 0;

    // 四种阶段分开显示：准备接管 / 排队 / 连接 / 下载，避免被误读成"卡在 0%"
    if (p.handoff) {
      st.running = false;
      c.title.textContent = "AI-IDM 正在接管…";
      c.left.textContent = "准备中";
      c.right.textContent = "";
      c.fill.style.width = "0%";
    } else if (p.queued) {
      st.running = false;
      c.title.textContent = "AI-IDM 排队中…";
      c.left.textContent = "排队中";
      c.right.textContent = "等待空闲连接";
      c.fill.style.width = "0%";
    } else if (p.connecting) {
      st.running = false;
      c.title.textContent = "AI-IDM 连接中…";
      c.left.textContent = "连接中";
      c.right.textContent = st.connections ? st.connections + " 连接" : "";
      c.fill.style.width = "0%";
    } else {
      c.title.textContent = "AI-IDM 下载中";
      st.percent = Math.max(0, Math.min(100, Number(p.percent) || 0));
      st.at = Date.now();
      st.running = true;
      paint(c, st.at);              // 真值到达立刻对齐，不留旧帧
      ensureTicker();
    }
  }

  function finish(key, p, ok) {
    if (dismissed.has(key)) return;   // 已屏蔽，终态也不重建卡
    const c = cardFor(key);
    const st = c.st;
    st.running = false;
    c.el.classList.remove("hide");
    c.el.classList.toggle("done", ok);
    c.el.classList.toggle("err", !ok);
    clearTimeout(c.hideTimer);
    c.title.textContent = ok ? "AI-IDM 下载完成" : "AI-IDM 下载失败";
    c.name.textContent = ok ? (p.dest || p.name || p.url || "") : (p.error || "未知错误");
    c.fill.style.width = "100%";
    c.left.textContent = ok ? "100%" : "";
    c.right.textContent = ok ? fmt(p.total) : "";
    // 终态停留后从 DOM 移除（旧版只是隐藏，节点会一直留着）
    c.hideTimer = setTimeout(() => removeCard(key), ok ? 6000 : 12000);
  }

  function removeCard(key) {
    const c = cards.get(key);
    if (!c) return;
    clearTimeout(c.hideTimer);
    c.el.remove();
    cards.delete(key);
  }

  /** 用户点 × —— 屏蔽该任务并立即移除卡片；该任务后续消息一律忽略。 */
  function dismiss(key) {
    dismissed.add(key);
    removeCard(key);
  }

  chrome.runtime.onMessage.addListener((msg) => {
    // 防御：缺 key 或 phase 未知的消息不建卡
    const key = msg && (msg.key || msg.tid);
    if (!msg || msg.type !== "hud" || !key) return;
    const p = msg.payload || {};
    if (p.phase === "progress") show(key, p);
    else if (p.phase === "done") finish(key, p, true);
    else if (p.phase === "error") finish(key, p, false);
    else if (p.phase === "gone") removeCard(key);   // 该标签页不再是显示目标：只移除，不屏蔽
  });
})();
