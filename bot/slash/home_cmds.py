"""Slash commands: home (family-bot-1od — real module, no exec)."""
from __future__ import annotations

from typing import Any

import discord
from discord import app_commands


def register(tree: app_commands.CommandTree, m: Any) -> None:
    """Register home slash commands. *m* is the bot module (helpers + config)."""
    # Explicit bot bindings (replaces exec + _LiveNS proxy)
    _is_admin_group = m._is_admin_group
    config = m.config
    frigate_service = getattr(m, 'frigate_service', None)
    get_session = m.get_session
    log = m.log
    update_config = m.update_config
    import asyncio
    import os
    import re
    from collections import defaultdict
    from datetime import datetime, timedelta, time, timezone
    from zoneinfo import ZoneInfo
    try:
        import aiohttp
    except ImportError:  # pragma: no cover
        aiohttp = None  # type: ignore

    @tree.command(name="temps", description="Current home temperatures from all sensors")
    async def cmd_temps(interaction: discord.Interaction):
        await interaction.response.defer()
        from ha_service import ha_service
        sensors = await ha_service.get_temperature_sensors()
        if not sensors:
            await interaction.followup.send("❌ No temperature sensors found (or HA is unavailable).")
            return

        embed = discord.Embed(title="🌡️ Home Temperatures", color=discord.Color.orange())
        for s in sensors:
            eid = s.get("entity_id", "")
            name = s.get("attributes", {}).get("friendly_name", eid)
            current = s.get("state", "?")
            unit = s.get("attributes", {}).get("unit_of_measurement", "°C")

            history = await ha_service.get_temperature_history(eid, hours=24)
            temps = []
            for h in history:
                try:
                    temps.append(float(h.get("state", "")))
                except (ValueError, TypeError):
                    pass

            if temps:
                value = f"{current}{unit}  (↑ {max(temps):.1f} / ↓ {min(temps):.1f})"
            else:
                value = f"{current}{unit}"
            embed.add_field(name=name, value=value, inline=False)

        await interaction.followup.send(embed=embed)


    @tree.command(name="ha_entities", description="List or search Home Assistant smart home devices")
    @app_commands.describe(
        domain="Optional Home Assistant domain to filter by",
        query="Optional keyword to search across friendly names and entity IDs"
    )
    @app_commands.choices(domain=[
        app_commands.Choice(name="Light", value="light"),
        app_commands.Choice(name="Sensor", value="sensor"),
        app_commands.Choice(name="Switch", value="switch"),
        app_commands.Choice(name="Media Player", value="media_player"),
        app_commands.Choice(name="Climate", value="climate"),
        app_commands.Choice(name="Automation", value="automation"),
        app_commands.Choice(name="Binary Sensor", value="binary_sensor"),
    ])
    async def cmd_ha_entities(
        interaction: discord.Interaction,
        domain: str | None = None,
        query: str | None = None
    ):
        # Device inventory includes locks/alarms — not for kids/guests
        if not _is_admin_group(interaction):
            await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
            return
        await interaction.response.defer()
        from ha_service import ha_service

        states = []
        if domain:
            states = await ha_service.get_live_states(domain=domain)
        else:
            states = await ha_service.get_live_states()

        if not states:
            await interaction.followup.send("❌ No Home Assistant devices found or Home Assistant is currently unreachable.")
            return

        if query:
            q = query.lower().strip()
            states = [
                s for s in states
                if q in s.get("entity_id", "").lower()
                or q in s.get("attributes", {}).get("friendly_name", "").lower()
            ]

        if not states:
            filter_desc = f" matching '{query}'" if query else ""
            domain_desc = f" under domain '{domain}'" if domain else ""
            await interaction.followup.send(f"❌ No entities found{domain_desc}{filter_desc}.")
            return

        states.sort(key=lambda s: s.get("attributes", {}).get("friendly_name", s.get("entity_id", "")).lower())

        title = "🏠 Home Assistant Entities"
        if domain:
            title = f"🏠 HA {domain.title()} Entities"
        if query:
            title += f" (Search: '{query}')"

        embed = discord.Embed(title=title, color=discord.Color.blue())

        lines = []
        for s in states:
            eid = s.get("entity_id", "")
            friendly = s.get("attributes", {}).get("friendly_name", eid)
            state_val = s.get("state", "unknown")
            lines.append(f"• **{friendly}** (`{eid}`) → `{state_val}`")

        current_chunk = []
        current_len = 0
        field_count = 0

        for line in lines:
            if current_len + len(line) + 1 > 1000:
                embed.add_field(
                    name=f"Devices {field_count * 10 + 1}-{field_count * 10 + len(current_chunk)}" if field_count > 0 else "Devices",
                    value="\n".join(current_chunk),
                    inline=False
                )
                current_chunk = [line]
                current_len = len(line)
                field_count += 1
                if field_count >= 24:
                    break
            else:
                current_chunk.append(line)
                current_len += len(line) + 1

        if current_chunk and field_count < 25:
            embed.add_field(
                name="Devices (continued)" if field_count > 0 else "Devices",
                value="\n".join(current_chunk),
                inline=False
            )

        embed.set_footer(text=f"Total: {len(states)} entities found")
        await interaction.followup.send(embed=embed)


    @tree.command(name="speedtest", description="Check internet speed (from UniFi WAN tests)")
    @app_commands.describe(
        count="Number of recent results to show (1–10, default 1)",
        live="Trigger a fresh speed test right now and wait for the result",
    )
    async def cmd_speedtest(interaction: discord.Interaction, count: int = 1, live: bool = False):
        from tools.network import _unifi_creds, _fetch_speedtest_history, _format_entry

        host, key = _unifi_creds(config)
        if not key:
            await interaction.response.send_message("❌ UniFi key not configured.", ephemeral=True)
            return

        await interaction.response.defer()

        if live:
            try:
                entries = await _fetch_speedtest_history(host, key)
            except Exception as e:
                await interaction.followup.send(f"❌ Could not reach UniFi: {e}")
                return
            latest_time = entries[-1]["time"] if entries else 0

            try:
                trigger_url = f"{host}/proxy/network/api/s/default/cmd/devmgr"
                session = get_session()
                async with session.post(
                        trigger_url,
                        headers={"x-api-key": key},
                        json={"cmd": "speedtest"},
                        ssl=False,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            await interaction.followup.send("❌ UniFi rejected the speed test trigger.")
                            return
            except Exception as e:
                await interaction.followup.send(f"❌ Could not reach UniFi: {e}")
                return

            msg = await interaction.followup.send("⏱ Speed test running on WAN (eth4)…")

            new_entry = None
            for _ in range(12):  # poll every 10 s, up to 2 min
                await asyncio.sleep(10)
                try:
                    fresh = await _fetch_speedtest_history(host, key)
                except Exception:
                    continue
                if fresh and fresh[-1]["time"] > latest_time:
                    new_entry = fresh[-1]
                    break

            if new_entry:
                dl = new_entry.get("download_mbps", 0)
                ul = new_entry.get("upload_mbps", 0)
                lat = new_entry.get("latency_ms", 0)
                await msg.edit(content=(
                    f"🌐 **Speed test result** (WAN eth4)\n"
                    f"↓ **{dl} Mbps**  ↑ **{ul} Mbps**  ·  **{lat} ms** latency"
                ))
            else:
                await msg.edit(content="⏱ Test triggered — result not yet in. Try `/speedtest` in a minute.")
        else:
            try:
                entries = await _fetch_speedtest_history(host, key)
            except Exception as e:
                await interaction.followup.send(f"❌ Could not reach UniFi: {e}")
                return
            if not entries:
                await interaction.followup.send("❌ No speed test history available.")
                return

            count = max(1, min(10, count))
            now = datetime.now(timezone.utc)
            recent = sorted(entries, key=lambda e: e["time"], reverse=True)[:count]
            lines = [_format_entry(e, now) for e in recent]
            title = "🌐 Latest speed test" if count == 1 else f"🌐 Last {count} speed tests"
            await interaction.followup.send(f"**{title}** (UniFi WAN eth4)\n" + "\n".join(lines))


    _snap_camera_choices = []
    if frigate_service is not None and getattr(frigate_service, "cameras", None):
        _snap_camera_choices = [
            app_commands.Choice(name=label, value=cam_id)
            for cam_id, label in frigate_service.cameras.items()
        ]
    if not _snap_camera_choices:
        _snap_camera_choices = [
            app_commands.Choice(name=label, value=cam_id)
            for cam_id, label in (config.get("frigate", {}) or {}).get("cameras", {}).items()
        ] or [
            app_commands.Choice(name="Kitchen (cam 8)", value="cam_8"),
            app_commands.Choice(name="Front Door (cam 18)", value="cam_18"),
        ]

    @tree.command(name="snap", description="Grab a camera snapshot from Frigate")
    @app_commands.describe(camera="Which camera to snapshot")
    @app_commands.choices(camera=_snap_camera_choices)
    async def cmd_snap(interaction: discord.Interaction, camera: str = "cam_18"):
        # Camera stills are security-sensitive (same bar as frigate_mode)
        if not _is_admin_group(interaction):
            await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
            return
        await interaction.response.defer()
        from io import BytesIO
        if frigate_service is None:
            await interaction.followup.send("❌ Frigate service not available.")
            return
        result = await frigate_service.get_snapshot(camera)
        if not result:
            await interaction.followup.send("❌ Could not fetch snapshot from Frigate.")
            return
        data, _ = result
        await interaction.followup.send(
            content=f"📷 {camera}",
            file=discord.File(BytesIO(data), filename=f"{camera}.jpg"),
        )


    @tree.command(name="frigate_mode", description="Set Frigate alert mode (on, off, or test)")
    @app_commands.describe(mode="on (standard), off (silenced), test (bypass suppression)")
    @app_commands.choices(mode=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
        app_commands.Choice(name="test", value="test"),
    ])
    async def cmd_frigate_mode(interaction: discord.Interaction, mode: str):
        if not _is_admin_group(interaction):
            await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            await update_config({"frigate": {"mode": mode, "test_mode": None}})
        except Exception as exc:
            log.error("frigate_mode update_config failed: %s", exc, exc_info=True)
            await interaction.followup.send(
                f"❌ Failed to save Frigate mode ({mode!r}). Check bot logs.",
                ephemeral=True,
            )
            return

        status_map = {
            "on": "ON (standard presence-based alerts)",
            "off": "OFF (alerts silenced)",
            "test": "TEST (alerts always active, home or away)"
        }
        await interaction.followup.send(f"🔔 Frigate mode is now **{status_map.get(mode, mode)}**.")


    _frigate_camera_choices = [
        app_commands.Choice(name=label, value=cam_id)
        for cam_id, label in config.get("frigate", {}).get("cameras", {}).items()
    ] or [
        app_commands.Choice(name="Kitchen (cam 8)", value="cam_8"),
        app_commands.Choice(name="Front Door (cam 18)", value="cam_18"),
    ]

    @tree.command(name="frigate_camera", description="Enable or disable alerts for a specific Frigate camera")
    @app_commands.describe(camera="Camera to toggle", enabled="on to enable alerts, off to disable")
    @app_commands.choices(
        camera=_frigate_camera_choices,
        enabled=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ],
    )
    async def cmd_frigate_camera(interaction: discord.Interaction, camera: str, enabled: str):
        if not _is_admin_group(interaction):
            await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await update_config({"frigate": {"cameras_enabled": {camera: (enabled == "on")}}})
        except Exception as exc:
            log.error("frigate_camera update_config failed: %s", exc, exc_info=True)
            await interaction.followup.send(
                f"❌ Failed to save camera setting for {camera!r}. Check bot logs.",
                ephemeral=True,
            )
            return
        camera_label = config.get("frigate", {}).get("cameras", {}).get(camera, camera)
        state_str = "enabled" if enabled == "on" else "disabled"
        await interaction.followup.send(f"📷 **{camera_label}** alerts are now **{state_str}**.")

