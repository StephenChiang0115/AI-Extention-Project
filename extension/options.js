const input = document.getElementById("api");
chrome.storage.sync.get({ apiBase: "http://localhost:8080" }).then((v) => (input.value = v.apiBase));
document.getElementById("save").addEventListener("click", async () => {
  await chrome.storage.sync.set({ apiBase: input.value.trim() });
  const m = document.getElementById("msg");
  m.textContent = "已儲存";
  setTimeout(() => (m.textContent = ""), 1500);
});
