"""
Nightly digest — runs at 2am Halifax time.
Reads yesterday's conversation_history, calls Claude once per person,
extracts insights, and stores them in family_insights.
"""

import asyncio
import logging
import os
import pathlib

from telemetry import fire_and_forget
from datetime import datetime, timezone, timedelta

from zoneinfo import ZoneInfo

from config import config
from db_binding import get_database
from llm.clients import make_observed_anthropic_client
from insight_extraction import (
    DIGEST_SYSTEM,
    build_digest_user_prompt,
    parse_insights_from_response,
)

log = logging.getLogger(__name__)

# Channel ID → label mapping (built from config at call time)
def _channel_label_map(config: dict) -> dict:
    return {
        str(config.get("schedule_channel_id", "")): "#smithy",
        str(config.get("furnace_channel_id", "")): "#furnace",
        str(config.get("slag_channel_id", "")): "#slag",
        str(config.get("anvil_channel_id", "")): "#anvil",
    }


async def _get_yesterday_messages(tz, now_local: datetime) -> list[dict]:
    """
    Return all conversation_history rows from Halifax yesterday.
    Uses UTC range query to avoid midnight boundary drift — messages stored in UTC.
    """
    yesterday_start = (now_local - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_end = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_start = yesterday_start.astimezone(timezone.utc).isoformat()
    utc_end = yesterday_end.astimezone(timezone.utc).isoformat()

    return await get_database().conversation_history_in_range(utc_start, utc_end)


def _format_messages_for_prompt(messages: list[dict], channel_labels: dict) -> str:
    """Format conversation messages into a readable string for Claude."""
    lines = []
    for m in messages:
        label = channel_labels.get(m["channel_id"], f"channel-{m['channel_id']}")
        lines.append(f"[{label}] {m['role']}: {m['content']}")
    return "\n".join(lines)


async def _generate_insights_for_person(
    person_name: str,
    person_id: str,
    yesterday_messages: str,
    api_key: str,
    model: str,
    client=None,
) -> list[dict]:
    """Call Claude to extract insights about person_name from yesterday_messages.

    Pass a shared *client* (family-bot-ah5.1) to avoid open/close per person.
    """
    if not yesterday_messages.strip():
        log.info(f"nightly_digest: no messages for {person_name}, skipping")
        return []

    prompt = build_digest_user_prompt(person_name, yesterday_messages)

    owns_client = client is None
    if owns_client:
        client = make_observed_anthropic_client(api_key)
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=512,
            system=DIGEST_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )

        # Log token usage for background task
        try:
            await get_database().log_token_usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                model=model,
                triggered_by="system"
            )
        except Exception as log_err:
            log.error(f"nightly_digest: failed to log token usage: {log_err}")

        text = " ".join(b.text for b in response.content if hasattr(b, "text"))
        try:
            from langfuse_logger import log_generation
            fire_and_forget(log_generation(
                model=model,
                user_input=prompt,
                output=text,
                input_tokens=getattr(response.usage, "input_tokens", 0) or 0,
                output_tokens=getattr(response.usage, "output_tokens", 0) or 0,
                name="nightly_digest",
                triggered_by="scheduler",
                metadata={"person": person_name},
            ))
        except Exception:
            log.debug("langfuse nightly_digest trace failed (non-fatal)", exc_info=True)
        return parse_insights_from_response(text)
    except Exception as e:
        log.error(f"nightly_digest: Claude call failed for {person_name}: {e}")
        return []
    finally:
        if owns_client and client is not None:
            await client.close()


async def _ollama_direct_for_digest(system: str, messages: list[dict], config: dict, model: str) -> str:
    """Route a digest fallback through ``worker._call_ollama_topic``.

    Bypasses ``claude_service._call_ollama`` because that helper prepends
    today's weather/presence/schedule as "LIVE CONTEXT" — wrong for a
    historical-analysis prompt that's analyzing yesterday. Cognitive
    workers already use ``_call_ollama_topic`` for the same reason; this
    function is the digest's matching path."""
    from worker import _call_ollama_topic
    # Flatten the chat-shape messages into a single user prompt; the worker
    # helper takes one topic string and a system prompt separately.
    user_text = "\n\n".join(
        m.get("content", "") for m in messages if m.get("role") != "system"
    )
    text, _ = await _call_ollama_topic(
        model, user_text, config,
        system=system, timeout_s=60,
    )
    return text or ""


