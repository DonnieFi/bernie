"""Discord-safe chunked message sends (DM 1900 / channel 3800)."""

from __future__ import annotations

from typing import Any


async def send_chunked(
    channel,
    text: str,
    *,
    is_dm: bool = False,
    embed: Any = None,
    files=None,
    **send_kwargs,
):
    """Send long text in Discord-safe chunks. Returns the last Message sent."""
    limit = 1900 if is_dm else 3800
    text = text or ""
    sent_msg = None
    first = True

    if files:
        head = text[:limit] if text else None
        sent_msg = await channel.send(
            content=head, embed=embed if first else None, files=files, **send_kwargs
        )
        first = False
        text = text[limit:]
    elif len(text) <= limit:
        return await channel.send(text or " ", embed=embed, files=files, **send_kwargs)

    pos = 0
    while pos < len(text):
        chunk = text[pos : pos + limit]
        pos += limit
        sent_msg = await channel.send(chunk, embed=embed if first else None, **send_kwargs)
        first = False
    return sent_msg
