"""API 请求/响应模型。"""

from datetime import datetime

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_\u4e00-\u9fa5]+$")
    password: str = Field(min_length=6, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    mode: str = Field(default="engineer", pattern=r"^(engineer|team)$")


class ProjectOut(BaseModel):
    id: int
    name: str
    mode: str
    created_at: datetime
    updated_at: datetime
    # 活跃发布的稳定 slug；未发布为 None（工单 0006）
    published_slug: str | None = None

    model_config = {"from_attributes": True}


class PublishOut(BaseModel):
    slug: str
    url: str


class WorldAppOut(BaseModel):
    """App 世界画廊条目（工单 0008）：已发布应用的公开卡片信息。"""

    slug: str
    title: str
    description: str
    author: str
    preview_url: str
    published_at: datetime


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class ConfirmPrdRequest(BaseModel):
    """确认 PRD（工单 0010）：可附带追加意见，随确认一并交给工程师。"""

    feedback: str = Field(default="", max_length=8000)


class MessageOut(BaseModel):
    id: int
    role: str
    kind: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class FileOut(BaseModel):
    path: str
    size: int

    model_config = {"from_attributes": True}


class FileContentOut(BaseModel):
    path: str
    size: int
    content: str


class SnapshotOut(BaseModel):
    id: int
    rev: int
    file_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class SnapshotDetailOut(SnapshotOut):
    files: list[FileOut] = []