async def _call_fallback_model(system: str, messages: list[dict], config: dict, fallback_model: str) -> str:
    """Rule of 3: Anthropic (caller's tier 1, not here) → LiteLLM → Ollama.

    - If `fallback_model` is in `ollama_models`, skip LiteLLM and go direct.
    - Otherwise try LiteLLM with the configured master key; on non-200 OR
      transport error, cascade to direct Ollama using `llm_fallback.model`.
    """
    if fallback_model in config.get("ollama_models", []):
        return await _ollama_direct_for_digest(system, messages, config, fallback_model)

    base_url = config.get("litellm_base_url", "https://litellm.example.local")
    import aiohttp
    from http_session import get_http_session
    from eval_service import _ssl_for  # share SSL policy with the rest of the codebase
    api_key = os.environ.get("LTE_LLM_MASTER_KEY", "")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }
    payload = {
        "model": fallback_model,
        "messages": [{"role": "system", "content": system}] + messages,
        "max_tokens": 512,
    }
    try:
        sess = get_http_session()
        async with sess.post(
                f"{base_url.rstrip('/')}/v1/chat/completions",
                json=payload,
                headers=headers,
                ssl=_ssl_for(base_url),
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]
                log.error(f"_call_fallback_model: HTTP {resp.status} from {base_url}; cascading to Ollama")
    except Exception as e:
        log.error(f"_call_fallback_model: LiteLLM transport error ({e!r}); cascading to Ollama")

    # Tier 3 — Ollama direct. Uses llm_fallback.model when configured, otherwise
    # the first entry of ollama_models. Returns "" if both unset.
    ollama_model = (
        (config.get("llm_fallback") or {}).get("model")
        or (config.get("ollama_models") or [None])[0]
    )
    if not ollama_model:
        log.error("_call_fallback_model: no Ollama fallback configured; giving up")
        return ""
    log.info(f"_call_fallback_model: tier-3 Ollama fallback → {ollama_model}")
    try:
        return await _ollama_direct_for_digest(system, messages, config, ollama_model)
    except Exception as e:
        log.error(f"_call_fallback_model: Ollama fallback also failed: {e!r}")
        return ""


async def mine_chat_threads(config: dict, api_key: str, model: str, fallback_model: str | None = None):
    """
    Extract insights from unmined chat threads.
    """
    unmined = await get_database().get_unmined_chat_threads()
    if not unmined:
        log.info("nightly_digest: no unmined chat threads")
        return

    for thread in unmined:
        thread_id = thread["id"]
        person_id = thread["person_id"]
        title = thread["title"]
        
        # Map person_id to name for the prompt
        from constants import registry as person_registry
        person_name = person_registry.first_name(person_id) if person_registry.get(person_id) else person_id

        messages = await get_database().get_chat_thread_messages(thread_id)
        if not messages:
            await get_database().mark_chat_threads_mined([thread_id])
            continue

        formatted = "\n".join([f"{m['role']}: {m['content']}" for m in messages])
        
        try:
            # Try primary Claude first
            insights = []
            if api_key:
                try:
                    insights = await _generate_insights_for_person(
                        person_name, person_id, formatted, api_key, model
                    )
                except Exception as e:
                    if "credit balance" in str(e).lower() and fallback_model:
                        log.warning(f"nightly_digest: Claude credits empty during chat mining, falling back for {person_name}")
                    else:
                        raise e

            # Fallback
            if not insights and fallback_model:
                prompt = build_digest_user_prompt(
                    person_name,
                    formatted,
                    source=f"a chat thread titled '{title}'",
                )
                messages_payload = [{"role": "user", "content": prompt}]
                response_text = await _call_fallback_model(DIGEST_SYSTEM, messages_payload, config, fallback_model)
                insights = parse_insights_from_response(response_text)

            if insights:
                # Use today's date for chat mining source
                source_date = datetime.now().date().isoformat()
                await get_database().store_insights(person_id, insights, f"chat:{thread_id}")
                await get_database().log_activity(
                    "digest",
                    f"Mined {len(insights)} insights from chat thread '{title}'",
                    person_id=person_id
                )
                log.info(f"nightly_digest: mined {len(insights)} insights from thread '{title}'")
            
            # Mark as mined even if no insights found, to avoid reprocessing
            await get_database().mark_chat_threads_mined([thread_id])
            
        except Exception as e:
            log.error(f"nightly_digest: failed to mine thread {thread_id}: {e}")

    # Cleanup old mined threads (e.g. older than 7 days)
    deleted_count = await get_database().delete_mined_chat_threads(days_old=7)
    if deleted_count > 0:
        log.info(f"nightly_digest: cleaned up {deleted_count} old chat threads")


