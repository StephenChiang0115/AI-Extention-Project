"""LMStudioProvider 測試：用假的 HTTP 傳輸層模擬 LM Studio，不需要真的開模型。"""
import asyncio
import base64
import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import create_app
from app.providers import LMStudioProvider, get_provider, parse_script
from app.store import MemoryStore

H = {"X-Client-Id": "client-abcdefgh"}

GOOD = {
    "landmark_name": "東京鐵塔",
    "confidence": 0.88,
    "character": {"name": "鐵塔精靈", "avatar_emotion": "excited"},
    "sequence_steps": [
        {"step_id": 1, "dialogue": "嗨！我是東京鐵塔！", "choices": [{"label": "介紹一下", "next_step": 2}]},
        {"step_id": 2, "dialogue": "我高 333 公尺。", "choices": []},
    ],
}


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 80, 40)).save(buf, "JPEG")
    return buf.getvalue()


def chat(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class FakeLMStudio:
    """記錄收到的請求，並依設定回覆。"""

    def __init__(self, content=json.dumps(GOOD, ensure_ascii=False), reject_schema=False,
                 models=("qwen2.5-vl-7b-instruct",), status=200):
        self.content, self.reject_schema, self.models, self.status = content, reject_schema, models, status
        self.requests: list[tuple[str, dict | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.url.path, body))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "text-embedding-nomic"}] + [{"id": m} for m in self.models]})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "boom"})
        if self.reject_schema and "response_format" in body:
            return httpx.Response(400, json={"error": "response_format not supported"})
        return httpx.Response(200, json=chat(self.content))


def provider(fake: FakeLMStudio, **kw) -> LMStudioProvider:
    return LMStudioProvider(base_url="http://lm.test/v1", transport=httpx.MockTransport(fake), **kw)


def run(coro):
    return asyncio.run(coro)


