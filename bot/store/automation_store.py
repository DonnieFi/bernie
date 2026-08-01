"""Automation persistence seam — writes route via cognition (40A-5b)."""
from typing import Any, Protocol


async def _routed_write(op: str, /, **kwargs: Any) -> Any:
    from db_client import cognition_db_write

    return await cognition_db_write(op, **kwargs)


class AutomationStore(Protocol):
    async def create_automation(
        self,
        title: str,
        message: str,
        person_id: str,
        schedule_kind: str,
        schedule_payload: dict,
        timezone: str,
        created_by: str,
        audience_scope: str = "self",
        next_run_at: str | None = None,
    ) -> dict: ...

    async def list_all_automations(self) -> list[dict]: ...

    async def set_automation_active(self, automation_id: int, is_active: bool) -> dict | None: ...

    async def delete_automation(self, automation_id: int) -> None: ...


class SQLiteAutomationStore(AutomationStore):
    async def create_automation(self, **kwargs) -> dict:
        return await _routed_write("create_automation", **kwargs)

    async def list_all_automations(self) -> list[dict]:
        from db_binding import get_database
        return await get_database().list_all_automations()

    async def set_automation_active(self, automation_id: int, is_active: bool) -> dict | None:
        return await _routed_write(
            "set_automation_active", automation_id=automation_id, is_active=is_active
        )

    async def delete_automation(self, automation_id: int) -> None:
        await _routed_write("delete_automation", automation_id=automation_id)
