"""FastAPI 应用工厂。"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import sessionmaker

from .config import Settings, get_settings
from .db import Base, make_engine
from .routers import auth


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(title="atoms-demo API", version="0.1.0")
    app.state.settings = settings

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

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    return app
