from cognitive_handlers.registry import task_handler


@task_handler("reflection")
async def handle_reflection(task: dict, container) -> dict | None:
    from config import config
    from cognitive_workers.reflection import ReflectionWorker

    cal_service = getattr(container, "calendar", None)
    return await ReflectionWorker(config, cal_service=cal_service).handle(task, container)
