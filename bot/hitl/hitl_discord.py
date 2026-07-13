import json
import logging
import os
from datetime import datetime, timezone

import discord

from executor import ToolContext
from hitl.hitl_service import deny_pending, resume_pending
import db_writes

log = logging.getLogger(__name__)

# Module-level variable for inline notifier callback
_inline_notifier = None
_anvil_audit_bot = None
_production_services = None


def set_inline_notifier(notifier):
    """Set the inline notifier callback (normally bot.py registers a lambda pointing to send_hitl_approval_dms)."""
    global _inline_notifier
    _inline_notifier = notifier


def get_inline_notifier():
    """Retrieve the registered inline notifier."""
    return _inline_notifier


def set_anvil_audit_bot(bot) -> None:
    """Register the discord.py client for tier-2 #anvil audit posts (Wave D)."""
    global _anvil_audit_bot
    _anvil_audit_bot = bot


def get_anvil_audit_bot():
    return _anvil_audit_bot


def _production_service_refs():
    from llm.services import build_service_refs
    from llm.runtime import get_container

    return build_service_refs(get_container())


def init_production_refs() -> None:
    """Warm gateway + service refs once (call from ``on_ready``)."""
    global _production_services
    from tool_gateway import get_tool_gateway

    get_tool_gateway()
    if _production_services is None:
        _production_services = _production_service_refs()


def _get_production_gateway():
    from tool_gateway import get_tool_gateway

    return get_tool_gateway()


def _get_production_services():
    if _production_services is None:
        init_production_refs()
    return _production_services


def _truncate_audit_text(text: str, max_len: int = 1900) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 20] + "\n[...truncated]"


async def post_tier2_anvil_audit(
    *,
    tool_name: str,
    args: dict,
    ctx: ToolContext,
    elapsed_ms: int | None = None,
) -> None:
    """Post a short audit line to #anvil after a successful tier-2 tool dispatch."""
    role = os.environ.get("ROLE")
    if role and role not in ("discord", "monolith"):
        return

    bot = _anvil_audit_bot
    if bot is None:
        log.debug("post_tier2_anvil_audit: discord bot not registered")
        return

    anvil_id = (ctx.config or {}).get("anvil_channel_id")
    if not anvil_id:
        return

    try:
        args_json = json.dumps(args, default=str)
        if len(args_json) > 500:
            args_json = args_json[:500] + "…"
    except (TypeError, ValueError):
        args_json = str(args)[:500]

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    elapsed = f" · {elapsed_ms}ms" if elapsed_ms is not None else ""
    msg = (
        f"📋 **Tier-2 write** `{tool_name}`{elapsed}\n"
        f"Actor: `{ctx.person_id or 'unknown'}` · channel: `{ctx.channel_id or '—'}` · "
        f"executor: `{ctx.executor}`\n"
        f"Args: `{args_json}`\n"
        f"_{ts}_"
    )
    msg = _truncate_audit_text(msg)

    try:
        channel = bot.get_channel(int(anvil_id))
        if channel is None:
            channel = await bot.fetch_channel(int(anvil_id))
        await channel.send(msg)
        log.info("post_tier2_anvil_audit: posted tier-2 audit for %s", tool_name)
    except Exception:
        log.exception("post_tier2_anvil_audit: failed to post to #anvil for %s", tool_name)


def _embed_json_field(args_json: str, *, max_len: int = 1010) -> str:
    """Format args for an embed field, staying under Discord's 1024-char limit."""
    raw = args_json or ""
    if len(raw) > 800:
        raw = raw[:800] + "\n[...truncated]"
    block = f"```json\n{raw}\n```"
    if len(block) <= max_len:
        return block
    overhead = len("```json\n\n```")
    budget = max(0, max_len - overhead - 20)
    trimmed = raw[:budget] + "\n[...truncated]"
    return f"```json\n{trimmed}\n```"


def resolve_admin_discord_ids(config: dict) -> list[int]:
    """Resolve admin Discord IDs — override list, family_members admins, then legacy singular."""
    override = config.get("admin_discord_ids")
    if override:
        try:
            ids = [int(uid) for uid in override if uid]
            if ids:
                return list(dict.fromkeys(ids))
        except (ValueError, TypeError):
            pass

    admin_ids: list[int] = []
    for member in config.get("family_members", {}).values():
        if member.get("role") == "admin":
            d_id = member.get("discord_id")
            if d_id:
                try:
                    admin_ids.append(int(d_id))
                except (ValueError, TypeError):
                    pass
    if admin_ids:
        return list(dict.fromkeys(admin_ids))

    legacy = config.get("admin_discord_id")
    if legacy:
        try:
            return [int(legacy)]
        except (ValueError, TypeError):
            pass
    return []


