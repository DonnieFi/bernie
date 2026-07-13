from cognitive_handlers.registry import task_handler


@task_handler("study_guide")
async def handle_study_guide(task: dict, container) -> dict | None:
    from config import config
    from cognitive_workers.study_guide import StudyGuideWorker

    return await StudyGuideWorker(config).handle(task, container)
