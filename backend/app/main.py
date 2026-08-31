"""FastAPI 应用工厂。"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import sessionmaker

from .agent.model import default_model_factory
from .config import Settings, get_settings
from .db import Base, make_engine
from .rag.embeddings import default_embedding_factory
from .rate_limit import GenerationLimiter
from .routers import auth, projects, publish, world


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(title="atoms-demo API", version="0.1.0")
    app.state.settings = settings
    # 测试可替换为可编程伪模型工厂（见 tests/fake_model.py）
    app.state.model_factory = default_model_factory
    # 测试可替换为桩 embedding 工厂（见 tests/conftest.py）；知识库懒构建，见 rag.store
    app.state.embedding_factory = default_embedding_factory
    app.state.knowledge_store = None
    # 生成限流器（工单 0011）：限额取自配置，测试可直接替换或改属性/时钟
    app.state.rate_limiter = GenerationLimiter(
        settings.rate_limit_per_user_hourly, settings.rate_limit_max_concurrent
    )

    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app.state.engine = engine
    app.state.session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(publish.router)
    app.include_router(publish.public_router)
    app.include_router(world.router)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    return app
