"""Discord slash commands and views for Halifax Transit tracking."""
from __future__ import annotations

import logging

import discord
from discord import app_commands

from config import config
from http_session import get_http_session
from constants import registry as person_registry
from transit_service import (
    LatLon,
    VehicleSnapshot,
    fetch_vehicles,
    filter_route,
    format_proximity,
    format_route_list,
    list_landmark_choices,
    maps_link,
    nearest_vehicle,
    normalize_route_id,
    refresh_zones,
    resolve_landmark,
    static_map_url,
)
from transit_tracking import tracking_manager

log = logging.getLogger(__name__)

bus_group = app_commands.Group(
    name="bus",
    description="Halifax Transit live GPS — use /bus help for usage",
)

BUS_HELP_TEXT = (
    "**🚌 /bus — Halifax Transit (live GPS)**\n\n"
    "**Commands**\n"
    "• `/bus help` — this guide\n"
    "• `/bus route <number>` — all active buses on a route (e.g. `/bus route 4`)\n"
    "• `/bus near <route> <landmark>` — nearest bus (e.g. `/bus near 4 sacredheart`)\n"
    "• `/bus track <vehicle> <route> [to] [person]` — track until home or a landmark\n"
    "• `/bus stop` — end your active tracking session\n\n"
    "**Landmarks** — from Home Assistant zones: `home`, `sacredheart`, `school`, "
    "`caller` (your phone GPS), or any zone slug Bernie knows (autocomplete on `to`).\n\n"
    "**Tracking**\n"
    "Only runs while you ask — not all day. Ephemeral updates every **3 min** (only you see them). "
    "After **30 min** you get **Keep tracking** / **Stop** buttons. When the tracked person "
    "arrives **home**, Bernie posts in **#smithy**.\n\n"
    "**Tips**\n"
    "• **Route number is required** on `route`, `near`, and `track`.\n"
    "• **Vehicle ID** — copy from `/bus route` (e.g. `3160`).\n"
    "• Distances are **straight-line**, not drive time — no arrival ETAs yet.\n"
    "• **`near` and `track`** show a map image + Google Maps link (not just text).\n"
    "• Ferries and buses share the feed; filter by route number.\n\n"
    "**Natural language** — in chat you can ask Bernie (e.g. “any route 4 buses near Sacred Heart?”); "
    "use `/bus track` when you want repeated updates.\n\n"
    "**Who can use this?** Everyone in the family — kids, parents, any channel Bernie is in."
)


def _bus_location_embed(
    v: VehicleSnapshot,
    distance_m: float,
    target: LatLon,
    *,
    title: str | None = None,
    trend: str | None = None,
) -> discord.Embed:
    """Discord embed with static map image + Google Maps link (shows reliably)."""
    gmaps = maps_link(v.lat, v.lon)
    speed = f"{v.speed_kmh:.0f} km/h" if v.speed_kmh is not None else "—"
    trend_bit = f" · {trend}" if trend else ""
    embed = discord.Embed(
        title=title or f"Route {v.route_id} bus near {target.label}",
        description=(
            f"**Vehicle {v.vehicle_id}** · ~{distance_m:.0f}m straight-line{trend_bit}\n"
            f"**Status:** {_bearing_cardinal(v)} at {speed}"
        ),
        url=gmaps,
        color=0x2B7A78,
    )
    embed.set_image(url=static_map_url(v.lat, v.lon))
    embed.add_field(name="Google Maps", value=f"[Open full map]({gmaps})", inline=False)
    return embed


def _bearing_cardinal(v: VehicleSnapshot) -> str:
    from transit_service import _bearing_cardinal as _card

    return _card(v.bearing)


def _resolve_caller_person(interaction: discord.Interaction) -> tuple[str, str]:
    pid = person_registry.resolve(interaction.user.id) or "unknown"
    display = person_registry.display_name(pid) or interaction.user.display_name
    return pid, display


async def _landmark_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    from transit_service import zones_cache_age_seconds

    if zones_cache_age_seconds() < 0:
        await refresh_zones()
    choices = list_landmark_choices()
    cur = (current or "").lower()
    out = [app_commands.Choice(name=c, value=c) for c in choices if cur in c.lower()]
    return out[:25]


@bus_group.command(name="help", description="How to use /bus (route, near, track, stop)")
@app_commands.default_permissions()
async def bus_help(interaction: discord.Interaction):
    # Public (non-ephemeral) so the whole channel can see the guide — not just the asker.
    await interaction.response.send_message(BUS_HELP_TEXT[:2000])


@bus_group.command(name="route", description="All active buses on a route")
@app_commands.describe(route="Route number (e.g. 4)")
@app_commands.default_permissions()
async def bus_route(interaction: discord.Interaction, route: str):
    await interaction.response.defer(ephemeral=True)
    try:
        session = get_http_session()
        vehicles = await fetch_vehicles(session)
        text = format_route_list(vehicles, route)
        await interaction.followup.send(text[:2000], ephemeral=True)
    except Exception as e:
        await interaction.followup.send(
            f"❌ Halifax Transit feed unavailable: {e}", ephemeral=True
        )


