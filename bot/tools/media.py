"""Media (speaker/playback) tool handlers — all WRITE. (TTS/announce retired — no hardware path.)"""
from __future__ import annotations

import json
import re

from tools import ROLE_ALL, ROLE_PARENTS, tool
from tool_utils import strip_markdown


def _ha(ctx):
    """Prefer the injected HA service; fall back to module-level singleton."""
    svc = getattr(ctx.services, "ha", None)
    if svc is not None:
        return svc
    from ha_service import ha_service
    return ha_service


def _resolve_media_player(ctx, speaker: str) -> str | None:
    """Resolve a speaker name/alias to a media_player.* entity_id."""
    ha_service = _ha(ctx)
    entity_id = ha_service.resolve_entity_id(speaker) or speaker
    if not entity_id.startswith("media_player."):
        if "." not in entity_id:
            entity_id = f"media_player.{entity_id.replace('.', '_')}"
        else:
            return None
    return entity_id


@tool(
    name="play_media",
    description=(
        "Play media on a Home Assistant media player (e.g. Chromecast). "
        "Note: Netflix and Disney+ do NOT work via this API."
    ),
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "speaker":            {"type": "string", "description": "Speaker name or entity_id"},
            "media_content_id":   {"type": "string", "description": "What to play (URL, app content ID)"},
            "media_content_type": {"type": "string", "description": "Type hint for HA (music, video, app)"},
            "volume":             {"type": "number", "description": "Volume 0.0–1.0 (optional)"},
        },
        "required": ["speaker", "media_content_id"],
    },
    role_required=ROLE_PARENTS,
    tier=2,
)
async def handle_play_media(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called play_media({args})]"
    ha_service = _ha(ctx)

    speaker = args["speaker"]
    raw_id = str(args["media_content_id"])
    media_content_type = args.get("media_content_type")

    if (
        "youtube" in raw_id.lower()
        or "youtu.be" in raw_id.lower()
        or (media_content_type and "youtube" in media_content_type.lower())
    ):
        media_content_type = "cast"
        m = re.search(r'(?:v=|/|be/)([0-9A-Za-z_-]{11})', raw_id)
        vid = m.group(1) if m else raw_id
        media_content_id = json.dumps({"app_name": "youtube", "media_id": vid})
    else:
        media_content_id = raw_id
        if not media_content_type:
            media_content_type = (
                "video"
                if str(media_content_id).startswith(("http://", "https://"))
                else "app"
            )

    volume = args.get("volume")
    entity_id = _resolve_media_player(ctx, speaker)
    if entity_id is None:
        return f"That doesn't look like a media player entity: {speaker}"

    ok = await ha_service.play_media(
        entity_id, media_content_id,
        media_content_type=media_content_type, volume=volume,
    )
    return f"Playing on {entity_id}." if ok else f"Failed to play media on {entity_id}."


@tool(
    name="media_control",
    description="Control media playback on a speaker — pause, play, stop, skip.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "speaker": {"type": "string", "description": "Speaker name or entity_id"},
            "command": {"type": "string", "enum": ["play", "pause", "stop", "next", "previous"]},
            "volume":  {"type": "number", "description": "Volume 0.0–1.0 (optional)"},
        },
        "required": ["speaker", "command"],
    },
    role_required=ROLE_PARENTS,
    tier=2,
)
async def handle_media_control(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called media_control({args})]"
    ha_service = _ha(ctx)

    speaker = args["speaker"]
    command = args["command"]
    volume = args.get("volume")
    entity_id = _resolve_media_player(ctx, speaker)
    if entity_id is None:
        return f"That doesn't look like a media player entity: {speaker}"
    ok = await ha_service.media_control(entity_id, command, volume=volume)
    return f"{command.title()} on {entity_id}." if ok else f"Failed to {command} {entity_id}."


# ── get_camera_snapshot (READ) ──────────────────────────────────────────────
@tool(
    name="get_camera_snapshot",
    description=(
        "Get a live snapshot from a security camera. Use when asked 'show me "
        "the door', 'who's at the kitchen?', or 'what's on the camera?'. YOU "
        "MUST include the markdown image link from the tool result in your "
        "response so the user can see it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "camera": {
                "type": "string",
                "description": "Camera ID: 'cam_8' (Kitchen) or 'cam_18' (Front Door). Defaults to 'cam_18' for door/outside questions.",
            }
        },
        "required": ["camera"],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_camera_snapshot(args: dict, ctx):
    import base64
    from frigate_service import frigate_service
    camera = args.get("camera", "cam_18")
    result = await frigate_service.get_snapshot(camera)
    if not result:
        return f"Error: Could not fetch snapshot for {camera}."
    data, content_type = result
    b64 = base64.b64encode(data).decode("utf-8")
    return [
        {
            "type": "text",
            "text": (
                f"Snapshot captured from {camera}. I have attached the image "
                f"data for you to see. IMPORTANT: The user cannot see the "
                f"attached image data; you MUST include the markdown link "
                f"`![{camera}](/api/cameras/{camera}/snapshot)` in your "
                f"response if you want the user to see the image."
            ),
        },
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": content_type,
                "data": b64,
            },
        },
    ]
