"""共享依赖：数据库会话与当前用户守卫。"""

from collections.abc import Generator

import jwt as pyjwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import Settings
from .models import User
from .security import decode_access_token

_bearer = HTTPBearer(auto_error=False)

# 预览鉴权用的登录 Cookie 名（iframe 无法携带 Authorization 头，工单 0005）
COOKIE_NAME = "atoms_token"


def resolve_user_by_token(settings: Settings, token: str, db: Session) -> User | None:
    """解析 JWT 返回对应用户；令牌无效或用户不存在返回 None。"""
    try:
        payload = decode_access_token(settings, token)
    except pyjwt.PyJWTError:
        return None
    return db.get(User, int(payload["sub"]))


def get_db(request: Request) -> Generator[Session, None, None]:
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )
    settings = request.app.state.settings
    user = resolve_user_by_token(settings, credentials.credentials, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
