// Content Script：框選畫面 → 截圖裁切 → 送後端 → 以 Shadow DOM 角色呈現劇本。
(() => {
  if (window.__landmarkGuide) return; // 避免重複注入
  const MAX_SIDE = 768;       // 上傳前縮圖的最長邊（降低頻寬與 Token）
  const MIN_BOX = 24;         // 太小的框選視為誤觸
  const CONF_THRESHOLD = 0.6; // 低於此信心值時，角色改用不確定語氣

  const EMOTION_COLOR = { excited: "#f5a623", calm: "#4a90d9", curious: "#7b61c9", apologetic: "#9aa5b1" };

  let host, root, overlay, box, panel, selecting = false, script = null, typer = null;

  // ---------- 掛載 Shadow DOM ----------
  function mount() {
    if (host) return;
    host = document.createElement("div");
    host.id = "landmark-guide-host";
    host.style.cssText = "all:initial;position:fixed;inset:0;z-index:2147483647;pointer-events:none;";
    root = host.attachShadow({ mode: "closed" }); // closed：宿主頁面腳本無法讀取
    root.innerHTML = `
<style>
  *{box-sizing:border-box;font-family:system-ui,"Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif}
  .overlay{position:fixed;inset:0;background:rgba(15,23,42,.35);cursor:crosshair;pointer-events:auto;display:none}
  .overlay.on{display:block}
  .hint{position:fixed;top:16px;left:50%;transform:translateX(-50%);background:#1f2933;color:#fff;padding:8px 14px;border-radius:999px;font-size:13px}
  .box{position:fixed;border:2px solid #4ea1ff;background:rgba(78,161,255,.15);display:none}
  .panel{position:fixed;right:20px;bottom:20px;width:340px;max-width:calc(100vw - 40px);background:#fff;color:#1f2933;border-radius:14px;
    box-shadow:0 10px 30px rgba(0,0,0,.28);padding:14px;pointer-events:auto;display:none;animation:pop .25s ease-out}
  @keyframes pop{from{transform:translateY(12px);opacity:0}to{transform:none;opacity:1}}
  .head{display:flex;align-items:center;gap:10px;margin-bottom:8px}
  .avatar{width:42px;height:42px;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:18px;border:3px solid;flex:none}
  .name{font-weight:700;font-size:14px}.sub{font-size:12px;color:#6b7785}
  .close{margin-left:auto;border:0;background:none;font-size:20px;cursor:pointer;color:#6b7785}
  .text{font-size:14px;line-height:1.65;min-height:48px;white-space:pre-wrap}
  .choices{display:flex;flex-direction:column;gap:6px;margin-top:10px}
  button.c{border:1px solid #c9d3df;background:#f6f9fc;border-radius:8px;padding:8px 10px;font-size:13px;cursor:pointer;text-align:left}
  button.c:hover{background:#e8f1fb;border-color:#4ea1ff}
  .foot{margin-top:10px;font-size:11px;color:#8a96a3;display:flex;justify-content:space-between}
</style>
<div class="overlay"><div class="hint">拖曳框選地標　（Esc 取消）</div></div>
<div class="box"></div>
<div class="panel"></div>`;
    overlay = root.querySelector(".overlay");
    box = root.querySelector(".box");
    panel = root.querySelector(".panel");
    document.documentElement.appendChild(host);
    bindSelection();
  }

  function unmount() {
    stopTyping();
    selecting = false;
    host?.remove();
    host = root = overlay = box = panel = null;
  }

  // ---------- 框選 ----------
  function bindSelection() {
    let sx = 0, sy = 0, dragging = false;
    const rectOf = (e) => ({
      x: Math.min(sx, e.clientX), y: Math.min(sy, e.clientY),
      w: Math.abs(e.clientX - sx), h: Math.abs(e.clientY - sy),
    });
    overlay.addEventListener("mousedown", (e) => {
      dragging = true; sx = e.clientX; sy = e.clientY;
      Object.assign(box.style, { display: "block", left: sx + "px", top: sy + "px", width: "0px", height: "0px" });
      e.preventDefault();
    });
    overlay.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const r = rectOf(e);
      Object.assign(box.style, { left: r.x + "px", top: r.y + "px", width: r.w + "px", height: r.h + "px" });
    });
    overlay.addEventListener("mouseup", (e) => {
      if (!dragging) return;
      dragging = false;
      const r = rectOf(e);
      box.style.display = "none";
      if (r.w < MIN_BOX || r.h < MIN_BOX) return; // 誤觸，繼續等待框選
      stopSelecting();
      identify(r);
    });
  }

  function startSelecting() {
    mount();
    selecting = true;
    overlay.classList.add("on");
  }
  function stopSelecting() {
    selecting = false;
    overlay?.classList.remove("on");
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && selecting) { stopSelecting(); if (!panel || panel.style.display === "none") unmount(); }
  }, true);

  // ---------- 截圖與裁切 ----------
  function loadImage(src) {
    return new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = src; });
  }
  const nextFrame = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));

  async function captureCrop(r) {
    host.style.visibility = "hidden"; // 擷取前先隱藏自己，避免拍到遮罩
    await nextFrame();
    let resp;
    try { resp = await chrome.runtime.sendMessage({ type: "capture" }); }
    finally { host.style.visibility = "visible"; }
    if (!resp?.ok) throw new Error(resp?.error || "無法擷取畫面");
    const img = await loadImage(resp.dataUrl);
    const scale = img.naturalWidth / window.innerWidth; // 對應 devicePixelRatio
    let w = Math.round(r.w * scale), h = Math.round(r.h * scale);
    const shrink = Math.min(1, MAX_SIDE / Math.max(w, h));
    const cw = Math.max(1, Math.round(w * shrink)), ch = Math.max(1, Math.round(h * shrink));
    const canvas = document.createElement("canvas");
    canvas.width = cw; canvas.height = ch;
    canvas.getContext("2d").drawImage(img, r.x * scale, r.y * scale, w, h, 0, 0, cw, ch);
    return canvas.toDataURL("image/jpeg", 0.85);
  }

  // ---------- 辨識流程 ----------
  async function identify(r) {
    showMessage("讓我看看這是什麼地方…", "calm");
    try {
      const image = await captureCrop(r);
      const resp = await chrome.runtime.sendMessage({ type: "identify", image, pageUrl: location.href });
      if (resp?.quotaExceeded) return showQuotaExceeded(resp.quota);
      if (!resp?.ok) throw new Error(resp?.error || "辨識失敗");
      script = resp.data;
      renderScript(script);
    } catch (e) {
      showMessage("抱歉，這裡有點模糊或連線出了問題，再框選一次試試吧！\n（" + (e.message || e) + "）", "apologetic", true);
    }
  }

  // ---------- 角色面板 ----------
  function stopTyping() { if (typer) { clearInterval(typer); typer = null; } }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text; // 一律 textContent，避免模型輸出造成 XSS
    return n;
  }

  function frame(name, sub, emotion) {
    stopTyping();
    panel.replaceChildren();
    panel.style.display = "block";
    const head = el("div", "head");
    const av = el("div", "avatar", (name || "?").trim().charAt(0));
    const color = EMOTION_COLOR[emotion] || EMOTION_COLOR.calm;
    av.style.background = color; av.style.borderColor = color;
    const meta = el("div"); meta.append(el("div", "name", name), el("div", "sub", sub));
    const close = el("button", "close", "×"); close.title = "關閉"; close.addEventListener("click", unmount);
    head.append(av, meta, close);
    const text = el("div", "text"); const choices = el("div", "choices");
    const foot = el("div", "foot");
    panel.append(head, text, choices, foot);
    return { text, choices, foot };
  }

  function typeText(target, full, done) {
    let i = 0;
    typer = setInterval(() => {
      target.textContent = full.slice(0, ++i);
      if (i >= full.length) { stopTyping(); done?.(); }
    }, 18);
    target.addEventListener("click", () => { stopTyping(); target.textContent = full; done?.(); }, { once: true });
  }

  function addChoice(container, label, onClick) {
    const b = el("button", "c", label); b.addEventListener("click", onClick); container.append(b);
  }

  function showMessage(msg, emotion, retry) {
    const { text, choices } = frame("導覽精靈", "", emotion);
    text.textContent = msg;
    if (retry) addChoice(choices, "重新框選", startSelecting);
  }

  function showQuotaExceeded(q) {
    const { text, foot } = frame("導覽精靈", "", "apologetic");
    const when = q?.resets_at ? new Date(q.resets_at).toLocaleString("zh-TW", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "明天";
    text.textContent = `今天的免費導覽次數用完了。額度會在 ${when} 重置，已經看過的熱門景點仍可繼續使用。`;
    foot.textContent = "升級方案即將推出";
  }

  function setFoot(ui, s) {
    ui.foot.replaceChildren(el("span", "", s.cache_hit ? "快取結果" : "AI 即時辨識"),
      el("span", "", s.quota ? `今日剩餘 ${s.quota.remaining} / ${s.quota.limit} 次` : ""));
  }

  function renderScript(s) {
    const unsure = s.landmark_name == null || s.confidence < CONF_THRESHOLD;
    const sub = s.landmark_name == null ? "無法辨識" : unsure ? `可能是：${s.landmark_name}（不太確定）` : s.landmark_name;
    const ui = frame(s.character.name, sub, unsure ? "curious" : s.character.avatar_emotion);
    setFoot(ui, s);
    if (s.landmark_name == null) {
      ui.text.textContent = "我不太確定這是哪裡。可以框選更清楚、範圍更小的地標再試一次嗎？";
      addChoice(ui.choices, "重新框選", startSelecting);
      return;
    }
    renderStep(s, s.sequence_steps[0]?.step_id, ui, unsure);
  }

  function renderStep(s, stepId, ui, unsure) {
    const step = s.sequence_steps.find((x) => x.step_id === stepId);
    ui.choices.replaceChildren();
    if (!step) { ui.text.textContent = "這段導覽到這裡結束囉！"; endButtons(ui); return; }
    const prefix = unsure ? "我不太確定，但看起來像是" + s.landmark_name + "。\n" : "";
    typeText(ui.text, prefix + step.dialogue, () => {
      if (step.choices?.length) {
        step.choices.forEach((c) => addChoice(ui.choices, c.label, () => renderStep(s, c.next_step, ui, false)));
      } else {
        endButtons(ui);
      }
    });
  }

  function endButtons(ui) {
    addChoice(ui.choices, "再框選一個地標", startSelecting);
    addChoice(ui.choices, "結束", unmount);
  }

  // ---------- 與 background 溝通 ----------
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type !== "toggle") return;
    if (selecting) { stopSelecting(); if (panel.style.display === "none") unmount(); }
    else startSelecting();
  });

  window.__landmarkGuide = true;
})();
