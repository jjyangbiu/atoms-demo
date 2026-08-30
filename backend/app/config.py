"""应用配置：全部经环境变量注入（前缀 ATOMS_），支持 .env。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATOMS_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/atoms.db"
    # 生成文件落盘根目录
    storage_root: str = "./data/storage"
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 60 * 24 * 7
    # 逗号分隔的前端来源（本地开发用）
    cors_origins: str = "http://localhost:5173"


@lru_cache
def get_settings() -> Settings:
    return Settings()
