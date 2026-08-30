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

    model_config = {"from_attributes": True}


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class MessageOut(BaseModel):
    id: int
    role: str
    kind: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}
