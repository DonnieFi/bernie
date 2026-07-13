"""
Nightly eval worker and summary builder (Phase 4.3 Session 3 carve).

nightly_eval_worker, build_nightly_summary, _log_triplet_scores_to_langfuse,
_render_triplet_score_table (and its internal helpers).

Exact copy of logic. Calls judges, send_hitl_dms, audit funcs, DEFAULT_JUDGE_MODEL
via lazy facade (from eval_service) so that tests patching eval_service.* continue
to work, and to avoid import cycles with the thin facade.

Uses get_database() / injected db_module. Imports shared _LF_* from eval._http.

Do not duplicate judges. Leave audit/weekly code in facade (their helpers stay there too).
"""
import asyncio
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp

from http_session import get_http_session

from db_binding import get_database
from eval._http import _LF_HOST, _LF_PUBLIC, _LF_SECRET

log = logging.getLogger(__name__)


def _eval_service():
    """Lazy facade lookup so tests can patch eval_service.* (judges, send_hitl, audit_*, DEFAULT etc)
    and we avoid load-time cycles with the thin facade reexports.
    """
    import eval_service
    return eval_service


# ── Nightly Summary ──────────────────────────────────────────────────────────

async def _log_triplet_scores_to_langfuse(row: dict, scores: dict, judge_model: str) -> None:
    """Write three-way judge scores to Langfuse as score objects. Non-fatal.

    Uses prompt_hash as trace_id — stopgap until the real chat-turn trace_id is
    stored on shadow_calls rows.
    TODO: wire the real trace_id once shadow_calls stores it.
    """
    try:
        if not _LF_PUBLIC or not _LF_SECRET or not _LF_HOST:
            return
        trace_id = row.get("prompt_hash")
        if not trace_id:
            return
        import base64 as _b64
        creds = _b64.b64encode(f"{_LF_PUBLIC}:{_LF_SECRET}".encode()).decode()
        headers = {
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/json",
        }
        reasoning = scores.get("reasoning", "")
        score_batch = []
        for label, key in [("primary", "A"), ("model_shadow", "B"), ("harness_shadow", "C")]:
            s = scores.get(key, {})
            if isinstance(s, dict):
                score_batch.append({
                    "id": f"{trace_id}-{label}",
                    "type": "score-create",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "body": {
                        "traceId": trace_id,
                        "name": f"{label}_preference",
                        "value": s.get("preference", 0),
                        "comment": reasoning,
                        "dataType": "NUMERIC",
                    },
                })
        if not score_batch:
            return
        sess = get_http_session()
        async with sess.post(
                f"{_LF_HOST}/api/public/ingestion",
                headers=headers,
                json={"batch": score_batch},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300:
                    log.debug("_log_triplet_scores_to_langfuse HTTP %d", resp.status)
    except Exception:
        pass  # non-fatal


def build_nightly_summary(
    date_str: str,
    scored: list[dict],
    total: int,
    triplet_counts: dict | None = None,
    triplet_coverage: tuple[int, int] | None = None,
    primary_model: str | None = None,
    triplet_scores: dict[str, list[tuple[int, int, int]]] | None = None,
    triplet_models: dict[str, str] | None = None,
    triplet_empty: dict[str, int] | None = None,
) -> str:
    """Format a Discord message summarising the eval run, with per-model cost/capability table.

    `triplet_scores`, when present, holds per-leg score lists from the 3-way
    judge — `{"primary": [(intent, tool, pref), …], "model_shadow": [...],
    "harness_shadow": [...]}`. Used to render the per-leg averages table the
    nightly digest historically lacked; the 3-way scores live in
    `shadow_judgments.scores` but were never aggregated into the digest, so the
    operator couldn't see whether harness_shadow (smol) was tracking primary.

    `triplet_models` is an optional `{leg: model_name}` lookup so labels can
    surface which model each leg ran on — harness leg is pinned to the same
    model as model_shadow since c568757.
    """
    triplet_counts = triplet_counts or {}
    triplet_scores = triplet_scores or {}
    triplet_models = triplet_models or {}
    triplet_empty = triplet_empty or {}
    total_t = sum(triplet_counts.values())
    harness_ran, attempted = triplet_coverage if triplet_coverage else (0, 0)

    if not scored and total_t == 0 and attempted == 0:
        return (
            f"**Shadow Eval — {date_str}**\n"
            f"No scoreable shadow calls ({total} total)."
        )

    def avg(key: str, rows: list[dict]) -> float:
        vals = [r[key] for r in rows if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else 0.0

    lines = [f"**Shadow Eval — {date_str}** — {len(scored)}/{total} scored"]

    if scored:
        by_model: dict[str, list[dict]] = defaultdict(list)
        for s in scored:
            by_model[s.get("shadow_model", "unknown")].append(s)

        lines.append("```")
        lines.append(f"{'model':<22} {'n':>3} {'p_int':>6} {'p_fct':>6} {'s_int':>6} {'s_fct':>6} {'wins':>5} {'$/call':>8}")
        lines.append("-" * 68)

        p_intent = avg("primary_intent", scored)
        p_factual = avg("primary_tool", scored)
        primary_label = (primary_model or "[primary]")[-22:]
        lines.append(f"{primary_label:<22} {len(scored):>3} {p_intent:>6.2f} {p_factual:>6.2f} {'—':>6} {'—':>6} {'—':>5} {'(judge)':>8}")

        for model, rows in sorted(by_model.items()):
            s_int = avg("shadow_intent", rows)
            s_fct = avg("shadow_tool", rows)
            wins = sum(1 for r in rows if (r["shadow_intent"] + r["shadow_tool"]) > (r["primary_intent"] + r["primary_tool"]))
            costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
            cost_str = f"${sum(costs)/len(costs):.5f}" if costs else "local"
            short = model[-22:] if len(model) > 22 else model
            lines.append(f"{short:<22} {len(rows):>3} {'—':>6} {'—':>6} {s_int:>6.2f} {s_fct:>6.2f} {wins:>3}/{len(rows):<2} {cost_str:>8}")

        lines.append("```")
        lines.append("_p_int=primary intent  p_fct=primary factual  s_int=shadow intent  s_fct=shadow factual_")

    if attempted:
        cov_pct = harness_ran * 100 // attempted
        lines.append("")
        lines.append(
            f"_Triplet judge — harness coverage {harness_ran}/{attempted} ({cov_pct}%)_"
        )
        if total_t:
            # Per-leg averages first (the gap the operator flagged 2026-05-20 —
            # win counts alone don't tell you if smol is tracking primary).
            score_lines = _render_triplet_score_table(
                triplet_scores, triplet_models,
                triplet_empty=triplet_empty, triplet_attempted=attempted,
            )
            if score_lines:
                lines.extend(score_lines)

            lines.append("```")
            lines.append(f"{'winner':<16} {'n':>3}  share")
            for label in ("primary", "model_shadow", "harness_shadow", "none"):
                n = triplet_counts.get(label, 0)
                pct = (n * 100 // total_t) if total_t else 0
                lines.append(f"{label:<16} {n:>3}  ({pct}%)")
            lines.append("```")
        else:
            lines.append("_No triplets scored — harness leg missing on all candidates._")

    return "\n".join(lines)


def _render_triplet_score_table(
    triplet_scores: dict[str, list[tuple[int, int, int]]],
    triplet_models: dict[str, str],
    triplet_empty: dict[str, int] | None = None,
    triplet_attempted: int = 0,
) -> list[str]:
    """Format the per-leg average score table for the nightly digest.

    Returns an empty list if no leg has any scores so the caller can skip the
    table cleanly. Each row shows the model the leg ran on so the operator can
    spot when the harness pin (c568757) is in effect — both shadow legs will
    share the same model, primary will differ.

    `triplet_empty` is an optional `{leg: empty_count}` lookup; when present, an
    `empty` column is appended showing `n/attempted` per leg. Surfaces the gap
    flagged 2026-05-21: model_shadow returns empty ~17% of the time (LiteLLM
    timeouts + cheaper OR models punting on tool-requiring prompts), but the
    digest had no place to see it.
    """
    if not triplet_scores or not any(triplet_scores.values()):
        return []

    triplet_empty = triplet_empty or {}
    show_empty = bool(triplet_empty) and triplet_attempted > 0

    def avg(samples: list[tuple[int, int, int]], idx: int) -> float:
        vals = [s[idx] for s in samples if s[idx] is not None]
        return sum(vals) / len(vals) if vals else 0.0

    def _fit_model(name: str, width: int = 30) -> str:
        """Truncate from the right (keep the prefix) so 'or-deepseek-v4+claude…'
        stays readable. Mixed-model runs (operator swapped mid-window) are the
        only place this kicks in; single-model runs fit comfortably."""
        if len(name) <= width:
            return name
        return name[: width - 1] + "…"

    header = f"{'leg':<16} {'model':<30} {'n':>3} {'intent':>6} {'tool':>5} {'pref':>5}"
    if show_empty:
        header += f" {'empty':>7}"
    rows: list[str] = ["```", header]
    for leg in ("primary", "model_shadow", "harness_shadow"):
        samples = triplet_scores.get(leg) or []
        model = _fit_model(triplet_models.get(leg) or "?")
        if samples:
            line = (
                f"{leg:<16} {model:<30} {len(samples):>3} "
                f"{avg(samples,0):>6.2f} {avg(samples,1):>5.2f} {avg(samples,2):>5.2f}"
            )
        else:
            line = f"{leg:<16} {model:<30} {0:>3} {'—':>6} {'—':>5} {'—':>5}"
        if show_empty:
            empty_n = triplet_empty.get(leg, 0)
            line += f" {f'{empty_n}/{triplet_attempted}':>7}"
        rows.append(line)
    rows.append("```")
    rows.append("_intent=intent_match  tool=tool_accuracy  pref=preference (0–10 each)_")
    if show_empty:
        # Leg-agnostic phrasing: model_shadow empties are usually LiteLLM
        # timeouts; harness_shadow empties are smol returning "" (tool loop
        # gave up or model produced no final text); primary empties are rare
        # but possible on Anthropic API errors.
        rows.append("_empty=responses with no content (timeout, no final text, or executor error) / attempted_")
    return rows


# ── Nightly Worker ────────────────────────────────────────────────────────────

async def nightly_eval_worker(
    config: dict,
    notification_router=None,
    orchestrator=None,
    bot_instance=None,
) -> None:
    """Run at ~2:30am: score yesterday's shadow calls, post digest to #anvil."""
    db_module = get_database()

    notification_orchestrator = orchestrator or notification_router

    from eval.policy import resolve_eval_policy
    policy = resolve_eval_policy(config)

    if not policy.nightly_enabled:
        log.debug("nightly_eval_worker: nightly eval disabled by policy, skipping")
        return

    # Use TASK_TZ so the date boundary aligns with the 2:30am schedule,
    # not with a potentially different UTC midnight.
    try:
        from config import TASK_TZ
        now_local = datetime.now(TASK_TZ)
    except Exception:
        now_local = datetime.now(timezone.utc)
    today_str = now_local.strftime("%Y-%m-%d")
    max_score = policy.max_scored_per_night

    unscored = await db_module.get_unscored_shadow_calls(today_str)
    if not policy.score_pairs:
        unscored = []
    log.info("nightly_eval_worker: %d unscored calls before %s", len(unscored), today_str)

    scored_results: list[dict] = []
    eval_model = policy.eval_model
    svc = _eval_service()
    if eval_model:
        log.info("nightly_eval_worker: using configured eval_model=%s", eval_model)
    else:
        log.info("nightly_eval_worker: eval_model not set, using default judge %s", svc.DEFAULT_JUDGE_MODEL)

    # family-bot-ah5.2: concurrent judge pool (default 3); no fixed 0.5s sleep.
    try:
        judge_concurrency = int((config.get("eval") or {}).get("judge_concurrency") or 3)
    except (TypeError, ValueError):
        judge_concurrency = 3
    judge_concurrency = max(1, min(judge_concurrency, 8))
    judge_sem = asyncio.Semaphore(judge_concurrency)
    log.info("nightly_eval_worker: judge_concurrency=%d", judge_concurrency)

    async def _score_pair(row: dict) -> dict | None:
        # Hold semaphore across judge + DB write so concurrency caps LiteLLM
        # and SQLite write storms together (agent2 review).
        async with judge_sem:
            scores = await svc.judge_pair(
                row["primary_response"], row["shadow_response"],
                eval_model=eval_model,
                user_message=row["user_message"],
            )
            if not scores:
                return None
            await db_module.update_shadow_scores(
                row["id"],
                scores["primary_intent"],
                scores["primary_tool"],
                scores["shadow_intent"],
                scores["shadow_tool"],
            )
            return {**scores, "shadow_model": row["shadow_model"], "cost_usd": row["cost_usd"]}

    pair_rows = unscored[:max_score]
    if pair_rows:
        pair_out = await asyncio.gather(
            *(_score_pair(r) for r in pair_rows),
            return_exceptions=True,
        )
        for item in pair_out:
            if isinstance(item, Exception):
                log.exception("judge_pair failed: %s", item)
            elif item:
                scored_results.append(item)

    # Score triplets — gate on harness leg actually being present so the judge
    # doesn't rate an empty string and pollute averages. Rows missing the
    # harness leg get a "no_harness" sentinel so they aren't re-fetched.
    triplets = await db_module.get_unscored_triplets(today_str)
    if not policy.score_triplets:
        triplets = []
    attempted = len(triplets[:max_score])
    log.info("nightly_eval_worker: %d unscored triplet candidates before %s", attempted, today_str)
    triplet_counts: dict[str, int] = {"primary": 0, "model_shadow": 0, "harness_shadow": 0, "none": 0}
    triplet_scores: dict[str, list[tuple[int, int, int]]] = {
        "primary": [], "model_shadow": [], "harness_shadow": [],
    }
    # Empty-response counters per leg across the full attempted set. Counted
    # before the no_harness gate so harness_shadow's empty count is visible too
    # (not just elided as no_harness). Surfaces the gap noted 2026-05-21:
    # model_shadow returns empty ~17% of the time historically and we had no
    # visibility into it.
    triplet_empty: dict[str, int] = {"primary": 0, "model_shadow": 0, "harness_shadow": 0}
    _empty_fields = (
        ("primary", "primary_response"),
        ("model_shadow", "shadow_response"),
        ("harness_shadow", "harness_shadow_response"),
    )
    # Per-leg model labels: collect Counters so the digest can show
    # "or-grok" if all triplets used it, "or-grok+or-qwen-36" if the operator
    # swapped mid-window. Keeps the per-leg averages table honest about
    # what was actually being measured.
    from collections import Counter
    triplet_model_counts: dict[str, Counter] = {
        "primary": Counter(), "model_shadow": Counter(), "harness_shadow": Counter(),
    }
    no_harness = 0
    _agg_lock = asyncio.Lock()

    async def _score_triplet(row: dict) -> None:
        nonlocal no_harness
        for leg, field in _empty_fields:
            if not (row.get(field) or "").strip():
                async with _agg_lock:
                    triplet_empty[leg] += 1
        if not (row.get("harness_shadow_response") or "").strip():
            async with _agg_lock:
                no_harness += 1
            try:
                # no_harness is a cheap DB write — still share the pool cap
                async with judge_sem:
                    await db_module.store_shadow_judgment(
                        request_id=row["id"],
                        judge_kind="llm",
                        winner="no_harness",
                        scores={"reason": "harness leg missing"},
                        judge_model=eval_model or svc.DEFAULT_JUDGE_MODEL,
                    )
            except Exception:
                log.exception("store_shadow_judgment (no_harness) failed for row %s", row.get("id"))
            return
        try:
            async with judge_sem:
                scores = await svc.judge_triplet(row, eval_model or svc.DEFAULT_JUDGE_MODEL)
                if not scores:
                    return
                winner_map = {"A": "primary", "B": "model_shadow", "C": "harness_shadow", "none": "none"}
                winner = winner_map.get(scores.get("winner"), None)
                async with _agg_lock:
                    if winner in triplet_counts:
                        triplet_counts[winner] += 1
                    # Accumulate per-leg scores so build_nightly_summary can show
                    # smol-on-shadow vs native averages, not just win counts.
                    leg_to_letter = {"primary": "A", "model_shadow": "B", "harness_shadow": "C"}
                    for leg, letter in leg_to_letter.items():
                        leg_scores = scores.get(letter) or {}
                        if not isinstance(leg_scores, dict):
                            continue
                        i = leg_scores.get("intent_match")
                        t = leg_scores.get("tool_accuracy")
                        p = leg_scores.get("preference")
                        if i is not None and t is not None and p is not None:
                            triplet_scores[leg].append((int(i), int(t), int(p)))
                    # Capture the model each leg ran on. Harness is pinned to
                    # shadow_model since c568757 so it gets the same label as
                    # model_shadow.
                    pm = row.get("primary_model") or "?"
                    sm = row.get("shadow_model") or "?"
                    triplet_model_counts["primary"][pm] += 1
                    triplet_model_counts["model_shadow"][sm] += 1
                    triplet_model_counts["harness_shadow"][sm] += 1
                await db_module.store_shadow_judgment(
                    request_id=row["id"],
                    judge_kind="llm",
                    winner=winner,
                    scores=scores,
                    judge_model=eval_model or svc.DEFAULT_JUDGE_MODEL,
                )
                await _log_triplet_scores_to_langfuse(row, scores, eval_model or svc.DEFAULT_JUDGE_MODEL)
        except Exception:
            log.exception("judge_triplet failed for row %s", row.get("id"))

    trip_rows = triplets[:max_score]
    if trip_rows:
        trip_out = await asyncio.gather(
            *(_score_triplet(r) for r in trip_rows),
            return_exceptions=True,
        )
        for item in trip_out:
            if isinstance(item, Exception):
                log.exception("triplet task failed: %s", item)

    harness_ran = attempted - no_harness
    if attempted:
        cov_pct = harness_ran * 100 // attempted
        log.info("nightly_eval_worker: harness coverage %d/%d (%d%%)", harness_ran, attempted, cov_pct)

    # Flatten the per-leg model counters into "model_a" or "model_a+model_b"
    # labels for the digest. Single model → bare name, multi-model → joined.
    def _label(counter):
        if not counter:
            return ""
        names = [m for m, _ in counter.most_common()]
        return "+".join(names) if len(names) > 1 else names[0]
    triplet_models = {leg: _label(c) for leg, c in triplet_model_counts.items()}

    summary = build_nightly_summary(
        today_str,
        scored_results,
        len(unscored),
        triplet_counts=triplet_counts,
        triplet_coverage=(harness_ran, attempted),
        primary_model=config.get("model"),
        triplet_scores=triplet_scores,
        triplet_models=triplet_models,
        triplet_empty=triplet_empty,
    )

    if policy.ungrounded_audit:
        try:
            ungrounded = await svc.audit_ungrounded_live_data(db_module, since_hours=24)
            audit_section = svc.format_ungrounded_audit_section(ungrounded)
            if audit_section:
                summary = summary + audit_section
                log.info("nightly_eval_worker: %d ungrounded live-data flags", len(ungrounded))
        except Exception:
            log.exception("nightly_eval_worker: ungrounded audit failed (non-fatal)")

    log.info("nightly_eval_worker: %s", summary.replace("\n", " | "))

    # HITL DMs before the #anvil digest — a slow Discord post must not block surveys.
    if bot_instance is not None and policy.hitl:
        try:
            await svc.send_hitl_dms(config, bot_instance, db_module=db_module, orchestrator=notification_orchestrator)
        except Exception:
            log.exception("nightly_eval_worker: HITL sampling failed")

    anvil_id = config.get("anvil_channel_id")
    if anvil_id:
        posted = False
        role = os.environ.get("ROLE", "monolith")
        if role in ("cognition",):
            # Cognition has no live Discord client — post via bernie-discord internal API.
            try:
                from cross_container import post_to_discord
                await post_to_discord(int(anvil_id), content=summary)
                posted = True
            except Exception:
                log.exception("nightly_eval_worker: cognition cross_container post failed")
        elif notification_orchestrator:
            try:
                results = await notification_orchestrator.notify(
                    notification_orchestrator.notification(recipient_id=str(anvil_id), message=summary, urgency="high")
                )
                posted = bool(results.get("discord"))
            except Exception:
                pass
        if not posted:
            # Fallback for cognition container (no native bot connected)
            try:
                from cross_container import post_to_discord
                await post_to_discord(int(anvil_id), content=summary)
            except Exception:
                log.exception("nightly_eval_worker: failed to post to #anvil (fallback also failed)")
