"""StudyGuideWorker — generates a prep cheat-sheet for a tagged calendar event.

After generating, stores the guide in task_outputs and enqueues a separate
'study_guide_deliver' cognitive_task with run_at = event_start - lead_time
so the CognitiveWorker picks it up at the right moment for DM delivery.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from cognitive_workers import CognitiveWorkerBase

log = logging.getLogger("bernie.study_guide")


STUDY_SYSTEM = (
    "You are a focused study coach for a household assistant. Given an upcoming event "
    "(test/exam/rehearsal/recital), produce a concise prep summary for the student. "
    "Format the output as plain markdown with these sections only:\n"
    "  ### Key Concepts\n"
    "  ### Quick Drill (3-5 short questions)\n"
    "  ### Last-Minute Tips\n"
    "Keep the whole output under 600 words. No preamble, no commentary. "
    "If you do not have enough domain context, write 'Limited context — focus on:' "
    "and list general prep advice rather than inventing specifics."
)


class StudyGuideWorker(CognitiveWorkerBase):
    name = "study_guide"

    def __init__(self, config: dict):
        cfg = config.get("cognitive_workers", {}).get("study_guide", {})
        self.default_model = cfg.get("default_model", "hermes3:8b-llama3.1-q6_K")
        self.upgrade_model = cfg.get("upgrade_model")
        self.escalate_above_tokens = cfg.get("escalate_above_tokens", 99999)
        self.num_ctx = cfg.get("num_ctx", 8192)
        self.max_runtime_s = cfg.get("max_runtime_s", 120)
        self.lead_time_hours = cfg.get("dm_lead_time_hours", 2)
        self._config = config

    @staticmethod
    def _parse_start(start: str) -> datetime:
        """Best-effort ISO parser. Returns a UTC datetime; falls back to now+6h on garbage."""
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return datetime.now(dt_timezone.utc) + timedelta(hours=6)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt

    async def handle(self, task: dict, container) -> dict:
        db = container.db
        from worker import _call_ollama_topic

        payload = task.get("payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        event_id = payload.get("event_id")
        person_id = payload.get("person_id", "")
        summary = payload.get("summary") or "(untitled)"
        description = payload.get("description") or ""
        start = payload.get("start") or ""

        prompt = (
            f"Student: {person_id}\n"
            f"Event: {summary}\n"
            f"Starts: {start}\n"
            f"Event details:\n{description}\n\n"
            "Produce the markdown cheat-sheet now."
        )
        approx_tokens = len(prompt) // 4
        model = self.pick_model(approx_tokens)
        text, stats = await _call_ollama_topic(
            model, prompt, self._config,
            num_ctx=self.num_ctx, system=STUDY_SYSTEM,
            timeout_s=self.max_runtime_s,
        )
        if not text:
            raise RuntimeError("StudyGuide: Ollama returned no text")

        await db.store_task_output(
            task_id=task.get("id"),
            key=f"study_guide:{event_id}",
            content=text,
        )

        # Schedule delivery: dm_lead_time_hours before event_start.
        # If event is imminent (< lead time), deliver immediately.
        event_start = self._parse_start(start)
        deliver_at = event_start - timedelta(hours=self.lead_time_hours)
        now = datetime.now(dt_timezone.utc)
        if deliver_at < now:
            deliver_at = now
        await db.create_cognitive_task(
            type="study_guide_deliver",
            payload={
                "event_id": event_id, "person_id": person_id,
                "summary": summary, "start": start,
            },
            priority=8,
            run_at=deliver_at.isoformat(),
        )

        return {"_result": {"event_id": event_id, "guide_chars": len(text)}, "_stats": stats}
