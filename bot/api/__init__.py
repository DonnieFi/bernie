"""Bernie HTTP API package (family-bot-8lx.2).

Public surface matches legacy ``import api``.
"""
from api.common import (
    ConnectionManager,
    Person,
    verify_token,
    verify_bearer_token,
    require_admin,
    ChatRequest,
    LightControl,
    WEB_ROOT,
    config,
    get_model_info,
    BOT_LOG,
)
from api.app import create_api

__all__ = [
    "create_api",
    "ConnectionManager",
    "Person",
    "verify_token",
    "verify_bearer_token",
    "require_admin",
    "ChatRequest",
    "LightControl",
    "WEB_ROOT",
    "config",
    "get_model_info",
    "BOT_LOG",
]
