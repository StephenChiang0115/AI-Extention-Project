// Service Worker：負責注入腳本、螢幕擷取與呼叫後端。
// 網路請求放在這裡，可避開宿主網頁的 CSP 與混合內容限制。
const DEFAULT_API = "http://localhost:8080";

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

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id || !/^https?:/.test(tab.url || "")) return; // chrome:// 等頁面無法注入
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
  chrome.tabs.sendMessage(tab.id, { type: "toggle" });
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (msg.type === "capture") {
      const dataUrl = await chrome.tabs.captureVisibleTab(sender.tab.windowId, { format: "png" });
      return { ok: true, dataUrl };
    }
    if (msg.type === "identify") {
      const res = await fetch(`${await apiBase()}/api/identify`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Client-Id": await clientId() },
        body: JSON.stringify({ image: msg.image, pageUrl: msg.pageUrl }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const d = body.detail;
        if (res.status === 429 && d && typeof d === "object") {
          return { ok: false, quotaExceeded: true, error: d.message, quota: d.quota };
        }
        return { ok: false, error: (typeof d === "string" && d) || `伺服器錯誤 (${res.status})` };
      }
      return { ok: true, data: await res.json() };
    }
    return { ok: false, error: "未知的訊息類型" };
  })()
    .then(sendResponse)
    .catch((e) => sendResponse({ ok: false, error: e.message || String(e) }));
  return true; // 非同步回應
});
