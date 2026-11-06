"""視覺模型抽象層：換模型或供應商時，只需新增一個 Provider。"""
import os
from abc import ABC, abstractmethod

from .schemas import Character, Choice, Script, Step


class VisionProvider(ABC):
    @abstractmethod
    async def identify(self, image_bytes: bytes, page_url: str | None) -> Script: ...


class MockProvider(VisionProvider):
    """不需要 API 金鑰的假資料；用於開發、測試與前端串接。"""

    async def identify(self, image_bytes: bytes, page_url: str | None) -> Script:
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


class GeminiProvider(VisionProvider):
    """階段 6 實作：呼叫 Gemini 視覺模型並以 Structured Output 回傳 Script。"""

    def __init__(self, api_key: str, model: str):
        self.api_key, self.model = api_key, model

    async def identify(self, image_bytes: bytes, page_url: str | None) -> Script:
        raise NotImplementedError("Gemini 串接尚未實作（階段 6）。請先使用 PROVIDER=mock。")


def get_provider() -> VisionProvider:
    name = os.getenv("PROVIDER", "mock").lower()
    if name == "gemini":
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("PROVIDER=gemini 需要設定 GEMINI_API_KEY")
        return GeminiProvider(key, os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"))
    return MockProvider()
