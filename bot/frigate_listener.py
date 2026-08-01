import asyncio
import io
import json
import logging
import os
from datetime import datetime

import aiomqtt
import discord
from zoneinfo import ZoneInfo

from config import config
from frigate_service import frigate_service
from db_binding import get_database
import db_writes

log = logging.getLogger("bernie.frigate")

_FRIGATE_DEDUP_KEY = -1  # sentinel remind_min in sent_reminders — won't collide with calendar reminders
_seen_tracks: set[str] = set()  # in-memory per-session dedup; DB catches restarts


def _frigate_notification_channel_id(cfg: dict | None = None) -> int | str | None:
    """Resolve where Frigate snapshots post — matches security mode channel pins."""
    cfg = cfg if cfg is not None else config
    frigate_cfg = cfg.get("frigate") or {}
    return (
        frigate_cfg.get("notification_channel_id")
        or cfg.get("security_channel_id")
        or cfg.get("anvil_channel_id")
    )


def _is_night_hours() -> bool:
    tz_name = config.get("timezone", "America/Halifax")
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz).time()
    night = config.get("frigate", {}).get("night_hours", {"start": "22:00", "end": "06:00"})
    start = datetime.strptime(night.get("start", "22:00"), "%H:%M").time()
    end = datetime.strptime(night.get("end", "06:00"), "%H:%M").time()
    if start > end:  # window wraps midnight (e.g. 22:00–06:00)
        return now >= start or now < end
    return start <= now < end


async def _is_away() -> bool:
    """True unless a parent is *confirmed* home.

    Routes through presence_service (the staleness-aware, WiFi+GPS source the
    dashboard uses) rather than reading the raw HA person entity directly. The
    person entity can report a stale "home" for hours after a phone stops
    pinging GPS — that masked every daytime detection (2026-05-27). A parent
    counts as home only when presence reports home=True, which requires a fresh
    WiFi or fresh GPS-home signal; stale GPS and "away" both resolve to not-home.

    Fails toward sending: any error, missing presence data, or no parents
    configured → away, so a real intruder is never suppressed by a glitch.
    """
    from presence_service import presence_service
    from constants import registry

    family_members = config.get("family_members", {})
    parent_ids: list[str] = []
    for display_name, m in family_members.items():
        if m.get("role") not in ("parent", "parents", "admin"):
            continue
        cid = m.get("canonical_id") or registry.resolve(display_name) or display_name.lower()
        if cid:
            parent_ids.append(cid)

    if not parent_ids:
        log.warning("frigate _is_away: no parents in family_members — treating as away (sending)")
        return True

    try:
        # family-bot-1bf.4: DB presence only (no get_full_presence REST hydrate)
        anyone_home = await presence_service.is_any_home(parent_ids)
    except Exception as e:
        log.warning(f"frigate _is_away: presence lookup failed ({e}) — treating as away")
        return True

    # Confirmed-home wins: is_home already encodes fresh WiFi/GPS + 120s grace.
    if anyone_home:
        return False
    return True


async def _post_snapshot(bot: discord.Client, image_bytes: bytes | None, camera: str, label: str):
    channel_id = _frigate_notification_channel_id()
    channel = bot.get_channel(channel_id)
    if not channel:
        log.error(f"frigate_listener: channel {channel_id} not found")
        return

    tz_name = config.get("timezone", "America/Halifax")
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    date_str = now.strftime("%A, %b %d")
    time_str = now.strftime("%-I:%M %p")
    camera_label = frigate_service.cameras.get(camera, camera)

    msg = (
        f"🔔 **Frigate Alert**\n"
        f"**Camera:** {camera_label}\n"
        f"**Detection:** {label}\n"
        f"**When:** {date_str} at {time_str}"
    )
    if image_bytes is None:
        msg += "\n_(snapshot unavailable)_"

    try:
        file = discord.File(io.BytesIO(image_bytes), filename="detection.jpg") if image_bytes else None
        await channel.send(msg, file=file)
    except Exception as e:
        log.error(f"frigate_listener: failed to post alert for {camera} {label}: {e}")


async def frigate_listener_loop(bot: discord.Client):
    await bot.wait_until_ready()

    mqtt_cfg = config.get("mqtt", {})
    host = mqtt_cfg.get("host", "192.168.1.X")  # placeholder; set mqtt.host in config.json
    port = int(mqtt_cfg.get("port", 1883))
    user = os.environ.get("MQTT_USER")
    password = os.environ.get("MQTT_PASSWORD")
    log.info(f"Frigate listener starting — {host}:{port}")

    while True:
        try:
            async with aiomqtt.Client(
                hostname=host,
                port=port,
                username=user,
                password=password,
                identifier="bernie-frigate",
            ) as client:
                log.info("Frigate listener connected to MQTT broker")
                await client.subscribe("frigate/events")

                async for message in client.messages:
                    try:
                        data = json.loads(message.payload)
                    except Exception:
                        continue

                    if data.get("type") != "new":
                        continue

                    after = data.get("after", {})
                    camera = after.get("camera", "")
                    label = after.get("label", "")
                    event_id = after.get("id", "")

                    # Re-read cameras/labels each event so /reload config is honored
                    alert_cameras = set(frigate_service.cameras.keys())
                    alert_labels = set(config.get("frigate", {}).get("alert_labels", ["person"]))

                    if not event_id or camera not in alert_cameras or label not in alert_labels:
                        continue

                    if event_id in _seen_tracks:
                        continue
                    _seen_tracks.add(event_id)
                    if len(_seen_tracks) > 500:
                        _seen_tracks.clear()

                    mode = config.get("frigate", {}).get("mode", "on")
                    if mode == "off":
                        log.debug("Frigate alert suppressed — mode is OFF")
                        continue

                    cameras_enabled = config.get("frigate", {}).get("cameras_enabled", {})
                    if not cameras_enabled.get(camera, True):
                        log.debug(f"Frigate alert suppressed — camera {camera} disabled")
                        continue

                    if mode == "on":
                        if not _is_night_hours() and not await _is_away():
                            log.debug("Frigate alert suppressed — home and not night hours (mode: ON)")
                            continue

                    # mode == "test" falls through to process alert regardless of presence

                    if await get_database().is_reminder_sent(event_id, _FRIGATE_DEDUP_KEY):
                        continue
                    await db_writes.routed(
                        "mark_reminder_sent", event_id=event_id, remind_min=_FRIGATE_DEDUP_KEY
                    )

                    # Fetch the full snapshot (not cropped) as requested by the user.
                    # If the snapshot fails, still post a text-only alert — the event
                    # is already marked sent, so dropping here would lose it for good.
                    result = await frigate_service.get_event_snapshot(event_id, crop=False)
                    image_bytes = result[0] if result else None
                    if image_bytes is None:
                        log.warning(f"No snapshot for Frigate event {event_id} — posting text-only alert")
                    asyncio.create_task(_post_snapshot(bot, image_bytes, camera, label))
                    log.info(f"Frigate alert posted: {camera} {label} ({event_id})")

        except aiomqtt.MqttError as e:
            log.warning(f"Frigate MQTT error: {e} — reconnecting in 10s")
            await asyncio.sleep(10)
        except Exception as e:
            log.error(f"Frigate listener error: {e} — reconnecting in 10s")
            await asyncio.sleep(10)
