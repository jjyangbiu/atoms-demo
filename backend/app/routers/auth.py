"""注册 / 登录 / 登出 / 当前用户。

登录成功除返回 JWT 外，同时写入 HttpOnly Cookie atoms_token：
iframe 预览无法携带 Authorization 头，靠同源 Cookie 自动携带完成鉴权（工单 0005）。
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import COOKIE_NAME, get_current_user, get_db
from ..models import User
from ..schemas import LoginRequest, RegisterRequest, TokenOut, UserOut
from ..security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> User:
    exists = db.scalar(select(User).where(User.username == body.username))
    if exists is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已被占用")
    user = User(username=body.username, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenOut)
def login(
    body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)
) -> TokenOut:
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误"
        )
    settings = request.app.state.settings
    token = create_access_token(settings, user.id, user.username)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.jwt_expires_minutes * 60,
        httponly=True,
        samesite="lax",
    )
    return TokenOut(access_token=token)


@router.post("/logout")
def logout(response: Response, _: User = Depends(get_current_user)) -> dict:
    # JWT 无状态：登出即客户端丢弃令牌，同时清掉预览用的 Cookie
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    return user
