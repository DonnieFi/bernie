"""
Caller-facing Notification Module Interface:
- notify(notification: Notification) -> dict: Sends a notification (Discord, email, etc.) based on preferences/urgency.
- notify_all(notification: Notification) -> None: Posts a notification to the main family channel (#smithy).
- ping(recipient_id: str, message: str) -> bool: Directly ping a recipient on Discord and log to activity feed.
- flush_pending(recipient_id: str) -> None: Flushes queued quiet-hours notifications for a recipient.

Wiring / Service Plan:
- ServiceRefs.orchestrator type is now NotificationRouter (previously wrapped by NotificationOrchestrator, which is deleted).
"""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from typing import List, Dict, Any
from types import SimpleNamespace

try:
    import discord
    from discord.utils import MISSING as _DISCORD_MISSING
except ModuleNotFoundError:
    class _DiscordStubError(Exception):
        pass

    class _DiscordStubEmbed:
        @classmethod
        def from_dict(cls, data):
            return cls()

        def to_dict(self):
            return {}

    class _DiscordStubClient:
        pass

    class _DiscordStubMessageable:
        pass

    discord = SimpleNamespace(
        Embed=_DiscordStubEmbed,
        Client=_DiscordStubClient,
        NotFound=_DiscordStubError,
        Forbidden=_DiscordStubError,
        abc=SimpleNamespace(Messageable=_DiscordStubMessageable),
    )
    _DISCORD_MISSING = object()

from db_binding import get_database
from config import config
import db_writes


def _is_quiet_hours(now: datetime) -> bool:
    """True if current Halifax time is within the global quiet window (default 22:00–07:00)."""
    from zoneinfo import ZoneInfo
    tz_name = config.get("timezone", "America/Halifax")
    tz = ZoneInfo(tz_name)
    local_hour = now.astimezone(tz).hour
    quiet = config.get("quiet_hours", {})
    start = quiet.get("start_hour", 22)
    end = quiet.get("end_hour", 7)
    if start > end:  # wraps midnight
        return local_hour >= start or local_hour < end
    return start <= local_hour < end


logger = logging.getLogger(__name__)


def _discord_value(val):
    """Return None for unset discord.py 2.x MISSING sentinels."""
    if val is None or val is _DISCORD_MISSING:
        return None
    return val


def _embed_log_text(embed) -> str:
    if not embed:
        return ""
    for attr in ("description", "title"):
        text = _discord_value(getattr(embed, attr, None))
        if text:
            return text
    return ""


@dataclass
class Notification:
    recipient_id: str
    message: str | None = None
    title: str | None = None
    embed: discord.Embed | None = None
    urgency: str = "normal"       # normal | high | silent
    channels: List[str] | None = None  # set ["email"] to also send via email
    event_id: str | None = None        # opaque ID forwarded to message_event_map
    message_type: str | None = None    # e.g. "proactive_nudge"

async def _has_explicit_prefs_row(person_id: str) -> bool:
    """Return True if person has a row in person_preferences (i.e. they've explicitly set prefs)."""
    return await get_database().person_preferences_row_exists(person_id)


