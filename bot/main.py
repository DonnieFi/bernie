import asyncio
import os
import logging
import argparse
import uvicorn
import httpx
from anthropic import AsyncAnthropic
from service_container import ServiceContainer
import bot as bot_module
bot = bot_module.bot
from config import config
from presence_service import presence_service
from calendar_service import CalendarService
import weather_service
from api import create_api, ConnectionManager
from ha_service import ha_service
import summary_builder
import identity_service
import network_service
import frigate_service
import litellm_service
import claude_service
from notification_router import NotificationRouter
import database
import db_writes
from constants import registry as person_registry, _rebuild_legacy as _rebuild_person_legacy
from nightly_digest import nightly_digest_loop
from frigate_listener import frigate_listener_loop

# Configure logging — LOG_PREFIX is set per container in the split compose
# (e.g. [discord], [api], [cognition]); defaults to [bernie] in monolith mode.
_log_prefix = os.environ.get("LOG_PREFIX", "[bernie]")
logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s {_log_prefix} [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bernie-main")

async def main(role: str = "monolith"):
    """Top-level entry point. Dispatches to the appropriate role coroutine
    based on --role (or ROLE env var). Monolith is the default and preserves
    100% of current behavior during the shadow period.
    """
    if role == "discord":
        await run_discord_role()
    elif role == "api":
        await run_api_role()
    elif role == "cognition":
        await run_cognition_role()
    else:
        await run_monolith_role()


# ── Role coroutines (Wave 2b container split) ────────────────────────────────

async def _common_setup(role: str = "monolith"):
    """Shared initialization used by all roles.

    This function now accepts a role hint so it can skip work that belongs
    exclusively to other containers. This is the first light conditionalization
    step — only obviously role-owned pieces are guarded.

    - discord role: owns presence, Discord client wiring, frigate listener
    - api role: owns the FastAPI server
    - cognition role: owns CognitiveWorker / WatchdogWorker registration + BTS
    - monolith: gets everything (unchanged behavior)

    Returns a dict containing only the objects the caller is expected to use.
    """
    loop = asyncio.get_running_loop()
    loop.slow_callback_duration = 0.2
    logger.info(f"Starting Bernie 3.0 system (role={role})...")

    person_registry.load(config)
    _rebuild_person_legacy(config)
    if role in ("cognition", "monolith"):
        for _attempt in range(10):
            try:
                await database.init_db()
                break
            except Exception as _e:
                if "database is locked" in str(_e).lower() and _attempt < 9:
                    logger.warning(f"init_db locked, retry {_attempt+1}/10...")
                    await asyncio.sleep(3)
                else:
                    raise
    else:
        logger.info("Skipping init_db for role=%s (read-only data mount; cognition owns schema)", role)

    from llm.model_state import set_model
    active_model = config.get("active_model")
    if active_model:
        set_model(active_model)

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        logger.error("DISCORD_TOKEN environment variable not set.")
        return {"token": None, "container": None, "server": None, "bot": None}

    calendar_service = CalendarService(config)

    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo
    _tz = ZoneInfo(config.get("timezone", "America/Halifax"))

    from supervisor import init_supervisor
    supervisor = init_supervisor(bot)

    from background_scheduler import init_scheduler
    bts = init_scheduler(supervisor)

    from watchman import init_watchman
    notification_orchestrator = NotificationRouter(bot)
    init_watchman(bot, router=notification_orchestrator, db_module=database)

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        logger.error("ANTHROPIC_API_KEY environment variable not set.")
        return {"token": None, "container": None, "server": None, "bot": None}

    anthropic_client = AsyncAnthropic(api_key=anthropic_key)

    litellm_base_url = config.get("litellm_base_url", "https://litellm.example.local")
    litellm_key = os.environ.get("LTE_LLM_MASTER_KEY", anthropic_key)

    verify = True
    if "litellm.example.local" in litellm_base_url:
        if os.path.exists("/app/caddy-root.crt"):
            verify = "/app/caddy-root.crt"
        elif os.path.exists("bot/caddy-root.crt"):
            verify = "bot/caddy-root.crt"

    litellm_http = httpx.AsyncClient(verify=verify)
    litellm_client = AsyncAnthropic(
        base_url=litellm_base_url,
        api_key=litellm_key,
        default_headers={"x-litellm-api-key": litellm_key},
        http_client=litellm_http
    )
    litellm_client._owned_http_client = litellm_http

    from llm.clients import make_openrouter_client
    from openrouter_models import openrouter_api_key, openrouter_direct_enabled

    openrouter_client = None
    if openrouter_direct_enabled(config):
        try:
            openrouter_client = make_openrouter_client(openrouter_api_key(config))
            logger.info("OpenRouter direct client enabled (bypassing LiteLLM for or-* models)")
        except Exception as exc:
            logger.error("OpenRouter client init failed: %s — or-* models may fall back to LiteLLM", exc)

    from http_session import make_shared_session

    # family-bot-1bf.1: never create a bare ClientSession without timeouts
    shared_session = make_shared_session()

    from store.task_store import SQLiteTaskStore
    from store.automation_store import SQLiteAutomationStore
    from services.unified_task_service import UnifiedTaskService
    t_store = SQLiteTaskStore()
    a_store = SQLiteAutomationStore()
    u_tasks = UnifiedTaskService(
        task_store=t_store,
        person_registry=None,
        config=config,
        notification_router=notification_orchestrator,
    )
    container = ServiceContainer(
        db=database,
        task_store=t_store,
        automation_store=a_store,
        unified_tasks=u_tasks,
        calendar=calendar_service,
        connection_manager=ConnectionManager(),
        notification_orchestrator=notification_orchestrator,
        supervisor=supervisor,
        scheduler=bts,
        session=shared_session,
        frigate=frigate_service.frigate_service,
        ha=ha_service,
        identity=identity_service,
        network=network_service.network_service,
        presence=presence_service,
        anthropic=anthropic_client,
        litellm=litellm_client,
        openrouter=openrouter_client,
        ollama=config.get("ollama_base_url", "http://192.168.1.X:11434"),  # placeholder; set ollama_base_url in config.json
        litellm_admin=litellm_service,
        weather=weather_service,
        summary_builder=summary_builder,
        tz=_tz
    )

    # Cognition workers (CognitiveWorker + WatchdogWorker) belong to the
    # cognition container. Only register them when we are cognition or monolith.
    if role in ("cognition", "monolith"):
        from worker import init_workers
        cognitive_worker, watchdog_worker = init_workers(container)
        cognitive_worker.register_with_bts(bts)
        watchdog_worker.register_with_bts(bts)

        container.cognitive_worker = cognitive_worker
        container.watchdog_worker = watchdog_worker

    bot_module._init(container)
    claude_service._init(container)

    # Ensure tool domains + modes for gateway and surface math (Wave 1a).
    # Validation fails loud on bad domains in modes/channel map/discovery list or broken YAML.
    # Loads are idempotent. Discord on_ready also warms domains; we validate early for all roles.
    from tools import load_all_domains
    from modes import load_all_modes
    from llm.tool_surface import validate_tool_surface_at_startup
    load_all_domains()
    load_all_modes()
    validate_tool_surface_at_startup(config)
    logger.info("Tool domains + surface validated for role=%s", role)

    from litellm_service import _refresh_model_cache_modes
    asyncio.create_task(_refresh_model_cache_modes())

    container.ha.set_broadcaster(container.connection_manager.broadcast)

    # FastAPI server is only needed by the api role (and monolith for now).
    server = None
    if role in ("api", "monolith"):
        app = create_api(bot, container)
        uvicorn_config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
        server = uvicorn.Server(uvicorn_config)

    # Presence / Discord client wiring / frigate listener belong to the
    # discord role. Skip the whole block for pure api or cognition roles.
    if role in ("discord", "monolith"):
        logger.info("Launching Discord bot and FastAPI server...")

        async def _handle_presence_change(name: str, is_home: bool, status_label: str = None):
            person_id = person_registry.resolve(name) or name
            display = person_registry.first_name(person_id) if person_id else name
            now_local = datetime.now(_tz)
            now_str = now_local.strftime("%-I:%M %p")
            if is_home:
                verb = "arrived home"
                sub = f"Home since {now_str}"
            else:
                loc = status_label if status_label else "Away"
                verb = f"departed (to {loc})" if loc != "Away" else "departed"
                sub = f"{loc} since {now_str}"
            await db_writes.log_activity(
                "presence", f"<b>{display}</b> {verb}", "Source: Unifi", "Presence", person_id=person_id,
            )
            await container.connection_manager.broadcast({
                "type": "presence.update",
                "id": person_id,
                "home": is_home,
                "sub": sub,
                "last_seen_ts": datetime.now(timezone.utc).isoformat()
            })

        async def presence_arrived(name: str):
            # No notification flush on arrival — the overnight queue drains via the
            # scheduled quiet_hours_flush job (and /reminders on), not on return home.
            # Flushing here dumped the whole backlog at once every time you walked in.
            await _handle_presence_change(name, True)

        async def presence_departed(name: str, status_label: str = None):
            await _handle_presence_change(name, False, status_label=status_label)

        async def friend_arrived(label: str, mac: str):
            channel = bot.get_channel(int(config.get("schedule_channel_id", 0)))
            if channel:
                await channel.send(f"👋 {label} has arrived!")

        container.presence.on_arrive(presence_arrived)
        container.presence.on_depart(presence_departed)
        container.presence.on_friend_arrive(friend_arrived)

        # === Role responsibilities (Wave 2b split) ===
        # - discord / monolith roles: own Discord client, start HA WebSocket,
        #   and receive real-time presence events.
        # - cognition role: runs workers, posts back to Discord via /internal/post
        #   (see cross_container.py). It does NOT start the HA WebSocket.
        # - api role: handles OpenWebUI + internal tools.

        # Wire real-time HA WebSocket presence events into the presence service
        container.ha._on_person_state_change = container.presence._on_person_state_change

        await container.presence.start()

        try:
            await container.ha.refresh_entities()
            await container.ha.start_websocket()
        except Exception as e:
            logger.warning(f"Initial HA entity refresh failed: {e}")

    # Return only what the role actually needs.
    # Callers are responsible for using only the keys that belong to them.
    return {
        "token": token,
        "container": container,
        "server": server,
        "bot": bot,
    }


async def run_discord_role():
    """Discord-only container role (bot client, reminders, slash commands, presence, frigate listener).

    Only extracts the keys it actually needs from the role-aware _common_setup().
    """
    setup = await _common_setup("discord")
    token = setup["token"]
    bot = setup["bot"]
    if not token:
        return

    # Wave 2b internal cross-container posting endpoint — only served by discord role.
    # Runs a minimal FastAPI app on port 9000 (internal only, not published to host).
    # Cognition containers reach it via http://bernie-discord:9000 over bernie-net.
    internal_task = asyncio.create_task(_run_internal_post_server(bot))

    logger.info("Launching Discord-only role (bot + frigate listener + internal post server)...")
    try:
        await asyncio.gather(
            bot.start(token),
            frigate_listener_loop(bot),
        )
    except Exception as e:
        logger.error(f"Error running Discord role: {e}")
    finally:
        internal_task.cancel()
        await setup["container"].aclose()
        if not bot.is_closed():
            await bot.close()


async def _run_internal_post_server(bot):
    """Minimal internal-only FastAPI server for Wave 2b cross-container posting."""
    import uvicorn
    from internal_discord import create_internal_discord_app

    internal_app = create_internal_discord_app(bot)
    cfg = uvicorn.Config(internal_app, host="0.0.0.0", port=9000, log_level="warning")
    server = uvicorn.Server(cfg)
    await server.serve()


async def run_api_role():
    """API-only container role (FastAPI + web dashboard).

    Only extracts the keys it actually needs from the role-aware _common_setup().
    """
    setup = await _common_setup("api")
    server = setup["server"]
    if not server:
        return
    logger.info("Launching API-only role (FastAPI server)...")
    try:
        await asyncio.gather(
            server.serve(),
        )
    except Exception as e:
        logger.error(f"Error running API role: {e}")
    finally:
        await setup["container"].aclose()
        if not setup["bot"].is_closed():
            await setup["bot"].close()


async def run_cognition_role():
    """Cognition-only container role (CognitiveWorker, WatchdogWorker, BTS,
    nightly_digest_loop, reflection/consolidation/research workers, Watchman,
    etc.).

    Only extracts the keys it actually needs from the role-aware _common_setup().
    """
    setup = await _common_setup("cognition")
    if not setup.get("container"):
        return

    logger.info("Launching Cognition-only role (workers + BTS + nightly digest)...")

    # 40A-1: inbound write RPC for discord/api roles (sole SQLite writer).
    from cognition_write import run_internal_write_server
    internal_write_task = asyncio.create_task(run_internal_write_server())

    from background_scheduler import get_scheduler
    bts = get_scheduler()

    # 40A-2: cognition overnight/enqueue BTS jobs (not registered on discord).
    bot_module.register_cognition_bts_tasks(bts)

    # The CognitiveWorker and WatchdogWorker were registered with BTS during
    # _common_setup() (only for cognition/monolith roles). We start the
    # scheduler here so the workers actually poll.
    await bts.start_all()

    try:
        from migrate_tasks_v32 import migrate as _migrate_tasks

        _n = await _migrate_tasks()
        if _n:
            logger.info("Cognition startup: unified_tasks migration copied %d row(s)", _n)
    except Exception as exc:
        logger.warning("Cognition startup: unified_tasks migration failed (non-fatal): %s", exc)

    # 40B-2A: prune + optional VACUUM on cognition startup (replaces Sunday digest VACUUM).
    try:
        counts = await database.prune_logs(retention_days=30)
        total = sum(counts.values())
        if total:
            logger.info("Cognition startup: pruned %d old log rows — %s", total, counts)
        if await database.maybe_maintenance_vacuum():
            logger.info("Cognition startup: maintenance VACUUM complete")
    except Exception as exc:
        logger.error("Cognition startup DB maintenance failed: %s", exc)

    try:
        await asyncio.gather(
            nightly_digest_loop(config),
            # Other cognition-driven tasks (Reflection, Consolidation, Research,
            # Watchman audit, etc.) are scheduled via the
            # CognitiveWorker / WatchdogWorker + BackgroundTaskScheduler.
        )
    except Exception as e:
        logger.error(f"Error running Cognition role: {e}")
    finally:
        internal_write_task.cancel()
        await setup["container"].aclose()
        if setup.get("bot") and not setup["bot"].is_closed():
            await setup["bot"].close()


async def run_monolith_role():
    """Full monolith behavior (current production path)."""
    # Monolith always requests the full set — zero behavior change.
    setup = await _common_setup("monolith")
    token = setup["token"]
    container = setup["container"]
    server = setup["server"]
    bot = setup["bot"]
    if not token:
        return
    logger.info("Launching full monolith (Discord + API + cognition loops)...")
    try:
        await asyncio.gather(
            bot.start(token),
            server.serve(),
            nightly_digest_loop(config),
            frigate_listener_loop(bot),
        )
    except Exception as e:
        logger.error(f"Error running system: {e}")
    finally:
        await container.aclose()
        if not bot.is_closed():
            await bot.close()

def parse_args():
    """Parse command-line arguments for Wave 2b container role selection."""
    parser = argparse.ArgumentParser(
        prog="bernie",
        description="Bernie family assistant (Phase 28 Wave 2b — container split)"
    )
    parser.add_argument(
        "--role",
        choices=["discord", "api", "cognition", "monolith"],
        default=os.environ.get("ROLE", "monolith"),
        help="Execution role for the Wave 2b container split. "
             "Default 'monolith' preserves current single-container behavior "
             "(critical for shadow testing and rollback). "
             "Can also be set via the ROLE environment variable for cleaner compose files."
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    logger.info(f"Starting Bernie with role={args.role}")
    try:
        asyncio.run(main(args.role))
    except KeyboardInterrupt:
        logger.info("System shutdown requested by user.")
