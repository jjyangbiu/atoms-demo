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

    # LLM（OpenAI 兼容），默认 MiniMax-M3；API Key 经环境变量注入，测试不依赖
    llm_base_url: str = "https://api.minimaxi.com/v1"
    llm_model: str = "MiniMax-M3"
    llm_api_key: str = ""
    llm_temperature: float = 0.2
    # 智能体单次生成的最大工具循环步数与失败重试次数
    agent_max_steps: int = 20
    agent_max_retries: int = 2
    # 迭代时喂给模型的最近对话轮数（1 轮 = 一问一答）；持久化不受影响（工单 0004）
    agent_history_window: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