class NotificationRouter:
    def __init__(self, bot: discord.Client):
        self.bot = bot

    def notification(
        self,
        recipient_id: str,
        message: str | None = None,
        title: str | None = None,
        embed: discord.Embed | None = None,
        urgency: str = "normal",
        channels: List[str] | None = None,
        event_id: str | None = None,
        message_type: str | None = None,
    ) -> Notification:
        """Thin factory to construct a Notification dataclass without importing it."""
        return Notification(
            recipient_id=recipient_id,
            message=message,
            title=title,
            embed=embed,
            urgency=urgency,
            channels=channels,
            event_id=event_id,
            message_type=message_type,
        )

    async def notify(self, notification: Notification) -> Dict[str, Any]:
        # 1. Time-Based Delivery Gating
        # Suppression is OVERNIGHT ONLY — never presence-based. Being away should
        # not queue notifications (that caused a spam burst on every return home,
        # and stale data made it fire incorrectly). Normal notifications deliver
        # immediately; only quiet hours hold them, flushed by the morning job.
        # "high"/"silent" always go through; email path is never gated.
        if notification.urgency == "normal" and not (notification.channels and "email" in notification.channels):
            from constants import registry

            # Map snowflake to person_id for the quiet-hours opt-in check.
            person_id = registry.resolve(notification.recipient_id)

            # Quiet hours gate (22:00–07:00) — queue unless person explicitly opted in.
            # A row in person_preferences with reminders_enabled=True means "wake me up anyway".
            if _is_quiet_hours(datetime.now(timezone.utc)):
                prefs = await get_database().get_person_pref(person_id=person_id) if person_id else {}
                # Only bypass quiet hours if person has an explicit row with reminders on
                has_override = person_id and prefs.get("reminders_enabled") and await _has_explicit_prefs_row(person_id)
                if not has_override:
                    embed_json = None
                    if notification.embed is not None:
                        try:
                            embed_json = json.dumps(notification.embed.to_dict())
                        except Exception:
                            embed_json = None
                    await db_writes.routed("add_pending_notification",
                        recipient_id=notification.recipient_id,
                        message=notification.message,
                        title=notification.title,
                        embed_json=embed_json,
                        urgency=notification.urgency,
                        event_id=notification.event_id,
                        message_type=notification.message_type,
                    )
                    logger.info(f"NotificationRouter: Queued during quiet hours for {person_id or notification.recipient_id}")
                    return {"status": "queued_quiet_hours"}

        results = {}

        # Discord — always attempted
        resp = await self._send_discord(notification)
        results["discord"] = resp
        log_msg = notification.message or _embed_log_text(notification.embed) or "(embed)"
        await db_writes.routed("log_notification", 
            recipient_id=notification.recipient_id,
            channel="discord",
            message=log_msg,
            success=bool(resp),
        )

        # Email — only when explicitly requested
        if notification.channels and "email" in notification.channels:
            success = await self._send_email(notification)
            results["email"] = success
            await db_writes.routed("log_notification", 
                recipient_id=notification.recipient_id,
                channel="email",
                message=log_msg,
                success=success,
            )

        # Warn on any channels the router no longer supports
        _supported = {"discord", "email"}
        if notification.channels:
            for ch in notification.channels:
                if ch not in _supported:
                    logger.warning(f"NotificationRouter: unsupported channel '{ch}' requested for {notification.recipient_id} — ignored")

        return results

    async def flush_pending(self, recipient_id: str):
        """Send all queued notifications for a recipient.

        - Stale entries (> notification_queue_max_age_hours, default 48h) are pruned.
        - If > notification_flush_batch_threshold (default 5) fresh messages remain,
          they are batched into a single "While you were out" summary to avoid
          Discord rate-limits after a long absence.
        - Failed deliveries stay in the queue for the next flush attempt.
        """
        pending = await get_database().list_pending_notifications(recipient_id)
        if not pending:
            return

        max_age_hours = config.get("notification_queue_max_age_hours", 48)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        batch_threshold = config.get("notification_flush_batch_threshold", 5)

        logger.info(f"NotificationRouter: Flushing {len(pending)} pending messages for {recipient_id}")
        stale_ids: list[int] = []
        fresh: list[dict] = []

        for p in pending:
            try:
                created_at = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if created_at < cutoff:
                    logger.info(f"NotificationRouter: Pruning stale id={p['id']} for {recipient_id}")
                    stale_ids.append(p["id"])
                    continue
            except Exception:
                pass  # Missing/unparseable timestamp — treat as fresh
            fresh.append(p)

        to_remove: list[int] = list(stale_ids)

        # Proactive nudges must always be sent individually so reactions can be attached.
        # Split them out before the batch-threshold check.
        nudge_fresh = [p for p in fresh if p.get("message_type") == "proactive_nudge"]
        other_fresh = [p for p in fresh if p.get("message_type") != "proactive_nudge"]

        if len(other_fresh) > batch_threshold:
            # Batch into a single "While you were out" embed
            lines = [
                f"**{p.get('title') or 'Message'}**: {(p.get('message') or '')[:120]}"
                for p in other_fresh[:20]
            ]
            overflow = len(other_fresh) - 20
            if overflow > 0:
                lines.append(f"_(and {overflow} more)_")
            embed = discord.Embed(
                title=f"📬 {len(other_fresh)} messages while you were out",
                description="\n".join(lines),
                color=0x5865F2,
            )
            batch_notif = Notification(
                recipient_id=recipient_id,
                message=None,
                embed=embed,
                urgency="high",
            )
            result = await self._send_discord(batch_notif)
            if result:
                to_remove.extend(p["id"] for p in other_fresh)
            else:
                logger.warning(f"NotificationRouter: Batch delivery failed for {recipient_id}, keeping for next flush")
        else:
            for p in other_fresh:
                embed = None
                if p.get("embed_json"):
                    try:
                        embed = discord.Embed.from_dict(json.loads(p["embed_json"]))
                    except Exception:
                        pass
                notif = Notification(
                    recipient_id=recipient_id,
                    message=p.get("message"),
                    title=p.get("title"),
                    embed=embed,
                    urgency="high",
                )
                result = await self._send_discord(notif)
                if result:
                    to_remove.append(p["id"])
                else:
                    logger.warning(f"NotificationRouter: Delivery failed for id={p['id']}, keeping for next flush")

        # Always send nudges individually so ✅/❌ reactions can be attached.
        for p in nudge_fresh:
            embed = None
            if p.get("embed_json"):
                try:
                    embed = discord.Embed.from_dict(json.loads(p["embed_json"]))
                except Exception:
                    pass
            notif = Notification(
                recipient_id=recipient_id,
                message=p.get("message"),
                title=p.get("title"),
                embed=embed,
                urgency="high",
            )
            result = await self._send_discord(notif)
            if result:
                to_remove.append(p["id"])
                p_event_id = p.get("event_id")
                if p_event_id and hasattr(result, "id"):
                    try:
                        await result.add_reaction("✅")
                        await result.add_reaction("❌")
                        await db_writes.routed("store_message_mapping", 
                            message_id=result.id,
                            event_id=p_event_id,
                            message_type="proactive_nudge",
                        )
                    except Exception:
                        logger.debug("NotificationRouter: flush reaction/mapping failed", exc_info=True)
            else:
                logger.warning(f"NotificationRouter: Nudge delivery failed for id={p['id']}, keeping for next flush")

        if to_remove:
            await db_writes.routed("clear_pending_notifications_by_ids", ids=to_remove)


    async def _send_discord(self, notification: Notification) -> Any:
        logger.info(f"_send_discord: notification to {notification.recipient_id}")
        try:
            # Try to find recipient as a user
            try:
                recipient_id_int = int(notification.recipient_id)
            except ValueError:
                logger.error(f"Invalid Discord recipient ID: {notification.recipient_id}")
                return False

            # Guard: if the bot is not connected (e.g. bernie-cognition role), the
            # discord.Client was never logged in and get_user / fetch_user will
            # raise AttributeError('_MissingSentinel' object has no attribute 'is_set').
            # Fall back to bernie-discord /internal/post (channel or user snowflake → DM).
            bot_ready = hasattr(self.bot, "is_ready") and self.bot.is_ready()
            if not bot_ready:
                from cross_container import post_to_discord
                embed_dict = None
                if notification.embed is not None:
                    try:
                        embed_dict = notification.embed.to_dict()
                    except Exception:
                        pass
                try:
                    posted = await post_to_discord(
                        channel_id=recipient_id_int,
                        content=notification.message or None,
                        embed=embed_dict,
                    )
                    logger.info(
                        "_send_discord (cross-container): posted to channel %s → msg_id=%s",
                        recipient_id_int, posted.id,
                    )
                    return posted
                except Exception as cc_err:
                    logger.warning(
                        "_send_discord (cross-container): could not post to %s — %s",
                        recipient_id_int, cc_err,
                    )
                    return False

            logger.info(f"Looking up user {recipient_id_int}")
            user = None
            if hasattr(self.bot, "get_user"):
                user = self.bot.get_user(recipient_id_int)
            
            if not user and hasattr(self.bot, "fetch_user"):
                try:
                    user = await self.bot.fetch_user(recipient_id_int)
                except discord.NotFound:
                    pass
            
            if user:
                try:
                    from discord_chunk import send_chunked

                    logger.info(
                        "Sending DM to %s (%s)",
                        _discord_value(getattr(user, "display_name", None))
                        or _discord_value(getattr(user, "name", None))
                        or user.id,
                        user.id,
                    )
                    channel = user.dm_channel
                    if channel is None:
                        channel = await user.create_dm()
                    res = await send_chunked(
                        channel,
                        notification.message or "",
                        is_dm=True,
                        embed=notification.embed,
                    )
                    logger.info("DM sent successfully to %s", user.id)
                    return res
                except discord.Forbidden:
                    display = (
                        _discord_value(getattr(user, "display_name", None))
                        or _discord_value(getattr(user, "name", None))
                        or str(user.id)
                    )
                    logger.warning("Forbidden: Could not DM %s", display)
                    schedule_channel = None
                    if hasattr(self.bot, "get_channel"):
                        schedule_channel = self.bot.get_channel(config.get("schedule_channel_id", 0))
                    
                    if schedule_channel and isinstance(schedule_channel, discord.abc.Messageable):
                        await schedule_channel.send(f"<@{user.id}>, I couldn't DM you. Check your privacy settings.", embed=notification.embed)
                    return False
            
            # Try as a channel
            channel = None
            if hasattr(self.bot, "get_channel"):
                channel = self.bot.get_channel(recipient_id_int)
            
            if channel and isinstance(channel, discord.abc.Messageable):
                from discord_chunk import send_chunked

                is_dm = isinstance(channel, discord.DMChannel)
                return await send_chunked(
                    channel,
                    notification.message or "",
                    is_dm=is_dm,
                    embed=notification.embed,
                )

            logger.warning(f"Could not find Discord recipient {notification.recipient_id}")
            return False
        except Exception as e:
            logger.error(f"Failed to send Discord notification: {e}")
            return False

    async def _send_email(self, notification: Notification) -> bool:
        from constants import registry
        to_addr = None
        
        person_id = registry.resolve(notification.recipient_id)
        if person_id:
            person = registry.get(person_id)
            if person:
                to_addr = person.get("email")
                
        if not to_addr:
            logger.warning(f"No email address found for recipient {notification.recipient_id!r} in registry — skipping email")
            return False

        token_file = config.get("gmail_token_file", "/credentials/gmail_token.json")
        if not os.path.exists(token_file):
            logger.warning(f"Gmail token not found at {token_file} — skipping email")
            return False

        try:
            from email_service import EmailPolicyError, EmailRateLimitError, send

            subject = notification.title or "Bernie"
            body = notification.message or ""
            await send(
                to_addr,
                subject,
                body,
                requester_id="agent:notification-router",
                requester_role="system",
                config=config,
            )
            logger.info(f"Email sent to {to_addr} (subject: {subject!r})")
            return True
        except (EmailPolicyError, EmailRateLimitError) as e:
            logger.warning(f"Email policy/rate block for {to_addr}: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to send email to {to_addr}: {e}")
            return False

    async def notify_all(self, notification: Notification) -> None:
        """Post notification to the main family channel (#smithy)."""
        channel_id = config.get("schedule_channel_id")
        if not channel_id:
            logger.warning("notify_all: no schedule_channel_id configured")
            return
        await self.notify(Notification(
            recipient_id=str(channel_id),
            message=notification.message,
            title=notification.title,
            embed=notification.embed,
            urgency=notification.urgency,
            channels=notification.channels,
        ))

    async def ping(self, recipient_id: str, message: str) -> bool:
        notification = Notification(recipient_id=recipient_id, message=message, urgency="high")
        results = await self.notify(notification)
        
        # Log to activity feed
        success = any(results.values())
        if success:
            from constants import registry
            person_id = registry.resolve(recipient_id)
            person_name = registry.display_name(person_id) if person_id else f"User …{str(recipient_id)[-4:]}"

            await db_writes.routed("log_activity", "ping", f"Pinged <b>{person_name}</b>", f"Message: {message}", "Discord")

        return success
