"""Slash command: /flight — FlightAware status (family-bot)."""
from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from flight_service import FlightPhase, track_flight


def _phase_color(phase: FlightPhase) -> discord.Color:
    return {
        FlightPhase.landed: discord.Color.green(),
        FlightPhase.en_route: discord.Color.blue(),
        FlightPhase.scheduled: discord.Color.light_grey(),
        FlightPhase.cancelled: discord.Color.red(),
        FlightPhase.diverted: discord.Color.orange(),
        FlightPhase.unknown: discord.Color.dark_grey(),
    }.get(phase, discord.Color.dark_grey())


def _embed_for(result) -> discord.Embed:
    phase_label = result.phase.value.replace("_", " ").title()
    embed = discord.Embed(
        title=f"✈️ {result.ident} — {phase_label}",
        description=result.status_detail or result.route,
        color=_phase_color(result.phase),
        url=result.google_maps_url or result.map_url,
    )
    if result.static_map_image_url:
        embed.set_image(url=result.static_map_image_url)
    if result.route:
        embed.add_field(name="Route", value=result.route, inline=True)
    t = result.times
    if t.scheduled_departure or t.actual_departure or t.estimated_departure:
        dep_lines = []
        if t.scheduled_departure:
            dep_lines.append(f"Sched: {t.scheduled_departure}")
        if t.estimated_departure and t.estimated_departure != t.scheduled_departure:
            dep_lines.append(f"Est: {t.estimated_departure}")
        if t.actual_departure:
            dep_lines.append(f"**Actual: {t.actual_departure}**")
        embed.add_field(name="Departure", value="\n".join(dep_lines), inline=True)
    if result.phase == FlightPhase.landed and t.actual_arrival:
        embed.add_field(name="Arrival", value=f"**Landed {t.actual_arrival}**", inline=True)
    elif t.estimated_arrival or t.scheduled_arrival:
        arr_lines = []
        if t.scheduled_arrival:
            arr_lines.append(f"Sched: {t.scheduled_arrival}")
        if t.estimated_arrival and t.estimated_arrival != t.scheduled_arrival:
            arr_lines.append(f"Est: {t.estimated_arrival}")
        if result.remaining_display:
            arr_lines.append(f"**~{result.remaining_display} remaining**")
        embed.add_field(name="Arrival", value="\n".join(arr_lines), inline=True)
    if result.relative_position:
        embed.add_field(name="Position", value=result.relative_position, inline=False)
    if result.position:
        p = result.position
        coords = f"`{p.latitude:.4f}, {p.longitude:.4f}`"
        extras = []
        if p.altitude_ft is not None:
            extras.append(f"FL{p.altitude_ft // 100}" if p.altitude_ft >= 1000 else f"{p.altitude_ft} ft")
        if p.ground_speed_kts is not None:
            extras.append(f"{p.ground_speed_kts} kts")
        if p.heading_deg is not None:
            extras.append(f"hdg {p.heading_deg}°")
        value = coords + (" · " + " · ".join(extras) if extras else "")
        embed.add_field(name="Coordinates", value=value, inline=False)
    if result.progress_percent is not None and result.phase == FlightPhase.en_route:
        embed.add_field(name="Progress", value=f"{result.progress_percent}%", inline=True)
    if result.google_maps_url:
        embed.add_field(
            name="Google Maps",
            value=f"[Open full map]({result.google_maps_url})",
            inline=False,
        )
    if result.map_url:
        embed.add_field(name="FlightAware", value=f"[Live track]({result.map_url})", inline=False)
    return embed


def register(tree: app_commands.CommandTree, m: Any) -> None:
    """Register /flight on *tree*. *m* is the bot module (unused)."""

    @tree.command(name="flight", description="Track a flight by number (e.g. AC123, OCN74, 4Y74)")
    @app_commands.describe(flight_number="Airline flight number or ident")
    async def cmd_flight(interaction: discord.Interaction, flight_number: str):
        await interaction.response.defer()
        ident = flight_number.strip()
        if not ident:
            await interaction.followup.send("❌ flight_number is required.")
            return
        try:
            result = await track_flight(ident)
            await interaction.followup.send(embed=_embed_for(result))
        except LookupError:
            await interaction.followup.send(f"❌ No flight found for **{ident.upper()}** in the current window.")
        except RuntimeError as exc:
            await interaction.followup.send(f"❌ FlightAware unavailable: {exc}")
        except Exception as exc:
            log = getattr(m, "log", None)
            if log:
                log.error("cmd_flight error: %s", exc, exc_info=True)
            await interaction.followup.send(f"❌ Flight lookup failed: {exc}")
