"""Shared LLM helpers for cognitive task handlers (Anthropic + Ollama)."""

from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

from http_session import get_http_session

from telemetry import fire_and_forget

log = logging.getLogger("bernie.worker")

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
_LLM_SEMAPHORE = asyncio.Semaphore(2)

_ollama_semaphore: asyncio.Semaphore | None = None


def _get_ollama_semaphore() -> asyncio.Semaphore:
    global _ollama_semaphore
    if _ollama_semaphore is None:
        _ollama_semaphore = asyncio.Semaphore(1)
    return _ollama_semaphore


def _get_ollama_sem_for_tests() -> asyncio.Semaphore:
    return _get_ollama_semaphore()


def _reset_for_tests() -> None:
    global _ollama_semaphore
    _ollama_semaphore = None


class _OllamaSemaphoreProxy:
    @property
    def _value(self):
        return _get_ollama_semaphore()._value

    async def acquire(self):
        return await _get_ollama_semaphore().acquire()

    def release(self):
        return _get_ollama_semaphore().release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *exc):
        self.release()


OLLAMA_SEMAPHORE = _OllamaSemaphoreProxy()

SMALL_MODEL_DISCIPLINE = (
    "Small-model rules:\n"
    "- Respond with the requested output shape only — JSON, plain text, or markdown as instructed.\n"
    "- Do not preface with 'Sure!' or 'Certainly,' — start with the answer.\n"
    "- Do not explain your reasoning unless explicitly asked.\n"
    "- No markdown fences around JSON output.\n"
    "- If you cannot answer with the available context, say so in one sentence; do not invent facts.\n"
)

_WORKER_SYSTEM = (
    "You are Bernie, a helpful home assistant bot for the Example family. "
    "You were asked to research something in the background. "
    "Give a clear, concise answer. If you need real-time data you don't have, "
    "say so in one sentence."
)


async def call_worker_model(topic: str) -> str | None:
    from config import config
    from model_registry import DEFAULT_MODEL

    worker_model = config.get("eval", {}).get("worker_model") or DEFAULT_MODEL
    ollama_models = config.get("ollama_models", [])

    if worker_model in ollama_models:
        log.info("worker: routing to Ollama default model=%s", worker_model)
        result, _ = await call_ollama_topic(worker_model, topic, config)
        if result:
            return result
        log.warning(
            "worker: Ollama default %s failed — falling back to Anthropic %s",
            worker_model,
            DEFAULT_MODEL,
        )
        return await call_anthropic_topic(DEFAULT_MODEL, topic)

    if worker_model.startswith("claude-"):
        log.info("worker: routing to Anthropic default model=%s", worker_model)
        result = await call_anthropic_topic(worker_model, topic)
        if result:
            return result
        log.warning("worker: Anthropic %s failed — falling back to Ollama", worker_model)
        return await call_ollama_fallback(topic, config)

    log.warning("worker: unknown model type %s — falling back to Ollama", worker_model)
    return await call_ollama_fallback(topic, config)


async def call_ollama_fallback(topic: str, config: dict) -> str | None:
    ollama_models = config.get("ollama_models", [])
    if not ollama_models:
        log.error("worker: no Ollama models configured — cannot fall back")
        return None
    fallback = ollama_models[0]
    log.info("worker: Ollama fallback model=%s", fallback)
    text, _ = await call_ollama_topic(fallback, topic, config)
    return text


async def call_anthropic_topic(model: str, topic: str) -> str | None:
    if not ANTHROPIC_KEY:
        log.warning("ANTHROPIC_API_KEY not set — cannot call Anthropic")
        return None
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 1024,
        "system": _WORKER_SYSTEM,
        "messages": [{"role": "user", "content": topic}],
    }
    async with _LLM_SEMAPHORE:
        try:
            session = get_http_session()
            async with session.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        log.warning("Anthropic returned HTTP %d for worker task", resp.status)
                        return None
                    body = await resp.json()
                    content = body.get("content") or []
                    text = content[0].get("text", "").strip() if content else None
                    usage = body.get("usage", {}) or {}
                    try:
                        from langfuse_logger import log_generation

                        fire_and_forget(
                            log_generation(
                                model=model,
                                user_input=topic,
                                output=text or "",
                                input_tokens=usage.get("input_tokens", 0) or 0,
                                output_tokens=usage.get("output_tokens", 0) or 0,
                                name="worker_topic",
                                triggered_by="cognitive_worker",
                            )
                        )
                    except Exception:
                        log.debug("langfuse worker_topic trace failed (non-fatal)", exc_info=True)
                    return text
        except Exception:
            log.exception("call_anthropic_topic failed")
            return None


async def call_ollama_topic(
    model: str,
    topic: str,
    config: dict,
    num_ctx: int | None = None,
    system: str | None = None,
    timeout_s: int = 300,
) -> tuple[str | None, dict]:
    from ollama_resolver import resolve_ollama_base_url

    effective_system = SMALL_MODEL_DISCIPLINE + "\n" + (system or _WORKER_SYSTEM)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": effective_system},
            {"role": "user", "content": topic},
        ],
        "stream": False,
    }
    if num_ctx is not None:
        payload["options"] = {"num_ctx": num_ctx}

    async with OLLAMA_SEMAPHORE:
        for attempt, force_probe in enumerate((False, True)):
            base_url = await resolve_ollama_base_url(config, force=force_probe)
            url = f"{base_url.rstrip('/')}/api/chat"
            try:
                session = get_http_session()
                async with session.post(
                        url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=timeout_s),
                    ) as resp:
                        if resp.status != 200:
                            log.warning(
                                "Ollama returned HTTP %d for worker task (host=%s)",
                                resp.status, base_url,
                            )
                            if attempt == 0:
                                log.info("worker: re-probing Ollama candidates after HTTP %d", resp.status)
                                continue
                            return None, {}
                        data = await resp.json()
                        text = (data.get("message", {}).get("content") or "").strip() or None
                        if not text and attempt == 0:
                            log.info("worker: empty Ollama response on %s — re-probing candidates", base_url)
                            continue
                        stats = {
                            "model": data.get("model", model),
                            "tokens_in": data.get("prompt_eval_count"),
                            "tokens_out": data.get("eval_count"),
                            "duration_ms": int(data.get("total_duration", 0) / 1_000_000) or None,
                            "gpu_ms": int(data.get("eval_duration", 0) / 1_000_000) or None,
                        }
                        try:
                            from langfuse_logger import log_generation

                            fire_and_forget(
                                log_generation(
                                    model=model,
                                    user_input=topic,
                                    output=text or "",
                                    input_tokens=stats.get("tokens_in") or 0,
                                    output_tokens=stats.get("tokens_out") or 0,
                                    name="worker_topic",
                                    triggered_by="cognitive_worker",
                                )
                            )
                        except Exception:
                            log.debug("langfuse worker_topic trace failed (non-fatal)", exc_info=True)
                        return text, stats
            except Exception:
                if attempt == 0:
                    log.info("worker: Ollama connection failed on %s — re-probing candidates", base_url)
                    continue
                log.exception("call_ollama_topic failed for model=%s", model)
                return None, {}
    return None, {}
