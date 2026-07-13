"""FastAPI app factory — composition only (family-bot-8lx.2)."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import api.common as _ac
from config import config
from api.context import ApiContext
from api.routes.auth import build_auth_router
from api.routes.cognition import build_cognition_router
from api.routes.tasks import build_tasks_router
from api.routes.health import build_health_router
from api.routes.today import build_today_router
from api.routes.chat import build_chat_router
from api.routes.activity import build_activity_router
from api.routes.config import build_config_router
from api.routes.realtime import build_realtime_router
from api.routes.home import build_home_router
from api.routes.models import build_models_router
from api.routes.email_keys import build_email_keys_router

log = logging.getLogger(__name__)


def create_api(bot, container):
    """Compose Bernie API: middleware + routers. No route bodies here."""
    import db_writes  # noqa: F401 — ensure write path module loaded

    ctx = ApiContext.from_container(bot, container)
    app = FastAPI(title="Bernie 3.0 API")

    # family-bot-mu2.3: homelab-first CORS — no bare *.
    cors_origins = _ac.config.get("cors_origins")
    if cors_origins is None:
        cors_origins = []
        log.info(
            "cors_origins unset — same-origin only (set explicit list for reverse-proxy/alternate hosts)"
        )
    if cors_origins == ["*"] or cors_origins == "*":
        log.error(
            "cors_origins is '*' — refusing open CORS; using empty allowlist (same-origin only)"
        )
        cors_origins = []
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins if isinstance(cors_origins, list) else list(cors_origins or []),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: blob: https:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'self'",
        )
        # family-bot-x46.3: long-cache fingerprinted static assets (?v=…)
        if request.url.path.startswith("/static") and request.query_params.get("v") is not None:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    app.mount("/static", StaticFiles(directory=f"{_ac.WEB_ROOT}/static"), name="static")

    for builder in (
        build_auth_router,
        build_cognition_router,
        build_tasks_router,
        build_health_router,
        build_today_router,
        build_chat_router,
        build_activity_router,
        build_config_router,
        build_realtime_router,
        build_home_router,
        build_models_router,
        build_email_keys_router,
    ):
        app.include_router(builder(ctx))

    # Cross-container Discord post stays on main.py :9000 internal server only.
    return app
