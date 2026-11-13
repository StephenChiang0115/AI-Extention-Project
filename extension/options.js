const DEFAULT_API = "http://localhost:8080";
const input = document.getElementById("api");
const msg = document.getElementById("msg");

function show(text, isError) {
  msg.textContent = text;
  msg.style.color = isError ? "#c62828" : "#2e7d32";
}

// 只接受 http(s) 網址；回傳去掉結尾斜線的網址，不合法則回傳 null
function normalize(value) {
  try {
    const u = new URL(value.trim());
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    return u.href.replace(/\/+$/, "");
  } catch {
    return null;
  }
}

chrome.storage.sync.get({ apiBase: DEFAULT_API }).then((v) => (input.value = v.apiBase));
document.getElementById("save").addEventListener("click", async () => {
  const url = normalize(input.value || DEFAULT_API);
  if (!url) {
    show("網址格式不正確，請以 http:// 或 https:// 開頭", true);
    return;
  }
  if (url.startsWith("http://") && !/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(url)) {
    show("已儲存（提醒：雲端網址請使用 https://）", false);
  } else {
    show("已儲存", false);
  }
  input.value = url;
  await chrome.storage.sync.set({ apiBase: url });
  setTimeout(() => (msg.textContent = ""), 2500);
});
