"""Active /bus track sessions — ephemeral edits, home announcements."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from config import config

if TYPE_CHECKING:
    import discord

log = logging.getLogger(__name__)


def _tracking_cfg() -> dict:
    return (config.get("transit") or {}).get("tracking") or {}


def poll_seconds() -> int:
    return int(_tracking_cfg().get("poll_seconds", 180))


def max_ticks() -> int:
    return int(_tracking_cfg().get("max_ticks", 10))


def session_ttl_seconds() -> int:
    return int(_tracking_cfg().get("session_ttl_seconds", 1800))


def error_retry_seconds() -> int:
    """Backoff between poll retries after feed/vehicle errors (capped vs poll interval)."""
    configured = int(_tracking_cfg().get("error_retry_seconds", 30))
    return min(configured, poll_seconds())


def _fallback_message_text(content: str | None, embed: Any = None) -> str:
    """Plain text for in-channel fallback when ephemeral embed edits fail."""
    if content:
        return content[:1500]
    if embed is not None:
        parts: list[str] = []
        if getattr(embed, "title", None):
            parts.append(f"**{embed.title}**")
        if getattr(embed, "description", None):
            parts.append(str(embed.description))
        for field in getattr(embed, "fields", []) or []:
            parts.append(f"{field.name}: {field.value}")
        if parts:
            return "\n".join(parts)[:1500]
    return (
        f"Bus **tracking** update — ephemeral message could not be refreshed. "
        "Use `/bus route` or `/bus near` for a fresh position."
    )


@dataclass
class VehicleBinding:
    vehicle_id: str
    route_id: str
    expires_at: float


@dataclass
class TransitSession:
    user_id: int
    channel_id: int
    person_id: str
    person_display: str
    vehicle_id: str
    route_id: str
    landmark_key: str
    landmark_person_id: str = ""
    message_id: int | None = None
    started_at: float = field(default_factory=time.monotonic)
    tick_count: int = 0
    last_distance_m: float | None = None
    task: asyncio.Task | None = None
    waiting_continue: bool = False
    fallback_posted: bool = False
    edit_failures: int = 0


class TransitTrackingManager:
    def __init__(self) -> None:
        self._sessions: dict[int, TransitSession] = {}
        self._bindings: dict[str, VehicleBinding] = {}
        self._bot: Any = None

    def set_bot(self, bot: Any) -> None:
        self._bot = bot

    def set_vehicle_binding(self, person_id: str, vehicle_id: str, route_id: str) -> None:
        from transit_service import normalize_route_id

        self._bindings[person_id.lower()] = VehicleBinding(
            vehicle_id=vehicle_id,
            route_id=normalize_route_id(route_id),
            expires_at=time.monotonic() + 4 * 3600,
        )

    def get_vehicle_binding(self, person_id: str) -> VehicleBinding | None:
        b = self._bindings.get(person_id.lower())
        if not b:
            return None
        if time.monotonic() > b.expires_at:
            del self._bindings[person_id.lower()]
            return None
        return b

    def get_session(self, user_id: int) -> TransitSession | None:
        return self._sessions.get(user_id)

    async def stop_session(self, user_id: int, *, reason: str = "stopped") -> bool:
        sess = self._sessions.pop(user_id, None)
        if not sess:
            return False
        if sess.task and not sess.task.done():
            sess.task.cancel()
            try:
                await sess.task
            except asyncio.CancelledError:
                pass
        log.info("Transit track stopped for user %s: %s", user_id, reason)
        return True

    async def start_session(
        self,
        *,
        user_id: int,
        channel_id: int,
        person_id: str,
        person_display: str,
        vehicle_id: str,
        route_id: str,
        landmark_key: str,
        landmark_person_id: str | None = None,
        interaction: discord.Interaction | None = None,
        initial_text: str | None = None,
        initial_embed: Any = None,
    ) -> TransitSession:
        from transit_service import normalize_route_id as norm_route

        await self.stop_session(user_id, reason="replaced")
        sess = TransitSession(
            user_id=user_id,
            channel_id=channel_id,
            person_id=person_id,
            person_display=person_display,
            vehicle_id=vehicle_id,
            route_id=norm_route(route_id),
            landmark_key=landmark_key,
            landmark_person_id=(landmark_person_id or person_id).lower(),
        )
        if interaction:
            if initial_embed is not None:
                if interaction.response.is_done():
                    msg = await interaction.followup.send(
                        embed=initial_embed, ephemeral=True, wait=True
                    )
                else:
                    await interaction.response.send_message(
                        embed=initial_embed, ephemeral=True
                    )
                    msg = await interaction.original_response()
            elif initial_text:
                if interaction.response.is_done():
                    msg = await interaction.followup.send(
                        initial_text[:2000], ephemeral=True, wait=True
                    )
                else:
                    await interaction.response.send_message(
                        initial_text[:2000], ephemeral=True
                    )
                    msg = await interaction.original_response()
            else:
                raise ValueError("start_session requires initial_text or initial_embed")
            sess.message_id = msg.id
        self._sessions[user_id] = sess
        sess.task = asyncio.create_task(self._poll_loop(sess))
        return sess

    async def extend_session(self, user_id: int) -> bool:
        sess = self._sessions.get(user_id)
        if not sess or not sess.waiting_continue:
            return False
        sess.waiting_continue = False
        sess.tick_count = 0
        sess.started_at = time.monotonic()
        sess.last_distance_m = None
        sess.fallback_posted = False
        sess.edit_failures = 0
        if sess.task and not sess.task.done():
            sess.task.cancel()
            try:
                await sess.task
            except asyncio.CancelledError:
                pass
        await self._edit_ephemeral(sess, f"**📍 Resumed tracking bus {sess.vehicle_id}**")
        sess.task = asyncio.create_task(self._poll_loop(sess))
        return True

    async def _poll_loop(self, sess: TransitSession) -> None:
        from http_session import get_http_session
        from transit_discord import _bus_location_embed
        from transit_service import (
            fetch_vehicles,
            get_person_home_state,
            haversine_m,
            resolve_landmark,
        )

        try:
            while True:
                if time.monotonic() - sess.started_at > session_ttl_seconds():
                    await self._finalize(sess, "Session timed out (30 min).")
                    return

                if sess.tick_count >= max_ticks():
                    await self._prompt_continue(sess)
                    return

                if sess.tick_count > 0:
                    await asyncio.sleep(poll_seconds())
                sess.tick_count += 1

                home_state = await get_person_home_state(sess.person_id)
                if home_state == "home":
                    await self._announce_home(sess)
                    await self._finalize(
                        sess, f"🏠 {sess.person_display} is home — tracking stopped."
                    )
                    return

                try:
                    http = get_http_session()
                    vehicles = await fetch_vehicles(http)
                    target = await resolve_landmark(
                        sess.landmark_key,
                        person_id=sess.landmark_person_id or sess.person_id,
                        session=http,
                    )
                except Exception as e:
                    log.warning("Transit track tick failed: %s", e)
                    await self._edit_ephemeral(
                        sess, f"⚠️ Transit feed unavailable ({e}). Retrying…"
                    )
                    await asyncio.sleep(error_retry_seconds())
                    continue

                if isinstance(target, str):
                    await self._finalize(sess, target)
                    return

                bus = next(
                    (v for v in vehicles if v.vehicle_id == sess.vehicle_id),
                    None,
                )
                if not bus:
                    await self._edit_ephemeral(
                        sess,
                        f"Vehicle **{sess.vehicle_id}** not in live feed (route {sess.route_id}).",
                    )
                    await asyncio.sleep(error_retry_seconds())
                    continue

                dist = haversine_m(bus.lat, bus.lon, target.lat, target.lon)
                trend = None
                if sess.last_distance_m is not None:
                    delta = dist - sess.last_distance_m
                    if delta < -30:
                        trend = "getting closer"
                    elif delta > 30:
                        trend = "moving away"
                    else:
                        trend = "holding distance"
                sess.last_distance_m = dist

                if target.radius_m and dist <= target.radius_m:
                    await self._finalize(
                        sess,
                        f"Bus **{sess.vehicle_id}** is at **{target.label}** (~{dist:.0f}m) — tracking stopped.",
                    )
                    return

                embed = _bus_location_embed(
                    bus,
                    dist,
                    target,
                    title=f"Tracking bus {sess.vehicle_id} · update {sess.tick_count}",
                    trend=trend,
                )
                await self._edit_ephemeral(sess, embed=embed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Transit poll loop crashed for user %s", sess.user_id)
            await self._finalize(sess, "Tracking stopped due to an error.")

    async def _prompt_continue(self, sess: TransitSession) -> None:
        sess.waiting_continue = True
        if sess.task and not sess.task.done():
            sess.task.cancel()
        from transit_discord import TransitContinueView

        view = TransitContinueView(sess.user_id)
        await self._edit_ephemeral(
            sess,
            f"Still tracking bus **{sess.vehicle_id}** ({sess.tick_count} updates). "
            "Continue for another 30 minutes?",
            view=view,
        )

    async def _announce_home(self, sess: TransitSession) -> None:
        if not self._bot:
            return
        ch_id = config.get("schedule_channel_id")
        if not ch_id:
            return
        channel = self._bot.get_channel(int(ch_id))
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(int(ch_id))
            except Exception:
                log.warning("Could not fetch #smithy for transit home announcement")
                return
        try:
            await channel.send(
                f"🏠 **{sess.person_display}** is home — stopped tracking bus **{sess.vehicle_id}**."
            )
        except Exception as e:
            log.warning("Smithy home announcement failed: %s", e)

    async def _finalize(self, sess: TransitSession, message: str) -> None:
        await self._edit_ephemeral(sess, message)
        await self.stop_session(sess.user_id, reason=message[:40])

    async def _edit_ephemeral(
        self,
        sess: TransitSession,
        content: str | None = None,
        view: Any = None,
        embed: Any = None,
    ) -> bool:
        if not self._bot or not sess.channel_id or not sess.message_id:
            return False
        if content is None and embed is None:
            return False
        try:
            channel = self._bot.get_channel(sess.channel_id)
            if channel is None:
                channel = await self._bot.fetch_channel(sess.channel_id)
            msg = channel.get_partial_message(sess.message_id)
            kwargs: dict[str, Any] = {"view": view}
            if content is not None:
                kwargs["content"] = content[:2000]
            if embed is not None:
                kwargs["embed"] = embed
            await msg.edit(**kwargs)
            sess.edit_failures = 0
            return True
        except Exception as e:
            sess.edit_failures += 1
            age_s = time.monotonic() - sess.started_at
            if sess.tick_count > 5 or age_s > 900 or sess.edit_failures >= 2:
                log.warning(
                    "Ephemeral track edit failed (tick=%s age=%.0fs user=%s): %s",
                    sess.tick_count,
                    age_s,
                    sess.user_id,
                    e,
                )
            else:
                log.debug("Ephemeral track edit failed: %s", e)
            if sess.edit_failures >= 2 and not sess.fallback_posted:
                await self._post_tracking_fallback(sess, content=content, embed=embed)
            return False

    async def _post_tracking_fallback(
        self,
        sess: TransitSession,
        *,
        content: str | None = None,
        embed: Any = None,
    ) -> None:
        """When ephemeral edits fail, post one visible update in-channel."""
        if not self._bot or sess.fallback_posted:
            return
        body = _fallback_message_text(content, embed)
        if not body:
            return
        sess.fallback_posted = True
        try:
            channel = self._bot.get_channel(sess.channel_id)
            if channel is None:
                channel = await self._bot.fetch_channel(sess.channel_id)
            await channel.send(
                f"<@{sess.user_id}> **Bus tracking** (ephemeral updates expired):\n"
                f"{body}\n"
                "_Use `/bus stop` to end tracking._"
            )
        except Exception as e:
            log.warning("Transit tracking fallback post failed: %s", e)


tracking_manager = TransitTrackingManager()