@bus_group.command(name="near", description="Nearest bus on a route to a landmark")
@app_commands.describe(
    route="Route number (e.g. 4)",
    landmark="home, sacredheart, caller (your GPS), or any HA zone",
)
@app_commands.autocomplete(landmark=_landmark_autocomplete)
@app_commands.default_permissions()
async def bus_near(interaction: discord.Interaction, route: str, landmark: str):
    await interaction.response.defer(ephemeral=True)
    person_id, _ = _resolve_caller_person(interaction)
    try:
        session = get_http_session()
        await refresh_zones()
        target = await resolve_landmark(landmark, person_id=person_id, session=session)
        if isinstance(target, str):
            await interaction.followup.send(f"❌ {target}", ephemeral=True)
            return
        vehicles = filter_route(await fetch_vehicles(session), route)
        bus, dist = nearest_vehicle(vehicles, target)
        if not bus or dist is None:
            await interaction.followup.send(
                f"No active vehicles on route {normalize_route_id(route)}.",
                ephemeral=True,
            )
            return
        embed = _bus_location_embed(bus, dist, target)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(
            f"❌ Halifax Transit feed unavailable: {e}", ephemeral=True
        )


@bus_group.command(name="track", description="Track a bus until home or destination")
@app_commands.describe(
    vehicle="Vehicle ID (e.g. 3160)",
    route="Route number",
    to="Landmark to monitor distance toward (default home)",
    person="Who to watch for arriving home (default: you)",
)
@app_commands.autocomplete(to=_landmark_autocomplete)
@app_commands.default_permissions()
async def bus_track(
    interaction: discord.Interaction,
    vehicle: str,
    route: str,
    to: str = "home",
    person: str | None = None,
):
    await interaction.response.defer(ephemeral=True)
    tracker_id, tracker_display = _resolve_caller_person(interaction)
    if person:
        tracked_id = person_registry.resolve(person) or person.lower()
        tracked_display = person_registry.display_name(tracked_id) or person
    else:
        tracked_id, tracked_display = tracker_id, tracker_display
    try:
        session = get_http_session()
        await refresh_zones()
        target = await resolve_landmark(to, person_id=tracker_id, session=session)
        if isinstance(target, str):
            await interaction.followup.send(f"❌ {target}", ephemeral=True)
            return
        vehicles = await fetch_vehicles(session)
        bus = next((v for v in vehicles if v.vehicle_id == vehicle), None)
        if not bus:
            await interaction.followup.send(
                f"❌ Vehicle **{vehicle}** not in live feed.", ephemeral=True
            )
            return
        from transit_service import haversine_m

        dist = haversine_m(bus.lat, bus.lon, target.lat, target.lon)
        initial_embed = _bus_location_embed(
            bus,
            dist,
            target,
            title=f"Tracking bus {vehicle} (route {route})",
        )
        tracking_manager.set_bot(interaction.client)
        await tracking_manager.start_session(
            user_id=interaction.user.id,
            channel_id=interaction.channel_id,
            person_id=tracked_id,
            person_display=tracked_display,
            vehicle_id=vehicle,
            route_id=route,
            landmark_key=to,
            landmark_person_id=tracker_id,
            interaction=interaction,
            initial_embed=initial_embed,
        )
    except Exception as e:
        log.exception("bus track failed")
        await interaction.followup.send(f"❌ {e}", ephemeral=True)


@bus_group.command(name="stop", description="Stop your active bus tracking session")
@app_commands.default_permissions()
async def bus_stop(interaction: discord.Interaction):
    stopped = await tracking_manager.stop_session(interaction.user.id, reason="user stop")
    if stopped:
        await interaction.response.send_message("🛑 Bus tracking stopped.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "No active bus tracking session.", ephemeral=True
        )


class TransitContinueView(discord.ui.View):
    def __init__(self, owner_user_id: int):
        super().__init__(timeout=600)
        self.owner_user_id = owner_user_id

    async def _check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_user_id:
            await interaction.response.send_message(
                "Only the person who started tracking can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Keep tracking", style=discord.ButtonStyle.primary)
    async def btn_continue(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await self._check(interaction):
            return
        ok = await tracking_manager.extend_session(interaction.user.id)
        if ok:
            await interaction.response.send_message(
                "✅ Tracking extended for another 30 minutes.", ephemeral=True
            )
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message(
                "No session waiting to continue.", ephemeral=True
            )

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.secondary)
    async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        await tracking_manager.stop_session(interaction.user.id, reason="user stop")
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="🛑 Bus tracking stopped.", view=self
        )


def register_bus_commands(tree: app_commands.CommandTree) -> None:
    tree.add_command(bus_group)


async def transit_zones_weekly_refresh() -> None:
    """Background: refresh HA zone cache weekly."""
    try:
        await refresh_zones(force=True)
    except Exception as e:
        log.warning("Weekly transit zone refresh failed: %s", e)


def init_transit(bot: discord.Client) -> None:
    tracking_manager.set_bot(bot)
