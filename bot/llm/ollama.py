"""Ollama fallback and direct interaction logic (Phase 4.4 Session 3).

Moved from claude_service.py.
"""
import asyncio
import logging
import aiohttp

from http_session import get_http_session
from .observability import log_llm_turn

log = logging.getLogger(__name__)


def resolve_ollama_target(
    config: dict,
    resolved_base_url: str,
    model_override: str | None,
) -> tuple[str, str | None]:
    """Pick the (base_url, model) for an Ollama call.

    The resolver has already probed the candidate hosts and returned a live one
    (`resolved_base_url`). That probed host always wins.
    """
    if model_override:
        return resolved_base_url, model_override
    fallback_cfg = config.get("llm_fallback")
    if not fallback_cfg:
        return resolved_base_url, None
    return resolved_base_url, fallback_cfg.get("model")


async def call_ollama(
    system: str,
    messages: list[dict],
    config: dict,
    session,
    cal_service=None,
    model_override: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
    user_message: str = "",
    channel_id: str = "",
    is_dm: bool = False,
    mode: str = "",
) -> str:
    """Call Ollama directly as a fallback when primary LLMs are down, or as a direct provider."""
    from ollama_resolver import resolve_ollama_base_url
    resolved_base_url = await resolve_ollama_base_url(config, session=session)

    base_url, model = resolve_ollama_target(config, resolved_base_url, model_override)
    if model is None:
        return "Primary LLM failed and no fallback is configured."

    url = f"{base_url.rstrip('/')}/api/chat"

    # Inject live context into the system prompt for Ollama since it can't use tools
    live_ctx = ""
    try:
        from llm.context_builder import build_context
        ctx_data = await build_context(
            config, cal_service, session,
            user_message=user_message or "",
            channel_id=channel_id or "",
            is_dm=is_dm or False,
            mode=mode or "",
        )
        
        context_parts = []
        if ctx_data.get("weather"):
            context_parts.append(f"Current Weather: {ctx_data['weather']}")
        if ctx_data.get("today_events"):
            context_parts.append(f"Today's Schedule:\n{ctx_data['today_events']}")
        if ctx_data.get("presence"):
            home = [n.capitalize() for n, p in ctx_data["presence"].items() if p.get("is_home")]
            if home:
                context_parts.append(f"Currently Home: {', '.join(home)}")
        
        if context_parts:
            live_ctx = "\n\nLIVE CONTEXT (since you cannot use tools right now):\n" + "\n".join(context_parts)
    except Exception as e:
        log.warning(f"Ollama context injection failed: {e}")

    def format_content(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    elif item.get("type") == "tool_use":
                        parts.append(f"[Tool Call: {item.get('name')} {item.get('input')}]")
                    elif item.get("type") == "tool_result":
                        parts.append(f"[Tool Result: {item.get('content')}]")
            return "\n".join(parts)
        return str(content)

    from worker import SMALL_MODEL_DISCIPLINE
    full_system = (
        SMALL_MODEL_DISCIPLINE
        + "\n"
        + format_content(system)
        + live_ctx
        + "\n\nNOTE: You currently do NOT have access to live tools. Use the LIVE CONTEXT provided above to answer questions. If you need info not in the context, say so in one sentence — do not invent."
    )
    ollama_messages = [{"role": "system", "content": full_system}]
    for m in messages:
        ollama_messages.append({
            "role": m["role"],
            "content": format_content(m["content"])
        })

    payload = {
        "model": model,
        "messages": ollama_messages,
        "stream": False
    }

    try:
        log.info(f"Ollama call: {model} at {url}")

        async def _do_call(s: aiohttp.ClientSession):
            async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error(f"Ollama error: {resp.status} - {body}")
                    return "Sorry, I'm having trouble reaching both my primary and backup brains."
                data = await resp.json()
                content = data.get("message", {}).get("content", "")

                # Pull last user message for Langfuse trace input
                _user_in = ""
                for _m in reversed(messages):
                    if _m.get("role") == "user":
                        _c = _m.get("content", "")
                        if isinstance(_c, str):
                            _user_in = _c
                        break

                await log_llm_turn(
                    model=model,
                    user_input=_user_in,
                    output=content,
                    input_tokens=data.get("prompt_eval_count", 0),
                    output_tokens=data.get("eval_count", 0),
                    triggered_by="system", # fallback runs as system or background
                    session_id=session_id,
                    conversation_id=conversation_id,
                    name="ollama_chat",
                    cost_usd=0.0,
                )

                return content or "I'm offline right now (fallback returned empty)."

        async def _run():
            if session and not session.closed:
                return await _do_call(session)
            return await _do_call(get_http_session())

        from config import load_config
        from llm.queue import queued_run

        app_config = load_config()
        return await queued_run(_run(), app_config, shadow=False)
    except asyncio.TimeoutError:
        log.error("Ollama call timed out (llm queue step timeout)")
        return "Everything is offline. I'm sorry, I can't help right now."
    except RuntimeError as exc:
        if str(exc) == "shed":
            log.warning("Ollama call shed by llm queue")
            return "Sorry, I'm busy right now — try again in a moment."
        raise
    except Exception as e:
        log.error(f"Ollama call failed: {e}")
        return "Everything is offline. I'm sorry, I can't help right now."
