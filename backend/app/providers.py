"""視覺模型抽象層：換模型或供應商時，只需新增一個 Provider。"""
import base64
import io
import json
import os
import re
from abc import ABC, abstractmethod
from urllib.parse import unquote

import httpx
from PIL import Image

from .schemas import Character, Choice, Script, Step


class VisionProvider(ABC):
    @abstractmethod
    async def identify(self, image_bytes: bytes, page_url: str | None, page_title: str | None = None) -> Script: ...


class MockProvider(VisionProvider):
    """不需要 API 金鑰的假資料；用於開發、測試與前端串接。"""

    async def identify(self, image_bytes: bytes, page_url: str | None, page_title: str | None = None) -> Script:
        return Script(
            landmark_name="台北 101",
            confidence=0.93,
            character=Character(name="101 導覽精靈", avatar_emotion="excited"),
            sequence_steps=[
                Step(
                    step_id=1,
                    dialogue="嗨！你發現了我！我是台北 101，曾經是世界第一高樓哦！你想知道我的地震阻尼器怎麼運作，還是去頂樓看夜景？",
                    choices=[Choice(label="想看阻尼器！", next_step=2), Choice(label="帶我去觀景台！", next_step=3)],
                ),
                Step(step_id=2, dialogue="阻尼器是一顆巨大的鋼球，靠擺動抵銷建築的搖晃，是 101 抗震的關鍵。", choices=[]),
                Step(step_id=3, dialogue="觀景台位於 89 樓，天氣好的時候可以俯瞰整個台北盆地。", choices=[]),
            ],
        )


# ---------------------------------------------------------------------------
# LM Studio（OpenAI 相容 API）
# ---------------------------------------------------------------------------

EMOTIONS = ["excited", "calm", "curious", "apologetic"]

