from cognitive_handlers.registry import task_handler


@task_handler("consolidation")
async def handle_consolidation(task: dict, container) -> dict | None:
    from config import config
    from cognitive_workers.consolidation import MemoryConsolidationWorker

    return await MemoryConsolidationWorker(config).handle(task, container)