async def generate_nightly_digest(config: dict):
    """
    Main digest entry point. Idempotent — checks digest_log before running.
    """
    from constants import registry as person_registry
    from llm.model_state import get_model_info

    tz = ZoneInfo(config.get("timezone", "America/Halifax"))
    now_local = datetime.now(tz)
    yesterday = (now_local - timedelta(days=1)).date()
    yesterday_str = yesterday.isoformat()

    # Deduplication check
    if await get_database().check_digest_exists(yesterday_str):
        log.info(f"nightly_digest: digest for {yesterday_str} already completed, skipping")
        return

    await get_database().start_digest(yesterday_str)
    log.info(f"nightly_digest: starting digest for {yesterday_str}")

    # Use configured digest model, falling back to DEFAULT_MODEL
    from model_registry import DEFAULT_MODEL
    primary_model = config.get("digest_model", DEFAULT_MODEL)
    active_model, active_base_url = get_model_info()
    
    # If active_model is NOT a Claude model, it's likely a local or LiteLLM model
    # we can use as a robust fallback.
    fallback_model = active_model if not active_model.startswith("claude-") else None
    fallback_base_url = active_base_url if fallback_model else None

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key and not fallback_model:
        log.error("nightly_digest: ANTHROPIC_API_KEY not set and no fallback model available, aborting")
        return

    channel_labels = _channel_label_map(config)

    all_messages = await _get_yesterday_messages(tz, now_local)
    if not all_messages:
        log.info(f"nightly_digest: no conversation history for {yesterday_str}")
        await get_database().complete_digest(yesterday_str)
        return

    formatted_all = _format_messages_for_prompt(all_messages, channel_labels)

    # One Claude call per person with a Discord account (for DM delivery)
    active_persons = {
        p["first_name"]: p["id"]
        for p in person_registry.family()
        if p.get("discord_id", "0") != "0"
    }

    # family-bot-ah5.1: shared Anthropic client + bounded concurrent person extracts
    try:
        digest_conc = int((config.get("cognitive_workers") or {}).get("digest_concurrency") or 3)
    except (TypeError, ValueError):
        digest_conc = 3
    digest_conc = max(1, min(digest_conc, 6))
    dig_sem = asyncio.Semaphore(digest_conc)
    had_errors = False
    error_lock = asyncio.Lock()

    shared_client = make_observed_anthropic_client(api_key) if api_key else None
    try:
        async def _one_person(person_name: str, person_id: str) -> None:
            nonlocal had_errors
            async with dig_sem:
                try:
                    insights: list[dict] = []
                    if api_key and shared_client is not None:
                        try:
                            insights = await _generate_insights_for_person(
                                person_name, person_id, formatted_all, api_key, primary_model,
                                client=shared_client,
                            )
                        except Exception as e:
                            if "credit balance" in str(e).lower() and fallback_model:
                                log.warning(
                                    "nightly_digest: Claude credits empty, falling back to %s for %s",
                                    fallback_model, person_name,
                                )
                            else:
                                raise

                    if not insights and fallback_model:
                        log.info(
                            "nightly_digest: using fallback model %s for %s",
                            fallback_model, person_name,
                        )
                        prompt = build_digest_user_prompt(person_name, formatted_all)
                        msgs = [{"role": "user", "content": prompt}]
                        response_text = await _call_fallback_model(
                            DIGEST_SYSTEM, msgs, config, fallback_model
                        )
                        insights = parse_insights_from_response(response_text)

                    if insights:
                        await get_database().store_insights(person_id, insights, yesterday_str)
                        await get_database().log_activity(
                            "digest",
                            f"Stored {len(insights)} insights for {person_name}",
                            person_id=person_id,
                        )
                        log.info(
                            "nightly_digest: stored %d insights for %s",
                            len(insights), person_name,
                        )
                except Exception as e:
                    log.error("nightly_digest: failed for %s: %s", person_name, e)
                    async with error_lock:
                        had_errors = True

        if active_persons:
            await asyncio.gather(
                *(_one_person(n, i) for n, i in active_persons.items())
            )
    finally:
        if shared_client is not None:
            try:
                await shared_client.close()
            except Exception:
                pass

    if not had_errors:
        # Also mine chat threads
        await mine_chat_threads(config, api_key, primary_model, fallback_model)
        
        await get_database().complete_digest(yesterday_str)
        log.info(f"nightly_digest: completed for {yesterday_str}")
    else:
        log.warning(f"nightly_digest: finished with errors for {yesterday_str}, will not mark complete")



async def nightly_digest_loop(config: dict):
    """
    Coroutine that sleeps until 2am Halifax time, runs the digest, then sleeps 24h.
    Designed to be added to asyncio.gather() in main.py.
    """
    tz = ZoneInfo(config.get("timezone", "America/Halifax"))
    while True:
        now = datetime.now(tz)
        # Next 2am
        next_run = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        sleep_seconds = (next_run - now).total_seconds()
        log.info(f"nightly_digest_loop: sleeping {sleep_seconds:.0f}s until {next_run.strftime('%Y-%m-%d %H:%M %Z')}")
        await asyncio.sleep(sleep_seconds)
        try:
            await generate_nightly_digest(config)
        except Exception as e:
            log.error(f"nightly_digest_loop: unhandled error: {e}")
        try:
            removed = await get_database().cleanup_old_drafts(max_age_hours=48)
            if removed:
                log.info(f"nightly_digest_loop: pruned {removed} abandoned draft(s)")
        except Exception as e:
            log.error(f"nightly_digest_loop: draft cleanup error: {e}")
        try:
            counts = await get_database().prune_logs(retention_days=30)
            total = sum(counts.values())
            if total:
                log.info(f"nightly_digest_loop: pruned {total} old log rows — {counts}")
        except Exception as e:
            log.error(f"nightly_digest_loop: log pruning error: {e}")
