"""Container view for API route factories (family-bot-8lx.2)."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ApiContext:
    bot: Any
    container: Any
    db: Any
    frigate: Any
    notification_dispatcher: Any
    calendar_service: Any
    weather_module: Any
    ha_service: Any
    summary_builder: Any
    connection_manager: Any
    supervisor: Any
    task_store: Any
    unified_tasks: Any
    http_session: Any
    login_attempts: dict = field(default_factory=dict)

    @classmethod
    def from_container(cls, bot, container) -> "ApiContext":
        return cls(
            bot=bot,
            container=container,
            db=container.db,
            frigate=container.frigate,
            notification_dispatcher=container.notification_orchestrator,
            calendar_service=container.calendar,
            weather_module=container.weather,
            ha_service=container.ha,
            summary_builder=container.summary_builder,
            connection_manager=container.connection_manager,
            supervisor=container.supervisor,
            task_store=container.task_store,
            unified_tasks=container.unified_tasks,
            http_session=container.session,
        )
