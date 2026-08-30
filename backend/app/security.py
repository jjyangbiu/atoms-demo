"""密码哈希与 JWT 令牌。"""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from .config import Settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


def create_access_token(settings: Settings, user_id: int, username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expires_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(settings: Settings, token: str) -> dict:
    """解码令牌；失败抛 jwt.PyJWTError。"""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
