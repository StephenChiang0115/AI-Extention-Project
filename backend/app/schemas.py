"""請求與回應的資料結構（對應報告 5.5 結構化 JSON Schema）。"""
from typing import Literal, Optional
from pydantic import BaseModel, Field

Emotion = Literal["excited", "calm", "curious", "apologetic"]


class IdentifyRequest(BaseModel):
    image: str = Field(..., description="框選區域的 Base64 影像（可含 data URL 前綴）")
    page_url: Optional[str] = Field(None, alias="pageUrl", description="當前網頁 URL，作為辨識脈絡")
    page_title: Optional[str] = Field(None, alias="pageTitle", description="當前網頁標題，作為辨識脈絡")

    model_config = {"populate_by_name": True}


class Choice(BaseModel):
    label: str
    next_step: int


class Step(BaseModel):
    step_id: int
    dialogue: str
    choices: list[Choice] = []


class Character(BaseModel):
    name: str
    avatar_emotion: Emotion = "calm"


class Script(BaseModel):
    """模型必須輸出的劇本結構；landmark_name 為 None 代表無法辨識。"""
    landmark_name: Optional[str]
    confidence: float = Field(..., ge=0, le=1)
    character: Character
    sequence_steps: list[Step]


class Quota(BaseModel):
    limit: int
    remaining: int
    resets_at: str


class IdentifyResponse(Script):
    cache_hit: bool = False
    image_hash: str
    quota: Quota
