"""ResearchWorker — bounded iterative SearXNG + Jina synthesis.

Hard caps:
- max_iterations (default 3) rounds of query + fetch + summarise
- max_urls_per_iteration (default 5) URLs per round
- max_runtime_s (default 300) total deadline; partial result returned if reached
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

from http_session import get_http_session

from cognitive_workers import CognitiveWorkerBase
from cognitive_workers import research_io

log = logging.getLogger("bernie.research")


QUERY_SYSTEM = (
    "You are a research planner. Decide 1-3 search queries that would best advance "
    "the answer to the user's topic. Output STRICT JSON object with a single key "
    '"queries" mapping to an array of strings, e.g. '
    '{"queries": ["query one", "query two"]}. No commentary, no markdown fences.'
)


SUMMARY_SYSTEM = (
    "Summarise the fetched pages with attention to facts directly relevant to the "
    "user's topic. Be concrete: cite numbers, dates, names. Avoid speculation. "
    "Plain text, <=300 words."
)

SYNTH_SYSTEM = (
    "Synthesise the final answer to the user's research question. Output well-"
    "structured markdown with headings, lists, and source URLs in parentheses. "
    "Be direct and useful — do not hedge unnecessarily. If findings are "
    "contradictory, surface the contradiction. <=1500 words."
)


class ResearchWorker(CognitiveWorkerBase):
    name = "research"

    def __init__(self, config: dict):
        cfg = config.get("cognitive_workers", {}).get("research", {})
        self.default_model = cfg.get("default_model", "qwen2.5:14b")
        self.upgrade_model = cfg.get("upgrade_model")
        self.escalate_above_tokens = cfg.get("escalate_above_tokens", 6000)
        self.num_ctx = cfg.get("num_ctx", 16384)
        self.max_runtime_s = cfg.get("max_runtime_s", 300)
        self.max_iterations = cfg.get("max_iterations", 3)
        self.max_urls = cfg.get("max_urls_per_iteration", 5)
        self.max_queries_per_iteration = int(cfg.get("max_queries_per_iteration", 5) or 5)
        self.searxng_url = config.get("searxng_url", "http://192.168.1.X:8081")
        self._config = config

    async def handle(self, task: dict, container) -> dict:
        db = container.db
        from worker import _call_ollama_topic
        from typed_outputs import ResearchQueries

        payload = task.get("payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        topic = (payload.get("topic") or "").strip()
        depth_req = int(payload.get("depth", 2))
        depth = max(1, min(depth_req, self.max_iterations))
        requester_id = payload.get("requester_id") or task.get("actor_id") or ""
        unified_task_id = payload.get("unified_task_id")

        if unified_task_id:
            try:
                prior = await db.list_research_memory(int(unified_task_id))
                memory_block = db.format_research_memory_for_prompt(prior)
                if memory_block:
                    topic = f"{topic}\n\n{memory_block}"
            except Exception:
                log.debug("research: failed to load thread memory for #%s", unified_task_id, exc_info=True)

        if not topic:
            raise ValueError("research task missing topic")

        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.max_runtime_s

        findings: list[str] = []
        sources: list[str] = []
        aggregate_stats = {
            "model": self.default_model,
            "tokens_in": 0, "tokens_out": 0,
            "duration_ms": 0, "gpu_ms": 0,
        }

        def _merge(s: dict):
            aggregate_stats["tokens_in"] += s.get("tokens_in") or 0
            aggregate_stats["tokens_out"] += s.get("tokens_out") or 0
            aggregate_stats["duration_ms"] += s.get("duration_ms") or 0
            aggregate_stats["gpu_ms"] += s.get("gpu_ms") or 0
            if s.get("model"):
                aggregate_stats["model"] = s["model"]

        def _remaining_timeout(default: int = 60) -> int:
            r = int(deadline - loop.time())
            return max(10, min(default, r))

        session = get_http_session()
        for iteration in range(depth):
            if loop.time() > deadline:
                log.warning("research: deadline reached at iteration %d", iteration)
                break
            prior = "\n\n".join(findings)
            query_prompt = (
                f"Topic: {topic}\n\nPrior findings:\n{prior or '(none yet)'}\n\n"
                'Produce the queries JSON now (object form: {"queries": [...]}).'
            )
            queries_model, q_stats = await self.call_and_parse(
                self._config, query_prompt, ResearchQueries,
                system=QUERY_SYSTEM,
                initial_model=self.default_model,
                num_ctx=self.num_ctx,
                timeout_s=_remaining_timeout(60),
                # Empty query response = "model thinks we've covered
                # enough" — legitimate iteration-ending signal, not a
                # transport failure. Don't fail the whole task.
                raise_on_empty=False,
            )
            _merge(q_stats)
            queries = queries_model.queries if queries_model else []
            if not queries:
                log.info("research: model produced no queries — stopping")
                break
            # Cap fan-out before parallel SearXNG (agent2 review: verbose models)
            max_queries = max(1, min(int(self.max_queries_per_iteration or 5), 10))
            if len(queries) > max_queries:
                log.info(
                    "research: truncating %d queries to %d",
                    len(queries), max_queries,
                )
                queries = list(queries)[:max_queries]

            # family-bot-ah5.5: parallel SearXNG queries (fetch_many already concurrent)
            search_hits = await asyncio.gather(
                *(
                    research_io.searxng_search(
                        session, self.searxng_url, q, limit=self.max_urls
                    )
                    for q in queries
                ),
                return_exceptions=True,
            )
            urls: list[str] = []
            for hit in search_hits:
                if isinstance(hit, Exception):
                    log.warning("research: searxng_search failed: %s", hit)
                    continue
                urls.extend(hit or [])
            # Dedup preserving order; cap to max_urls
            seen = set()
            deduped = []
            for u in urls:
                if u in seen:
                    continue
                seen.add(u)
                deduped.append(u)
            deduped = deduped[: self.max_urls]
            if not deduped:
                log.info("research: no new URLs at iteration %d — stopping", iteration)
                break
            sources.extend(deduped)

            pages = await research_io.fetch_many(
                session, deduped, concurrency=3, timeout_s=20, max_chars_per_doc=6000
            )
            if not pages:
                log.info("research: all fetches failed at iter %d — stopping", iteration)
                break

            docs_text = "\n\n".join(f"Source: {u}\n{t}" for u, t in pages)
            sum_prompt = f"Topic: {topic}\n\nPages:\n{docs_text}\n\nProduce the summary now."
            text, stats = await _call_ollama_topic(
                self.default_model, sum_prompt, self._config,
                num_ctx=self.num_ctx, system=SUMMARY_SYSTEM,
                timeout_s=_remaining_timeout(180),
            )
            _merge(stats)
            if text:
                findings.append(text.strip())

        synth_findings = "\n\n".join(findings)
        if len(synth_findings) > 24_000:
            synth_findings = synth_findings[-24_000:]

        synth_prompt = (
            f"Topic: {topic}\n\nAccumulated findings:\n"
            + synth_findings
            + "\n\nSources: " + json.dumps(sources)
            + "\n\nProduce the final markdown answer now."
        )
        text, stats = await _call_ollama_topic(
            self.default_model, synth_prompt, self._config,
            num_ctx=self.num_ctx, system=SYNTH_SYSTEM,
            timeout_s=_remaining_timeout(180),
        )
        _merge(stats)
        final = text or ""

        if not final:
            final = (
                f"Research on **{topic}** ran but the local model didn't produce a synthesis "
                f"within the runtime budget. {len(sources)} source(s) were collected."
            )

        await db.store_task_output(
            task_id=task.get("id"),
            key=f"research:{task.get('id')}",
            content=final,
        )
        if unified_task_id:
            try:
                await db.append_research_memory(int(unified_task_id), "finding", final[:4000])
            except Exception:
                log.debug("research: append_research_memory failed for #%s", unified_task_id, exc_info=True)
        # Propagate the requester's chosen delivery method + email through to
        # the deliver task. The reaction handler may have flipped these on the
        # parent task between enqueue and now — re-read from the parent's
        # payload (this `payload` var is the original snapshot from claim).
        try:
            parent = await db.get_cognitive_task(task.get("id"))
            current = parent.get("payload") if parent else {}
        except Exception:
            current = {}
        delivery = (current.get("delivery") or payload.get("delivery") or "dm").lower()
        # Board-routed tasks: the bridge write-back is the delivery — skip DM/email.
        if delivery != "board" and not payload.get("unified_task_id"):
            if delivery not in ("dm", "email"):
                delivery = "dm"
            email = current.get("email") or payload.get("email") or ""
            await db.create_cognitive_task(
                type="research_deliver",
                payload={
                    "task_id": task.get("id"),
                    "requester_id": str(requester_id),
                    "topic": topic,
                    "delivery": delivery,
                    "email": email,
                },
                actor_id=task.get("actor_id"),
                channel_id=task.get("channel_id"),
                priority=8,
            )

        return {
            "_result": {"sources": len(sources), "iterations_completed": len(findings)},
            "_stats": aggregate_stats,
        }
