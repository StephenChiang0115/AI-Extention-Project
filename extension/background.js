// Service Worker：負責注入腳本、螢幕擷取與呼叫後端。
// 網路請求放在這裡，可避開宿主網頁的 CSP 與混合內容限制。
const DEFAULT_API = "http://localhost:8080";

// Render 免費方案閒置 15 分鐘會休眠，喚醒約需 1 分鐘。
// 但 Chrome 會終止「fetch 超過 30 秒沒回應」的擴充功能 Service Worker，
// 所以喚醒時改用多次短的 /health 請求輪詢，而不是一個等 60 秒的請求。
const WAKE_TOTAL_MS = 90_000;     // 最多等伺服器喚醒 90 秒
const WAKE_ATTEMPT_MS = 20_000;   // 每次 /health 最多等 20 秒（低於 Chrome 的 30 秒限制）
const IDENTIFY_TIMEOUT_MS = 90_000; // 辨識請求的逾時
const WARM_FRESH_MS = 10 * 60_000;  // 10 分鐘內確認過伺服器醒著，就不再喚醒

let lastHealthyAt = 0;

async function apiBase() {
  const { apiBase } = await chrome.storage.sync.get({ apiBase: DEFAULT_API });
  return apiBase.replace(/\/+$/, "");
}

// 匿名用戶識別碼：安裝後第一次使用時產生，之後固定；用於每日額度，不含任何個人資料
async function clientId() {
  const { clientId } = await chrome.storage.local.get("clientId");
  if (clientId) return clientId;
  const id = crypto.randomUUID();
  await chrome.storage.local.set({ clientId: id });
  return id;
}

// 帶逾時的 fetch；逾時會丟出 name 為 "AbortError" 的錯誤
async function fetchWithTimeout(url, options, ms) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  try {
    return await fetch(url, { ...options, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

// 等待期間定期呼叫擴充功能 API，重設 Service Worker 的 30 秒閒置計時器
function keepAlive() {
  const t = setInterval(() => chrome.runtime.getPlatformInfo().catch(() => {}), 20_000);
  return () => clearInterval(t);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 確認後端醒著；休眠中就反覆打 /health 直到醒來或超過總時限。回傳是否成功
async function ensureAwake(base) {
  if (Date.now() - lastHealthyAt < WARM_FRESH_MS) return true;
  const deadline = Date.now() + WAKE_TOTAL_MS;
  const stop = keepAlive();
  try {
    while (Date.now() < deadline) {
      try {
        const res = await fetchWithTimeout(`${base}/health`, {}, Math.min(WAKE_ATTEMPT_MS, deadline - Date.now()));
        if (res.ok) {
          lastHealthyAt = Date.now();
          return true;
        }
      } catch {
        // 逾時或連線失敗（休眠中的服務可能暫時回 502/503 或連不上）：稍等再試
      }
      await sleep(2_000);
    }
    return false;
  } finally {
    stop();
  }
}

// 把網路層錯誤轉成給使用者看的訊息
function friendlyNetworkError(e) {
  if (e && e.name === "AbortError") return "伺服器回應逾時，可能正在喚醒中，請稍候再試一次";
  return "連不上伺服器，請確認網路連線，或伺服器正在喚醒中，請稍候再試";
}

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id || !/^https?:/.test(tab.url || "")) return; // chrome:// 等頁面無法注入
  // 先送一個 /health 叫醒雲端伺服器（不等結果），使用者框選期間它就能開機
  apiBase()
    .then((base) => fetchWithTimeout(`${base}/health`, {}, WAKE_ATTEMPT_MS))
    .then((res) => { if (res.ok) lastHealthyAt = Date.now(); })
    .catch(() => {});
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
  chrome.tabs.sendMessage(tab.id, { type: "toggle" });
});

async function identify(msg) {
  const base = await apiBase();
  if (!(await ensureAwake(base))) {
    return { ok: false, error: "伺服器喚醒中，請稍候一分鐘再試（免費雲端閒置後會休眠）" };
  }
  const stop = keepAlive();
  let res;
  try {
    res = await fetchWithTimeout(`${base}/api/identify`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Client-Id": await clientId() },
      body: JSON.stringify({ image: msg.image, pageUrl: msg.pageUrl, pageTitle: msg.pageTitle }),
    }, IDENTIFY_TIMEOUT_MS);
  } catch (e) {
    lastHealthyAt = 0; // 下次先重新確認伺服器狀態
    return { ok: false, error: friendlyNetworkError(e) };
  } finally {
    stop();
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const d = body.detail;
    if (res.status === 429 && d && typeof d === "object") {
      return { ok: false, quotaExceeded: true, error: d.message, quota: d.quota };
    }
    if ([502, 503, 504].includes(res.status) && typeof d !== "string") {
      // 沒有後端訊息的閘道錯誤，通常是平台正在重啟或喚醒服務
      lastHealthyAt = 0;
      return { ok: false, error: "伺服器喚醒中，請稍候再試" };
    }
    return { ok: false, error: (typeof d === "string" && d) || `伺服器錯誤 (${res.status})` };
  }
  lastHealthyAt = Date.now();
  return { ok: true, data: await res.json() };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (msg.type === "capture") {
      const dataUrl = await chrome.tabs.captureVisibleTab(sender.tab.windowId, { format: "png" });
      return { ok: true, dataUrl };
    }
    if (msg.type === "identify") return identify(msg);
    return { ok: false, error: "未知的訊息類型" };
  })()
    .then(sendResponse)
    .catch((e) => sendResponse({ ok: false, error: e.message || String(e) }));
  return true; // 非同步回應
});