def build_hitl_embed(row: dict) -> discord.Embed:
    """Build a detailed and polished embed for the tool approval card."""
    ctx_blob = {}
    try:
        ctx_blob = json.loads(row["ctx_json"])
    except (json.JSONDecodeError, TypeError):
        pass

    actor = ctx_blob.get("person_id") or "unknown"
    tool_name = row["tool_name"]
    expires_at = row["expires_at"]

    args_json = row["args_json"]
    args_field = _embed_json_field(args_json)

    embed = discord.Embed(
        title="🛡️ Administrative Approval Required",
        description=f"Action requested by **{actor}** requires admin review.",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Tool Name", value=f"`{tool_name}`", inline=True)
    embed.add_field(name="Expires At (UTC)", value=expires_at, inline=True)
    embed.add_field(name="Arguments", value=args_field, inline=False)
    if row.get("reasoning"):
        embed.add_field(name="Reasoning", value=row["reasoning"], inline=False)

    embed.set_footer(text=f"Request #{row['id']} · Pending Approval")
    return embed


async def sync_sibling_dm_cards(
    bot,
    pending_id: int,
    *,
    suffix: str,
    exclude_message_id: int | None = None,
) -> None:
    """Disable approval buttons on sibling admin DMs after one admin decides."""
    from db_binding import get_database

    db = get_database()

    row = await db.get_pending_hitl(pending_id)
    if not row:
        return

    mapping = db.parse_pending_hitl_notify_map(row.get("notify_message_ids"))
    gateway = _get_production_gateway()
    services = _get_production_services()

    for admin_id, msg_id in mapping.items():
        if exclude_message_id and msg_id == exclude_message_id:
            continue
        try:
            user = bot.get_user(admin_id) or await bot.fetch_user(admin_id)
            dm = user.dm_channel or await user.create_dm()
            msg = await dm.fetch_message(msg_id)
            view = HitlApprovalView(pending_id=pending_id, gateway=gateway, services=services)
            for item in view.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            view.stop()
            if msg.embeds:
                embed = msg.embeds[0]
                embed.set_footer(text=f"Request #{pending_id} · {suffix}")
                await msg.edit(embed=embed, view=view)
        except Exception:
            log.debug(
                "sync_sibling_dm_cards: failed admin=%s msg=%s",
                admin_id,
                msg_id,
                exc_info=True,
            )


class HitlApprovalView(discord.ui.View):
    """Persistent view with Approve and Deny buttons for HITL approval DMs."""

    def __init__(self, pending_id: int, gateway=None, services=None):
        # Persistent views must have timeout=None
        super().__init__(timeout=None)
        self.pending_id = pending_id
        self.gateway = gateway
        self.services = services

        # Approve button (success style)
        approve_btn = discord.ui.Button(
            label="Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"hitl:{pending_id}:approve",
        )
        approve_btn.callback = self.approve_callback
        self.add_item(approve_btn)

        # Deny button (danger style)
        deny_btn = discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            custom_id=f"hitl:{pending_id}:deny",
        )
        deny_btn.callback = self.deny_callback
        self.add_item(deny_btn)

    def _resolve_deps(self):
        if self.gateway is None:
            self.gateway = _get_production_gateway()
        if self.services is None:
            self.services = _get_production_services()

    async def _check_admin(self, interaction: discord.Interaction) -> bool:
        denied_msg = "You must be an admin to approve or deny this request."
        from config import load_config

        admin_ids = resolve_admin_discord_ids(load_config())
        if not admin_ids or interaction.user.id not in admin_ids:
            await interaction.response.send_message(denied_msg, ephemeral=True)
            return False
        return True

    async def _disable(self, interaction: discord.Interaction, suffix_text: str):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

        # Stop view listeners
        self.stop()

        message = interaction.message
        if message:
            embeds = message.embeds
            if embeds:
                embed = embeds[0]
                embed.set_footer(text=f"Request #{self.pending_id} · {suffix_text}")
                await message.edit(embed=embed, view=self)
            else:
                await message.edit(content=f"{message.content}\n{suffix_text}", view=self)

    async def approve_callback(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction):
            return

        # Defer immediately to avoid timeout
        await interaction.response.defer()

        self._resolve_deps()

        # Get decided_by identifier
        decided_by = interaction.user.name
        try:
            from constants import registry as person_registry
            decided_by = person_registry.resolve(interaction.user.id) or decided_by
        except ImportError:
            pass

        # Call resume_pending (which performs atomic resolve and runs the tool once)
        result = await resume_pending(
            self.pending_id,
            self.gateway,
            services=self.services,
            decided_by=decided_by,
        )

        result_lower = (result or "").lower()
        if "expired" in result_lower or "already decided" in result_lower:
            await self._disable(interaction, "Expired or already decided")
            await interaction.followup.send("Request expired or already decided.", ephemeral=True)
            return

        # Success - disable buttons and show approval state
        await self._disable(interaction, f"Approved by {interaction.user.display_name}")
        await interaction.followup.send(f"Request approved. Result:\n{result}", ephemeral=True)

        if interaction.client:
            await sync_sibling_dm_cards(
                interaction.client,
                self.pending_id,
                suffix=f"Approved by {interaction.user.display_name}",
                exclude_message_id=interaction.message.id if interaction.message else None,
            )

    async def deny_callback(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction):
            return

        # Defer immediately
        await interaction.response.defer()

        self._resolve_deps()

        decided_by = interaction.user.name
        try:
            from constants import registry as person_registry
            decided_by = person_registry.resolve(interaction.user.id) or decided_by
        except ImportError:
            pass

        denied = await deny_pending(
            self.pending_id,
            services=self.services,
            decided_by=decided_by,
        )
        if not denied:
            await self._disable(interaction, "Already decided")
            await interaction.followup.send("Request expired or already decided.", ephemeral=True)
            return

        # Disable buttons and show denied state
        await self._disable(interaction, f"Denied by {interaction.user.display_name}")
        await interaction.followup.send("Request denied.", ephemeral=True)

        if interaction.client:
            await sync_sibling_dm_cards(
                interaction.client,
                self.pending_id,
                suffix=f"Denied by {interaction.user.display_name}",
                exclude_message_id=interaction.message.id if interaction.message else None,
            )


