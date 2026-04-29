from __future__ import annotations
import asyncio
import os
import logging
from dotenv import load_dotenv

load_dotenv()
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.responses import JSONResponse
from config import Settings
from state import StateHub
from db import Database
from web.auth import BasicAuthMiddleware
from web.routes import (
    dashboard, sse_state, sse_logs, update_settings, get_config,
    list_operations, get_current_operation, start_operation, stop_operation,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(start_engine: bool = True) -> Starlette:
    settings = Settings.from_env()
    state = StateHub(
        hedge_ratio=settings.hedge_ratio,
        max_exposure_pct=settings.max_exposure_pct,
        repost_depth=settings.repost_depth,
    )
    db_path = os.environ.get("DB_PATH", "automoney.db")
    db = Database(db_path)

    async def _load_persisted_config():
        for key, caster, attr in [
            ("hedge_ratio", float, "hedge_ratio"),
            ("max_exposure_pct", float, "max_exposure_pct"),
            ("repost_depth", int, "repost_depth"),
            ("pool_deposited_usd", float, "pool_deposited_usd"),
        ]:
            raw = await db.get_config(key)
            if raw is not None:
                try:
                    setattr(state, attr, caster(raw))
                except ValueError:
                    pass

    @asynccontextmanager
    async def lifespan(app):
        if db._conn is None:
            await db.initialize()
        await _load_persisted_config()
        app.state.settings = settings
        app.state.hub = state
        app.state.db = db
        if start_engine:
            from engine import Engine
            engine = Engine(settings=settings, hub=state, db=db)
            await engine.start()
            app.state.engine = engine
        yield
        if start_engine and hasattr(app.state, 'engine'):
            await app.state.engine.stop()
        await db.close()

    routes = [
        Route("/health", lambda r: JSONResponse({"status": "ok"})),
        Route("/", dashboard),
        Route("/sse/state", sse_state),
        Route("/sse/logs", sse_logs),
        Route("/config", get_config),
        Route("/settings", update_settings, methods=["POST"]),
        Route("/operations", list_operations),
        Route("/operations/current", get_current_operation),
        Route("/operations/start", start_operation, methods=["POST"]),
        Route("/operations/stop", stop_operation, methods=["POST"]),
        Mount("/static", StaticFiles(directory="web/static"), name="static"),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    # Eagerly set state so routes work even without lifespan context (e.g. TestClient without 'with')
    app.state.settings = settings
    app.state.hub = state
    app.state.db = db
    # Eagerly initialize the DB so routes work without lifespan (e.g. TestClient without context manager)
    try:
        asyncio.get_running_loop()
        # Already in an async context; lifespan will handle init
    except RuntimeError:
        asyncio.run(db.initialize())
    app.add_middleware(
        BasicAuthMiddleware,
        username=settings.auth_user,
        password=settings.auth_pass,
        exclude=["/health"],
    )
    return app


app = create_app(start_engine=os.environ.get("START_ENGINE", "false").lower() == "true")
