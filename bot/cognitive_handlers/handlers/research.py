from cognitive_handlers.registry import task_handler


@task_handler("research")
async def handle_research(task: dict, container) -> dict | None:
    from config import config
    from cognitive_workers.research import ResearchWorker

    return await ResearchWorker(config).handle(task, container)