# 給模型的結構化輸出規格（對應 schemas.Script）。手寫而不用 Pydantic 自動產生，
# 是因為本機模型的結構化輸出對 $ref 支援不一，攤平的寫法相容性最好。
SCRIPT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "landmark_name": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "character": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "avatar_emotion": {"type": "string", "enum": EMOTIONS},
            },
            "required": ["name", "avatar_emotion"],
            "additionalProperties": False,
        },
        "sequence_steps": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "step_id": {"type": "integer"},
                    "dialogue": {"type": "string"},
                    "choices": {
                        "type": "array",
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "properties": {"label": {"type": "string"}, "next_step": {"type": "integer"}},
                            "required": ["label", "next_step"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["step_id", "dialogue", "choices"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["landmark_name", "confidence", "character", "sequence_steps"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """你是「AI 景點導覽精靈」的編劇。使用者在網頁上框選了一張圖片，請辨識圖中的地標或景點，
並把那個地標擬人化，用第一人稱、活潑親切的語氣，寫一段可以互動的簡短導覽劇本。

規則：
1. 只輸出一個 JSON 物件，不要有任何其他文字、說明或 Markdown。
2. 全部使用繁體中文（台灣用語）。
3. landmark_name：地標的常用名稱；如果圖中不是可辨識的地標，或你不確定，填 null。
4. confidence：0 到 1 的數字，代表你對辨識結果有多確定。不確定時要誠實給低分（例如 0.3）。
5. character.name：擬人化角色的名字，例如「101 導覽精靈」。avatar_emotion 從 excited、calm、curious、apologetic 選一個。
6. sequence_steps：3 到 5 個步驟。step_id 從 1 開始編號。
   - 第 1 步：打招呼並簡介自己，最後提出問題，給 2 個選項。
   - 每個選項的 next_step 必須指向「存在的」step_id。
   - 最後的步驟 choices 為空陣列 []。
   - 每段 dialogue 不超過 80 個字。
7. 只說你有把握的事實；不確定的數字或年份寧可不提。
8. 使用者正在瀏覽的網頁標題與網址常會直接寫出地點名稱。如果它和圖片內容吻合，請以它為準；
   如果和圖片明顯無關（例如影片標題與畫面無關），就只依圖片判斷。
9. 外觀相似的建築很多（例如中國宮殿式建築），不要直接猜最有名的那一個；沒有把握時降低 confidence。
10. 如果 landmark_name 是 null：confidence 填 0.2 以下，sequence_steps 填 []。

輸出範例：
{"landmark_name":"台北 101","confidence":0.93,"character":{"name":"101 導覽精靈","avatar_emotion":"excited"},
"sequence_steps":[{"step_id":1,"dialogue":"嗨！我是台北 101！想聽我的抗震祕密，還是去觀景台看看？","choices":[{"label":"抗震祕密！","next_step":2},{"label":"去觀景台","next_step":3}]},
{"step_id":2,"dialogue":"我的身體裡有一顆巨大的鋼球阻尼器，會擺動來抵銷搖晃。","choices":[]},
{"step_id":3,"dialogue":"從觀景台可以俯瞰整個台北盆地，天氣好時視野超棒！","choices":[]}]}"""

_UNKNOWN_NAMES = {"", "null", "none", "unknown", "未知", "無法辨識", "不確定"}


def _image_data_url(image_bytes: bytes) -> str:
    try:
        fmt = (Image.open(io.BytesIO(image_bytes)).format or "JPEG").lower()
    except Exception:
        fmt = "jpeg"
    mime = {"jpg": "jpeg", "mpo": "jpeg"}.get(fmt, fmt)
    return f"data:image/{mime};base64,{base64.b64encode(image_bytes).decode()}"


def _extract_json(text: str) -> dict:
    """從模型回覆中取出 JSON 物件：容忍 <think> 推理段落、```json 圍欄與前後多餘文字。"""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"模型回覆中找不到 JSON：{text[:200]!r}")
    return json.loads(text[start:end + 1])


def parse_script(text: str) -> Script:
    """把模型回覆轉成合法的 Script，並修正小模型常見的格式問題。

    無法修正的情況（例如辨識出地標卻沒有任何對話）會丟出例外，
    main.py 會因此回傳 502 並退還使用者額度。
    """
    d = _extract_json(text)

    name = d.get("landmark_name")
    name = str(name).strip() if name is not None else None
    if name is not None and name.lower() in _UNKNOWN_NAMES:
        name = None

    try:
        conf = float(d.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    if 1 < conf <= 100:  # 有些模型會給百分比
        conf /= 100
    conf = min(1.0, max(0.0, conf))

    ch = d.get("character") if isinstance(d.get("character"), dict) else {}
    emotion = ch.get("avatar_emotion") if ch.get("avatar_emotion") in EMOTIONS else "calm"
    char_name = str(ch.get("name") or "").strip() or (f"{name}導覽精靈" if name else "導覽精靈")

    steps: list[Step] = []
    seen: set[int] = set()
    for raw in d.get("sequence_steps") or []:
        if not isinstance(raw, dict):
            continue
        try:
            sid = int(raw.get("step_id"))
        except (TypeError, ValueError):
            continue
        dialogue = str(raw.get("dialogue") or "").strip()
        if sid in seen or not dialogue:
            continue
        seen.add(sid)
        choices = []
        for c in raw.get("choices") or []:
            try:
                choices.append(Choice(label=str(c["label"]), next_step=int(c["next_step"])))
            except (KeyError, TypeError, ValueError):
                continue  # 格式壞掉的選項直接略過
        steps.append(Step(step_id=sid, dialogue=dialogue, choices=choices))

    # 移除指向不存在步驟或指向自己的選項，避免前端點了之後卡住或無限迴圈
    for s in steps:
        s.choices = [c for c in s.choices if c.next_step in seen and c.next_step != s.step_id and c.label.strip()]

    if name is not None and not steps:
        raise ValueError("模型辨識出地標，但沒有產生任何導覽對話")

    return Script(landmark_name=name, confidence=conf,
                  character=Character(name=char_name, avatar_emotion=emotion), sequence_steps=steps)


def page_context(page_url: str | None, page_title: str | None) -> str:
    """把網頁標題與網址整理成給模型的參考文字。

    網址會先解碼：瀏覽器送來的是 %E5%9C%93... 這種編碼，模型看不懂，
    解碼後才看得到「圓山大飯店」這類地名。
    """
    lines = []
    if page_title and page_title.strip():
        lines.append(f"網頁標題：{page_title.strip()[:200]}")
    if page_url:
        lines.append(f"網址：{unquote(page_url)[:300]}")
    if not lines:
        return ""
    return "\n\n使用者正在瀏覽的網頁（參考脈絡）：\n" + "\n".join(lines)


class LMStudioProvider(VisionProvider):
    """呼叫 LM Studio 的本機伺服器（OpenAI 相容的 /v1/chat/completions）。

    需要在 LM Studio 載入「支援圖片輸入」的模型（例如 Qwen2.5-VL、Gemma 3、LLaVA 系列）。
    其他 OpenAI 相容服務（例如 Ollama、vLLM）也可以用同一個 Provider。
    """

    def __init__(self, base_url: str = "http://localhost:1234/v1", model: str | None = None,
                 timeout_s: float = 120.0, api_key: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model or None
        self.timeout_s = timeout_s
        self.api_key = api_key
        self.transport = transport  # 測試用：注入假的 HTTP 傳輸層
        self.use_schema = True      # 伺服器不支援結構化輸出時，會自動改為 False

    def _client(self) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        return httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_s,
                                 headers=headers, transport=self.transport)

    async def _resolve_model(self, client: httpx.AsyncClient) -> str:
        """沒有指定模型時，使用 LM Studio 目前載入的第一個非嵌入模型。"""
        if self.model:
            return self.model
        r = await client.get("/models")
        r.raise_for_status()
        ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
        ids = [i for i in ids if "embed" not in i.lower()]
        if not ids:
            raise RuntimeError("LM Studio 沒有載入任何模型，請先在 LM Studio 載入支援圖片的模型")
        self.model = ids[0]
        return self.model

    async def identify(self, image_bytes: bytes, page_url: str | None, page_title: str | None = None) -> Script:
        user_text = "請辨識這張圖片中的地標，並依規則輸出導覽劇本 JSON。" + page_context(page_url, page_title)

        async with self._client() as client:
            body = {
                "model": await self._resolve_model(client),
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": _image_data_url(image_bytes)}},
                    ]},
                ],
                "temperature": 0.4,
                "max_tokens": 1500,
                "stream": False,
            }
            if self.use_schema:
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "landmark_script", "strict": True, "schema": SCRIPT_JSON_SCHEMA}}

            r = await client.post("/chat/completions", json=body)
            if r.status_code == 400 and "response_format" in body:
                # 部分模型或版本不支援結構化輸出：改用純提示詞，之後不再嘗試
                self.use_schema = False
                body.pop("response_format")
                r = await client.post("/chat/completions", json=body)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"].get("content") or ""

        return parse_script(content)


class GeminiProvider(VisionProvider):
    """預留：呼叫 Gemini 視覺模型並以 Structured Output 回傳 Script。"""

    def __init__(self, api_key: str, model: str):
        self.api_key, self.model = api_key, model

    async def identify(self, image_bytes: bytes, page_url: str | None, page_title: str | None = None) -> Script:
        raise NotImplementedError("Gemini 串接尚未實作。請使用 PROVIDER=lmstudio 或 PROVIDER=mock。")


def get_provider() -> VisionProvider:
    name = os.getenv("PROVIDER", "mock").strip().lower()
    if name == "mock":
        return MockProvider()
    if name == "lmstudio":
        return LMStudioProvider(
            base_url=os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1"),
            model=os.getenv("LMSTUDIO_MODEL") or None,
            timeout_s=float(os.getenv("LMSTUDIO_TIMEOUT_S", "120")),
            api_key=os.getenv("LMSTUDIO_API_KEY") or None,
        )
    if name == "gemini":
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("PROVIDER=gemini 需要設定 GEMINI_API_KEY")
        return GeminiProvider(key, os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"))
    raise RuntimeError(f"未知的 PROVIDER={name!r}（可用：mock、lmstudio、gemini）")
