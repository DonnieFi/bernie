"""Message preparation helpers (Phase 4.4 Session 2)."""

_DEFAULT_HISTORY_VERBATIM_TAIL = 4
_TOOL_RESULT_STUB = "[tool result — pruned from history]"


def history_verbatim_tail(config: dict | None) -> int:
    ctx = (config or {}).get("context") or {}
    raw = ctx.get("history_verbatim_tail", _DEFAULT_HISTORY_VERBATIM_TAIL)
    try:
        n = int(raw)
        return max(2, min(n, 12))
    except (TypeError, ValueError):
        return _DEFAULT_HISTORY_VERBATIM_TAIL


def extract_last_user_message(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        parts.append(text)
            combined = " ".join(parts).strip()
            if combined:
                return combined
    return ""


def prune_old_tool_results(
    messages: list[dict],
    *,
    verbatim_tail: int | None = None,
) -> list[dict]:
    tail_n = verbatim_tail if verbatim_tail is not None else _DEFAULT_HISTORY_VERBATIM_TAIL
    if len(messages) <= tail_n:
        return messages
    prunable = messages[:-tail_n]
    tail = messages[-tail_n:]
    pruned = []
    for m in prunable:
        content = m.get("content")
        if isinstance(content, list):
            new_content = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    new_content.append({
                        "type": "tool_result",
                        "tool_use_id": block.get("tool_use_id", ""),
                        "content": _TOOL_RESULT_STUB,
                    })
                else:
                    new_content.append(block)
            pruned.append({**m, "content": new_content})
        else:
            pruned.append(m)
    return pruned + tail


def prepare_messages(
    history: list[dict],
    user_message: str | None = None,
    config: dict | None = None,
) -> list[dict]:
    """Sanitize history and current message for Anthropic API."""
    raw = [dict(m) for m in history]
    if user_message and user_message.strip():
        raw.append({"role": "user", "content": user_message})

    raw = prune_old_tool_results(
        raw, verbatim_tail=history_verbatim_tail(config),
    )

    filtered = []
    for m in raw:
        content = m.get("content")
        if content and (isinstance(content, list) or (isinstance(content, str) and content.strip())):
            filtered.append(m)

    if not filtered:
        return []

    final = []
    for m in filtered:
        if not final:
            if m["role"] == "user":
                final.append(m)
            continue

        if m["role"] == final[-1]["role"]:
            if isinstance(m["content"], str) and isinstance(final[-1]["content"], str):
                final[-1]["content"] += f"\n\n{m['content']}"
        else:
            final.append(m)

    return final

# Back-compat for tests importing the constant
_HISTORY_VERBATIM_TAIL = _DEFAULT_HISTORY_VERBATIM_TAIL
