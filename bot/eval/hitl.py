"""
HITL DM sampling and reaction handling (Phase 4.3 Session 3 carve).

send_hitl_dms, handle_hitl_reaction + _send_single_hitl_dm helper.

Exact copy. Uses get_database() / injected db_module. No raw database import.

eval_service.py remains thin facade re-exporting so bot.py reaction wiring,
nightly_eval_worker calls, and tests continue unchanged.

Do not move audit/weekly/shadow/judges.
"""
import logging
import random
from datetime import datetime, timedelta, timezone

from db_binding import get_database

log = logging.getLogger(__name__)


_ANVIL_SAMPLE_RATE = 0.50
_OTHER_SAMPLE_RATE = 0.25


async def send_hitl_dms(
    config: dict,
    bot,
    db_module=None,
    orchestrator=None,
) -> None:
    """Send HITL DMs for divergent triplets from yesterday's nightly eval."""
    from eval.policy import resolve_eval_policy
    if not resolve_eval_policy(config).hitl:
        return

    db_module = db_module or get_database()

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    candidates = await db_module.get_divergent_unsampled_triplets(yesterday)
    if not candidates:
        log.info("send_hitl_dms: no divergent unsampled triplets")
        return

    admin_discord_id = str(config.get("admin_discord_id", "") or "")
    if not admin_discord_id:
        log.warning("send_hitl_dms: no admin_discord_id in config, skipping HITL")
        return

    anvil_id = str(config.get("anvil_channel_id", "") or "")
    anvil_rows = [r for r in candidates if str(r.get("channel_id", "")) == anvil_id]
    other_rows = [r for r in candidates if str(r.get("channel_id", "")) != anvil_id]

    to_sample = [r for r in anvil_rows if random.random() < _ANVIL_SAMPLE_RATE]
    by_person: dict[str, list[dict]] = {}
    for row in other_rows:
        by_person.setdefault(str(row.get("actor_id") or "unknown"), []).append(row)
    for person_rows in by_person.values():
        if random.random() < _OTHER_SAMPLE_RATE:
            to_sample.append(random.choice(person_rows))

    log.info(
        "send_hitl_dms: %d divergent candidate(s) for %s — sampling %d (#anvil=%d, other=%d)",
        len(candidates), yesterday, len(to_sample), len(anvil_rows), len(other_rows),
    )
    if not to_sample:
        log.info(
            "send_hitl_dms: random sample skipped all %d candidate(s) "
            "(#anvil rate=%.0f%%, other rate=%.0f%%)",
            len(candidates), _ANVIL_SAMPLE_RATE * 100, _OTHER_SAMPLE_RATE * 100,
        )
        return

    sent = 0
    for row in to_sample:
        if await _send_single_hitl_dm(row, admin_discord_id, bot, db_module, orchestrator=orchestrator):
            sent += 1
    log.info("send_hitl_dms: sent %d/%d HITL survey DM(s)", sent, len(to_sample))


async def _send_single_hitl_dm(row: dict, admin_discord_id: str, bot, db_module, orchestrator=None) -> bool:
    """DM admin a single shuffled A/B/C triplet for blind preference vote."""
    primary = row.get("primary_response", "(empty)")
    model_shadow = row.get("shadow_response", "(empty)")
    harness_shadow = row.get("harness_shadow_response", "(empty)")

    order = [0, 1, 2]
    random.shuffle(order)
    responses = [primary, model_shadow, harness_shadow]

    tool_calls_by_idx: dict[int, str] = {}
    try:
        for idx, surface in [(0, "primary"), (2, "harness")]:
            rows = await db_module.get_tool_calls_for_prompt_hash(row.get("prompt_hash", ""), surface)
            if rows:
                tool_calls_by_idx[idx] = ", ".join(rows[:5])
    except Exception:
        pass

    def _fmt_response(label: str, resp: str, orig_idx: int) -> str:
        tool_note = tool_calls_by_idx.get(orig_idx)
        tool_line = f"\n_Tools: {tool_note}_" if tool_note else ""
        return f"**Response {label}:**\n{resp[:380]}{'...' if len(resp) > 380 else ''}{tool_line}"

    body = "\n\n".join(
        _fmt_response(chr(ord("A") + i), responses[order[i]], order[i])
        for i in range(3)
    )
    msg_text = (
        f"**Shadow Eval — Blind Preference Vote**\n"
        f"Original: _{row.get('user_message', '')[:120]}_\n\n"
        f"{body}\n\n"
        f"React: 1️⃣ 2️⃣ 3️⃣ (best response), ❌ (all bad), ⏭️ (skip)\n"
        f"_row_id:{row['id']}_"
    )

    reactions = ["1️⃣", "2️⃣", "3️⃣", "❌", "⏭️"]
    try:
        from cross_container import discord_client_ready, post_to_discord

        dm_message_id: int
        if discord_client_ready(bot):
            from discord_chunk import send_chunked

            admin_user = await bot.fetch_user(int(admin_discord_id))
            dm_channel = admin_user.dm_channel
            if dm_channel is None:
                dm_channel = await admin_user.create_dm()
            dm = await send_chunked(dm_channel, msg_text, is_dm=True)
            for emoji in reactions:
                await dm.add_reaction(emoji)
            dm_message_id = dm.id
        else:
            posted = await post_to_discord(
                int(admin_discord_id),
                content=msg_text,
                reactions=reactions,
            )
            dm_message_id = posted.id
        await db_module.store_shadow_judgment(
            request_id=row["id"],
            judge_kind="hitl_pending",
            winner=None,
            scores={"shuffle_order": order, "dm_message_id": dm_message_id},
            actor_id=admin_discord_id,
        )
        return True
    except Exception:
        log.exception("_send_single_hitl_dm failed for row %s", row.get("id"))
        return False


async def handle_hitl_reaction(
    message_id: int,
    emoji: str,
    actor_id: str,
    db_module=None,
    config: dict | None = None,
) -> None:
    """Record an admin reaction to a HITL DM as a shadow_judgment."""
    from eval.policy import resolve_eval_policy

    cfg = config if config is not None else {}
    if not resolve_eval_policy(cfg).hitl:
        log.debug("handle_hitl_reaction: nightly.hitl disabled, ignoring reaction")
        return

    db_module = db_module or get_database()

    import json as _json

    pending = await db_module.get_hitl_pending_by_message(message_id)
    if not pending:
        return

    try:
        scores = pending.get("scores") or {}
        if isinstance(scores, str):
            scores = _json.loads(scores or "{}")
    except Exception:
        scores = {}

    order = scores.get("shuffle_order", [0, 1, 2])
    label_map = {str(i + 1): order[i] for i in range(3)}
    idx_to_name = {0: "primary", 1: "model_shadow", 2: "harness_shadow"}
    emoji_to_choice = {"1️⃣": "1", "2️⃣": "2", "3️⃣": "3", "❌": "none", "⏭️": "skip"}
    choice = emoji_to_choice.get(emoji)
    if not choice:
        return

    if choice in ("none", "skip"):
        winner = choice
    else:
        orig_idx = label_map.get(choice, 0)
        winner = idx_to_name.get(orig_idx, "unknown")

    await db_module.store_shadow_judgment(
        request_id=pending["request_id"],
        judge_kind="hitl",
        winner=winner,
        scores={"emoji": emoji, "shuffle_order": order},
        actor_id=actor_id,
    )
    log.info("HITL vote recorded: row=%s winner=%s actor=%s", pending["request_id"], winner, actor_id)
