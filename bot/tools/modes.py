"""Mode switching tool (Phase 28 Wave 2c)."""

from tools import ROLE_ADMIN, tool
from modes import set_mode_override, get_mode, load_all_modes


@tool(
    name="switch_mode",
    description=(
        "Temporarily switch Bernie into a specific operational mode "
        "(chef, tutor, debug, security, home_automation, wind-down, ops, concierge, etc.). "
        "Pass 'auto', 'clear', or 'none' to remove the override and return to normal dynamic detection."
    ),
    is_write=False,
    input_schema={
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "description": "Mode slug (e.g. chef, tutor, debug, ops) or 'auto'/'clear' to reset to dynamic detection",
            }
        },
        "required": ["mode"],
    },
    role_required=ROLE_ADMIN,
    tier=3,
)
async def handle_switch_mode(args: dict, ctx) -> str:
    desired = args.get("mode", "").strip().lower()

    # Support clearing the override
    if desired in ("auto", "clear", "none", "default"):
        set_mode_override(None)
        return "Mode override cleared. Bernie will now use normal auto-detection (channel + keywords + quiet hours)."

    if not get_mode(desired):
        loaded = load_all_modes()
        valid = ", ".join(sorted(loaded.keys()))
        return f"Unknown mode '{desired}'. Valid modes: {valid} (or use 'auto'/'clear' to reset)"

    set_mode_override(desired)
    return f"✅ Switched to **{desired}** mode. This will stay active until you clear it (use 'auto'/'clear') or restart."
