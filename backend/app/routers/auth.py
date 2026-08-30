"""注册 / 登录 / 登出 / 当前用户。"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_current_user, get_db
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
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误"
        )
    token = create_access_token(request.app.state.settings, user.id, user.username)
    return TokenOut(access_token=token)


@router.post("/logout")
def logout(_: User = Depends(get_current_user)) -> dict:
    # JWT 无状态：登出即客户端丢弃令牌，此端点仅为语义完整
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    return user