async def send_hitl_approval_dms(pending_id: int, bot) -> list[tuple[int, int]]:
    """DM all admins about a pending HITL request."""
    import os

    if os.environ.get("BERNIE_DISABLE_HITL_DM") == "1":
        log.debug("send_hitl_approval_dms suppressed (BERNIE_DISABLE_HITL_DM)")
        return []

    from db_binding import get_database
    from config import load_config

    db = get_database()
    row = await db.get_pending_hitl(pending_id)
    if not row or row["status"] != "pending":
        return []

    config = load_config()
    admin_ids = resolve_admin_discord_ids(config)
    if not admin_ids:
        log.warning("No admin Discord IDs found to notify for pending HITL request #%d", pending_id)
        return []

    gateway = _get_production_gateway()
    services = _get_production_services()

    embed = build_hitl_embed(row)

    sent: list[tuple[int, int]] = []
    for admin_id in admin_ids:
        try:
            user = bot.get_user(admin_id)
            if user is None:
                user = await bot.fetch_user(admin_id)
            channel = user.dm_channel or await user.create_dm()
            # Fresh view per DM — discord.py binds one view instance per message.
            view = HitlApprovalView(pending_id=pending_id, gateway=gateway, services=services)
            msg = await channel.send(embed=embed, view=view)
            sent.append((admin_id, msg.id))
        except Exception as e:
            log.error("Failed to DM admin %d for HITL request #%d: %s", admin_id, pending_id, e)

    if sent:
        try:
            await db_writes.routed_best_effort(
                "set_pending_hitl_notify_message_ids", pending_id, sent,
            )
        except Exception as e:
            log.warning(
                "Failed to persist HITL notify map for #%d (DMs sent): %s",
                pending_id, e,
            )

    return sent


async def register_pending_hitl_views(bot) -> None:
    """Load all pending HITL requests and register their views with the bot on_ready."""
    from db_binding import get_database
    from db_client import wait_for_cognition_writer, writes_locally

    cog_ready = writes_locally() or await wait_for_cognition_writer(timeout_s=90)
    if not cog_ready:
        log.warning(
            "register_pending_hitl_views: cognition writer not ready; "
            "views only — skipping orphan re-DM",
        )

    db = get_database()
    try:
        pending_rows = await db.list_pending_hitl(status="pending")
        if not pending_rows:
            return

        init_production_refs()
        gateway = _get_production_gateway()
        services = _get_production_services()

        for row in pending_rows:
            pending_id = row["id"]
            view = HitlApprovalView(pending_id=pending_id, gateway=gateway, services=services)
            bot.add_view(view)

            notify_map = db.parse_pending_hitl_notify_map(row.get("notify_message_ids"))
            if not notify_map:
                if cog_ready:
                    log.info(
                        "hitl_orphan_notify: re-DMing admins for pending #%d (no notify map)",
                        pending_id,
                    )
                    try:
                        await send_hitl_approval_dms(pending_id, bot)
                    except Exception:
                        log.exception("hitl_orphan_notify failed for pending #%d", pending_id)
                else:
                    log.warning(
                        "hitl_orphan_notify: skipping re-DM for #%d (cognition writer not ready)",
                        pending_id,
                    )

        log.info("hitl_view_reregistered: re-registered %d pending HITL view(s)", len(pending_rows))
    except Exception as e:
        log.error("Failed to re-register pending HITL views: %s", e, exc_info=True)
