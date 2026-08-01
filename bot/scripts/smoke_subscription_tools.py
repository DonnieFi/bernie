#!/usr/bin/env python3
"""Smoke-test subscription chat tool loop (calendar + optional email).

Run inside bernie-cognition (bot mounted at /app, RW data):

  docker exec -w /app -e PYTHONPATH=/app bernie-cognition python3 scripts/smoke_subscription_tools.py
  docker exec -w /app -e PYTHONPATH=/app bernie-cognition python3 scripts/smoke_subscription_tools.py --email

Exit 0 when all requested checks pass.
"""
from __future__ import annotations

import argparse
import asyncio


async def _run_case(
    *,
    label: str,
    message: str,
    config: dict,
    channel_id: str,
    group: str,
) -> tuple[bool, list[dict]]:
    from llm.chat import chat_general

    print(f"\n=== {label} ===")
    print(f"prompt: {message!r}")
    attempts: list[dict] = []
    try:
        out = await chat_general(
            message,
            [],
            config,
            person_name="dad",
            group=group,
            actor_id="dad",
            channel_id=channel_id,
            suppress_shadow=True,
        )
    except Exception as exc:
        print(f"FAIL: {exc}")
        return False, attempts

    preview = (out or "").strip()
    if len(preview) > 1200:
        preview = preview[:1200] + "…"
    print(f"reply ({len(out or '')} chars):\n{preview}\n")

    if "Bernie fallback mode" in (out or ""):
        print("FAIL: fell back to Ollama instead of subscription tool loop")
        return False, attempts

    low = (out or "").lower()
    if label == "calendar":
        bad = ("saturtoolcall", "toolcall[", "evening update")
        if any(b in low for b in bad):
            print("FAIL: bad calendar reply (hallucinated tools or evening-update sprawl)")
            return False, attempts
        has_schedule = any(
            x in low for x in ("am", "pm", ":", "event", "nothing scheduled", "no events", "clear")
        )
        if not has_schedule and "calendar" not in low:
            print("FAIL: reply does not look like real calendar output")
            return False, attempts
    if label == "email":
        ok_markers = ("sent", "email sent", "delivered", "queued", "message id", "message_id")
        if not any(m in low for m in ok_markers):
            print("FAIL: reply does not confirm email send")
            return False, attempts
        if "smoke test" in low and "sent" not in low and "delivered" not in low:
            print("FAIL: email mentioned but send not confirmed")
            return False, attempts
    print(f"OK: {label}")
    return True, attempts


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--email",
        action="store_true",
        help="Also send a real smoke email to parent@example.com",
    )
    parser.add_argument(
        "--group",
        default="admin",
        help="RBAC group for tool execution (default: admin)",
    )
    args = parser.parse_args(argv)

    from config import reload_config
    from main import _common_setup

    setup = await _common_setup("cognition")
    container = setup.get("container")
    if container is None:
        print("FAIL: runtime init returned no container (check DISCORD_TOKEN / LLM routes)")
        return 1

    config = reload_config()
    channel_id = str(config.get("anvil_channel_id") or "")
    if not channel_id:
        print("FAIL: anvil_channel_id missing from config")
        await container.aclose()
        return 1

    active = config.get("active_model")
    print(f"active_model={active!r} channel={channel_id} group={args.group}")

    cases: list[tuple[str, str]] = [
        ("calendar", "What's on my calendar today? List events with times."),
    ]
    if args.email:
        cases.append(
            (
                "email",
                "Send an email to parent@example.com with subject 'Bernie smoke test' "
                "and body 'hi from subscription smoke check'. Use the send_email tool.",
            )
        )

    ok = True
    for label, message in cases:
        passed, _ = await _run_case(
            label=label,
            message=message,
            config=config,
            channel_id=channel_id,
            group=args.group,
        )
        if not passed:
            ok = False

    await container.aclose()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
