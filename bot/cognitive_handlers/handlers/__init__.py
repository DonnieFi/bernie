"""Import all handlers so @task_handler registrations run at worker startup."""

from . import (  # noqa: F401
    consolidation,
    discord_reply,
    reflection,
    research,
    research_deliver,
    study_guide,
    study_guide_deliver,
)
