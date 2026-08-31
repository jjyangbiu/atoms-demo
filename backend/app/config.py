"""应用配置：全部经环境变量注入（前缀 ATOMS_），支持 .env。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATOMS_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/atoms.db"
    # 生成文件落盘根目录
    storage_root: str = "./data/storage"
    # 开发默认值须 >=32 字节，否则 PyJWT 报 InsecureKeyLengthWarning；生产务必经环境变量覆盖
    jwt_secret: str = "dev-secret-change-me-0123456789abcdef"
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
    # 每个项目保留的版本快照上限，超出时连同文件清理最旧的（工单 0007）
    snapshot_max_kept: int = 50
    # 生成限流（工单 0011）：每用户每小时生成上限；<=0 表示不限
    rate_limit_per_user_hourly: int = 30
    # 全局同时进行的生成上限（自接受起至流结束）；<=0 表示不限
    rate_limit_max_concurrent: int = 10
    # 模板知识库 / 画廊语义搜索（工单 0009 / ADR 0002）：
    # embedding 默认 MiniMax embo-01（复用 LLM 端点与 Key）；向量库存 Milvus Lite 本地文件。
    # 工单 0012 回归后支持独立的 OpenAI 兼容 embedding 端点（置空则复用 LLM 配置）
    embedding_model: str = "embo-01"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    milvus_uri: str = "./data/milvus/atoms.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