# ---------- 請求內容 ----------
def test_sends_image_schema_and_auto_picks_model():
    fake = FakeLMStudio()
    s = run(provider(fake).identify(jpeg(), "https://www.youtube.com/watch?v=abc"))
    assert s.landmark_name == "東京鐵塔" and s.confidence == 0.88 and len(s.sequence_steps) == 2

    paths = [p for p, _ in fake.requests]
    assert paths == ["/v1/models", "/v1/chat/completions"]
    body = fake.requests[-1][1]
    assert body["model"] == "qwen2.5-vl-7b-instruct"  # 跳過嵌入模型
    assert body["response_format"]["type"] == "json_schema"
    user = body["messages"][1]["content"]
    assert "youtube.com" in user[0]["text"]
    url = user[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == jpeg()


def test_explicit_model_skips_model_lookup_and_sends_api_key():
    fake = FakeLMStudio()
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        return fake(req)

    p = LMStudioProvider(base_url="http://lm.test/v1", model="my-vlm", api_key="k123",
                         transport=httpx.MockTransport(handler))
    run(p.identify(jpeg(), None))
    assert [x for x, _ in fake.requests] == ["/v1/chat/completions"]
    assert fake.requests[0][1]["model"] == "my-vlm" and seen["auth"] == "Bearer k123"


def test_falls_back_when_structured_output_unsupported():
    fake = FakeLMStudio(reject_schema=True)
    p = provider(fake, model="m")
    assert run(p.identify(jpeg(), None)).landmark_name == "東京鐵塔"
    assert "response_format" in fake.requests[0][1] and "response_format" not in fake.requests[1][1]
    run(p.identify(jpeg(), None))  # 第二次直接用純提示詞，不再浪費一次請求
    assert len(fake.requests) == 3


def test_no_model_loaded_raises():
    with pytest.raises(RuntimeError, match="沒有載入"):
        run(provider(FakeLMStudio(models=())).identify(jpeg(), None))


# ---------- 解析與清理 ----------
def test_parse_tolerates_think_fence_and_extra_text():
    text = "<think>看起來是一座紅白色的塔…</think>\n好的，以下是結果：\n```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```\n希望有幫助！"
    assert parse_script(text).landmark_name == "東京鐵塔"


def test_parse_fixes_common_mistakes():
    bad = {
        "landmark_name": "東京鐵塔",
        "confidence": 85,                                  # 百分比
        "character": {"name": "", "avatar_emotion": "happy"},  # 名字空白、情緒不在清單
        "sequence_steps": [
            {"step_id": 1, "dialogue": "嗨！", "choices": [
                {"label": "下一步", "next_step": 2},
                {"label": "壞掉的", "next_step": 99},       # 指向不存在的步驟
                {"label": "繞圈", "next_step": 1},          # 指向自己
                {"label": "缺欄位"},
            ]},
            {"step_id": 2, "dialogue": "結束了。"},          # 沒有 choices
            {"step_id": 2, "dialogue": "重複的 id"},
            {"step_id": 3, "dialogue": ""},                 # 空白對話
        ],
    }
    s = parse_script(json.dumps(bad, ensure_ascii=False))
    assert s.confidence == 0.85
    assert s.character.name == "東京鐵塔導覽精靈" and s.character.avatar_emotion == "calm"
    assert [x.step_id for x in s.sequence_steps] == [1, 2]
    assert [(c.label, c.next_step) for c in s.sequence_steps[0].choices] == [("下一步", 2)]
    assert s.sequence_steps[1].choices == [] and s.sequence_steps[1].dialogue == "結束了。"


def test_parse_unknown_landmark():
    s = parse_script('{"landmark_name": "無法辨識", "confidence": 0.1, "character": {"name": "精靈", "avatar_emotion": "apologetic"}, "sequence_steps": []}')
    assert s.landmark_name is None and s.sequence_steps == []


@pytest.mark.parametrize("text", ["抱歉，我無法判斷。", '{"landmark_name": "台北 101", "confidence": 0.9, "sequence_steps": []}', "{壞掉的 json"])
def test_parse_rejects_unusable_output(text):
    with pytest.raises(ValueError):
        parse_script(text)


# ---------- 與 API 串接 ----------
def make_api(fake, limit=3):
    store = MemoryStore(daily_limit=limit)
    return TestClient(create_app(provider(fake, model="m"), store)), store


def test_api_end_to_end_with_lmstudio():
    fake = FakeLMStudio()
    c, store = make_api(fake)
    img = base64.b64encode(jpeg()).decode()
    r = c.post("/api/identify", json={"image": img}, headers=H)
    assert r.status_code == 200 and r.json()["landmark_name"] == "東京鐵塔" and r.json()["cache_hit"] is False
    r = c.post("/api/identify", json={"image": img}, headers=H)
    assert r.json()["cache_hit"] is True and len(fake.requests) == 1  # 第二次走快取，不呼叫模型
    assert store.quota_remaining("client-abcdefgh") == 2


@pytest.mark.parametrize("fake", [FakeLMStudio(status=500), FakeLMStudio(content="我看不出來這是什麼")])
def test_api_model_error_returns_502_and_refunds(fake):
    c, store = make_api(fake)
    r = c.post("/api/identify", json={"image": base64.b64encode(jpeg()).decode()}, headers=H)
    assert r.status_code == 502 and store.quota_remaining("client-abcdefgh") == 3


def test_api_connection_refused_returns_502():
    def down(req):
        raise httpx.ConnectError("connection refused")

    store = MemoryStore(daily_limit=3)
    p = LMStudioProvider(base_url="http://lm.test/v1", model="m", transport=httpx.MockTransport(down))
    c = TestClient(create_app(p, store))
    r = c.post("/api/identify", json={"image": base64.b64encode(jpeg()).decode()}, headers=H)
    assert r.status_code == 502 and store.quota_remaining("client-abcdefgh") == 3


# ---------- 設定 ----------
def test_get_provider_from_env(monkeypatch):
    monkeypatch.setenv("PROVIDER", "lmstudio")
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:5555/v1/")
    monkeypatch.setenv("LMSTUDIO_MODEL", "gemma-3-4b")
    p = get_provider()
    assert isinstance(p, LMStudioProvider) and p.base_url == "http://127.0.0.1:5555/v1" and p.model == "gemma-3-4b"
    monkeypatch.setenv("PROVIDER", "lmstudoi")
    with pytest.raises(RuntimeError, match="未知的 PROVIDER"):
        get_provider()
