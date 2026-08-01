"""Slash commands: admin (family-bot-1od — real module, no exec)."""
from __future__ import annotations

from typing import Any

import discord
from discord import app_commands


def register(tree: app_commands.CommandTree, m: Any) -> None:
    """Register admin slash commands. *m* is the bot module (helpers + config)."""
    # Explicit bot bindings (replaces exec + _LiveNS proxy)
    DEFAULT_MODEL = m.DEFAULT_MODEL
    TZ = getattr(m, 'TZ', None)
    _container = getattr(m, '_container', None)
    _is_admin_group = m._is_admin_group
    _is_anvil = m._is_anvil
    _rebuild_person_legacy = m._rebuild_person_legacy
    cal = getattr(m, 'cal', None)
    config = m.config
    get_database = m.get_database
    get_mode = m.get_mode
    get_mode_override = m.get_mode_override
    get_model_info = m.get_model_info
    get_scheduler = m.get_scheduler
    get_session = m.get_session
    load_all_modes = m.load_all_modes
    log = m.log
    person_registry = m.person_registry
    reload_config = m.reload_config
    # bot re-exports update_config; some handlers also `from config import` locally
    update_config = m.update_config
    set_mode_override = m.set_mode_override
    set_model = m.set_model
    timing_snapshot = m.timing_snapshot
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

    @tree.command(name="config_summary", description="[Admin] Change the daily summary time")
    @app_commands.describe(hour="Hour (0-23)", minute="Minute (0-59)")
    async def cmd_config_summary(interaction: discord.Interaction, hour: int, minute: int):
        if not _is_anvil(interaction):
            await interaction.response.send_message("❌ This command only works in #anvil.", ephemeral=True)
            return
        if not _is_admin_group(interaction):
            await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
            return
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            await interaction.response.send_message("❌ Invalid time format. Hour: 0-23, Minute: 0-59.", ephemeral=True)
            return

        prior = timing_snapshot()
        old_hour = prior["summary_hour"]
        old_min = prior["summary_minute"]

        await interaction.response.defer(ephemeral=True)
        try:
            await update_config({
                "summary_hour": hour,
                "summary_minute": minute
            })
        except Exception as exc:
            log.error("config_summary update_config failed: %s", exc, exc_info=True)
            await interaction.followup.send("❌ Failed to save summary time. Check bot logs.", ephemeral=True)
            return

        if hour != old_hour or minute != old_min:
            get_scheduler().sync_intervals_from_config(prior)
            await interaction.followup.send(f"✅ Daily summary rescheduled to **{hour:02d}:{minute:02d}**", ephemeral=True)
        else:
            await interaction.followup.send(f"ℹ️ Summary time unchanged (**{hour:02d}:{minute:02d}**). Task was not reset.", ephemeral=True)


    @tree.command(name="config_reminders", description="[Admin] Change default reminder lead time")
    @app_commands.describe(minutes="Minutes before event (e.g. 15)")
    async def cmd_config_reminders(interaction: discord.Interaction, minutes: int):
        if not _is_anvil(interaction):
            await interaction.response.send_message("❌ This command only works in #anvil.", ephemeral=True)
            return
        if not _is_admin_group(interaction):
            await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
            return
        if minutes < 0:
            await interaction.response.send_message("❌ Minutes cannot be negative.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await update_config({"default_reminder_minutes": [minutes]})
        except Exception as exc:
            log.error("config_reminders update_config failed: %s", exc, exc_info=True)
            await interaction.followup.send("❌ Failed to save reminder setting. Check bot logs.", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Default reminder set to **{minutes} minutes** before events.", ephemeral=True)

    @tree.command(name="model", description="View current model + available list, or switch model (anvil only)")
    @app_commands.describe(name=f"Model name to switch to, or 'reset' to return to {DEFAULT_MODEL}")
    async def cmd_model(interaction: discord.Interaction, name: str | None = None):
        if not _is_anvil(interaction):
            await interaction.response.send_message("❌ Use #anvil for model changes.", ephemeral=True)
            return
        if not _is_admin_group(interaction):
            await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
            return

        if name is not None:
            await interaction.response.defer(ephemeral=True)
            if name == "reset":
                from model_registry import reset_model_from_config
                name = reset_model_from_config(config, DEFAULT_MODEL)

            from model_catalog import build_catalog, format_chain
            from model_registry import model_base_url, model_source
            catalog = {e.model: e for e in build_catalog(config)}
            entry = catalog.get(name)
            if entry is None and name != "reset":
                # still allow if registry knows it
                try:
                    model_source(name, config)
                except Exception:
                    await interaction.followup.send(f"❌ Unknown model `{name}`.", ephemeral=True)
                    return

            source = entry.provider if entry else model_source(name, config)
            base_url = None if source in ("anthropic", "codex", "grok") else model_base_url(name, config)
            if source == "ollama":
                base_url = config.get("ollama_base_url", "http://192.168.1.X:11434")
            chain = format_chain(entry, name, config)
            route = f"{source} · chain: {chain}"

            set_model(name, base_url)
            try:
                await update_config({"active_model": name})
            except Exception as exc:
                log.error("model switch update_config failed: %s", exc, exc_info=True)
                await interaction.followup.send(
                    f"❌ Failed to persist model `{name}`. Check bot logs.",
                    ephemeral=True,
                )
                return
            log.info(f"Model switched to {name} via {route} by {interaction.user.display_name}")
            from llm.clients import model_cache_support
            await interaction.followup.send(
                f"✅ Switched to `{name}` (provider `{source}`)\n"
                f"Fallback chain: `{chain}`\n{model_cache_support(name)}",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        current_model, current_base_url = get_model_info()
        from model_catalog import (
            build_catalog,
            filter_for_active_auth,
            format_chain,
            provider_readiness_map,
        )
        from subscription_complete import fetch_runner_health
        health = await fetch_runner_health(config)
        entries = build_catalog(config, runner_health=health)
        by_id = {e.model: e for e in entries}
        entry = by_id.get(current_model)
        chain = format_chain(entry, current_model, config)
        readiness = provider_readiness_map(config, runner_health=health)
        provider = entry.provider if entry else "unknown"
        visible_entries = filter_for_active_auth(entries, current_model)

        lines = []
        for e in visible_entries:
            mark = "→" if e.model == current_model else "•"
            lines.append(f"{mark} `{e.model}` ({e.provider}, {e.readiness})")
        body = "\n".join(lines[:40]) or "None configured."

        await interaction.followup.send(
            f"**Active:** `{current_model}` · provider `{provider}` · readiness "
            f"`{readiness.get(provider, 'unknown')}`\n"
            f"**Chain:** `{chain}`\n\n"
            f"**Catalog:**\n{body}",
            ephemeral=True,
        )


    @cmd_model.autocomplete('name')
    async def cmd_model_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        from model_catalog import build_catalog, filter_for_active_auth
        current_model, _ = get_model_info()
        entries = filter_for_active_auth(build_catalog(config), current_model)
        choices = ["reset"] + [e.model for e in entries]
        return [
            app_commands.Choice(name=m[:100], value=m)
            for m in choices
            if current.lower() in m.lower()
        ][:25]

    @tree.command(name="model-auth", description="Subscription provider auth readiness (anvil admin, secret-safe)")
    @app_commands.describe(provider="codex or grok")
    async def cmd_model_auth(interaction: discord.Interaction, provider: str):
        if not _is_anvil(interaction):
            await interaction.response.send_message("❌ Use #anvil.", ephemeral=True)
            return
        if not _is_admin_group(interaction):
            await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
            return
        provider = (provider or "").strip().lower()
        if provider not in ("codex", "grok"):
            await interaction.response.send_message("❌ Provider must be `codex` or `grok`.", ephemeral=True)
            return
        from subscription_complete import fetch_runner_health, format_model_auth_message
        health = await fetch_runner_health(config)
        providers = (health or {}).get("providers") or {}
        state = providers.get(provider, "unavailable")
        # Never surface device codes, tokens, or account identity.
        msg = format_model_auth_message(provider, state)
        await interaction.response.send_message(msg, ephemeral=True)

    @cmd_model_auth.autocomplete("provider")
    async def cmd_model_auth_autocomplete(interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=p, value=p)
            for p in ("codex", "grok")
            if current.lower() in p
        ]

    @tree.command(name="provider", description="Provider readiness + usage (anvil admin, secret-safe)")
    @app_commands.describe(action="status (default) or usage")
    async def cmd_provider(interaction: discord.Interaction, action: str = "status"):
        if not _is_anvil(interaction):
            await interaction.response.send_message("❌ Use #anvil.", ephemeral=True)
            return
        if not _is_admin_group(interaction):
            await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        from model_catalog import build_catalog, provider_readiness_map, format_chain
        from subscription_complete import fetch_runner_health, subscription_usage_snapshot
        health = await fetch_runner_health(config)
        readiness = provider_readiness_map(config, runner_health=health)
        current_model, _ = get_model_info()
        entry = next(
            (e for e in build_catalog(config, runner_health=health) if e.model == current_model),
            None,
        )
        action = (action or "status").strip().lower()
        lines = [
            f"**Selected:** `{current_model}` ({entry.provider if entry else 'unknown'})",
            f"**Chain:** `{format_chain(entry, current_model, config)}`",
            "**Readiness:** "
            + ", ".join(f"{k}={readiness.get(k)}" for k in ("codex", "grok", "openrouter", "ollama", "anthropic")),
        ]
        if action in ("usage", "status", "all"):
            lines.append("**Usage:**")
            lines.append(await subscription_usage_snapshot(config))
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @cmd_provider.autocomplete("action")
    async def cmd_provider_autocomplete(interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=a, value=a)
            for a in ("status", "usage")
            if current.lower() in a
        ]

    @tree.command(name="model-add", description="Register a new OpenRouter model in LiteLLM (anvil only)")
    @app_commands.describe(
        alias="Bernie alias, e.g. or-deepseek-v3",
        openrouter_slug="OpenRouter model ID, e.g. deepseek/deepseek-chat"
    )
    async def cmd_model_add(interaction: discord.Interaction, alias: str, openrouter_slug: str):
        await interaction.response.defer(ephemeral=True)
        if not _is_anvil(interaction):
            await interaction.followup.send("❌ Use #anvil for model changes.", ephemeral=True)
            return
        if not _is_admin_group(interaction):
            await interaction.followup.send("❌ Admin role required.", ephemeral=True)
            return
        from litellm_service import add_openrouter_model
        if not alias.startswith("or-"):
            alias = f"or-{alias}"
        ok, result = await add_openrouter_model(alias, openrouter_slug)
        if ok:
            # Save alias to config so autocomplete and /model list pick it up
            models = config.get("litellm_models", [])
            if alias not in models:
                models.append(alias)
                await update_config({"litellm_models": sorted(models)})
            await interaction.followup.send(
                f"✅ Registered `{alias}` → `{openrouter_slug}` (model_id: `{result}`)\n"
                f"Switch with `/model {alias}`",
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"❌ Failed to add model: {result}", ephemeral=True)


    @tree.command(name="model-remove", description="Remove a model from LiteLLM by model_id (anvil only)")
    @app_commands.describe(model_id="The model_id returned when the model was added")
    async def cmd_model_remove(interaction: discord.Interaction, model_id: str):
        await interaction.response.defer(ephemeral=True)
        if not _is_anvil(interaction):
            await interaction.followup.send("❌ Use #anvil for model changes.", ephemeral=True)
            return
        if not _is_admin_group(interaction):
            await interaction.followup.send("❌ Admin role required.", ephemeral=True)
            return
        from litellm_service import delete_model
        ok, msg = await delete_model(model_id)
        if ok:
            configured = config.get("litellm_models", [])
            if model_id in configured:
                await update_config({"litellm_models": [m for m in configured if m != model_id]})
            await interaction.followup.send(f"✅ Removed model `{model_id}` from LiteLLM.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Failed: {msg}", ephemeral=True)


    @tree.command(name="reload", description="Reload config.json without restarting (admin only, #anvil)")
    async def cmd_reload(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _is_admin_group(interaction):
            await interaction.followup.send("❌ Admin role required.", ephemeral=True)
            return
        if not _is_anvil(interaction):
            await interaction.followup.send("❌ Use #anvil for config changes.", ephemeral=True)
            return
        try:
            nonlocal cal, TZ
            timing_prior = timing_snapshot()
            reload_config()
            person_registry.load(config)
            _rebuild_person_legacy(config)
            try:
                from transit_service import refresh_zones

                await refresh_zones(force=True)
            except Exception as _tz_err:
                log.warning("cmd_reload: transit zone refresh failed (non-fatal): %s", _tz_err)
            if _container:
                from calendar_service import CalendarService
                _container.calendar = CalendarService(config)
            TZ = ZoneInfo(config["timezone"])
            m.TZ = TZ
            if cal is not None:
                m.cal = cal
            rescheduled = get_scheduler().sync_intervals_from_config(timing_prior)
            if rescheduled:
                log.info("cmd_reload: rescheduled BTS tasks: %s", ", ".join(rescheduled))

            try:
                from migrate_identity import seed_from_config
                id_result = await seed_from_config(config)
                log.info(f"cmd_reload: identity graph synced — {id_result}")
            except Exception as _id_err:
                log.warning(f"cmd_reload: identity graph sync failed (non-fatal): {_id_err}")

            if os.environ.get("ROLE", "monolith") in ("monolith", "cognition"):
                try:
                    from migrate_tasks_v32 import migrate as _migrate_tasks
                    _n = await _migrate_tasks()
                    log.info(f"cmd_reload: unified_tasks migration copied {_n} row(s)")
                except Exception as _mt_err:
                    log.warning(f"cmd_reload: unified_tasks migration failed (non-fatal): {_mt_err}")

            from ha_service import ha_service
            await ha_service.refresh_entities()

            price_count = get_database().reload_model_prices()

            from activity_aggregator import invalidate_cache as _inval_dash
            _inval_dash()

            from context import invalidate_behaviour_cache
            invalidate_behaviour_cache()

            await interaction.followup.send(
                f"✅ Config reloaded, identity graph synced + HA registry refreshed. "
                f"Model prices: {price_count} entries loaded.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)

    _EVAL_CAPTURE_OFF_NOTE = (
        "Also sets legacy eval.enabled=false. Nightly scoring turns off via that "
        "fallback unless eval.nightly.enabled was set explicitly (/nightly_eval)."
    )
    _EXPLICIT_NESTED_NOTE = (
        "Sets the nested key explicitly; /eval_mode on does not override a prior off."
    )


    @tree.command(name="eval_mode", description="[Admin] Toggle the shadow evaluation pipeline on/off")
    async def cmd_eval_mode(interaction: discord.Interaction):
        if not _is_admin_group(interaction):
            await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
            return
        if not _is_anvil(interaction):
            await interaction.response.send_message("❌ Use #anvil for config changes.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        from eval.policy import resolve_eval_policy
        policy = resolve_eval_policy(config)
        new_state = not policy.capture_enabled
        from config import update_config
        try:
            await update_config({"eval": {"capture": {"enabled": new_state}, "enabled": new_state}})
        except Exception as exc:
            log.error("eval_mode update_config failed: %s", exc, exc_info=True)
            await interaction.followup.send("❌ Failed to save eval setting. Check bot logs.", ephemeral=True)
            return
        emoji = "✅" if new_state else "⏸️"
        note = f" {_EVAL_CAPTURE_OFF_NOTE}" if not new_state else ""
        await interaction.followup.send(
            f"{emoji} Shadow eval capture **{'enabled' if new_state else 'disabled'}**.{note}",
            ephemeral=True,
        )

    @tree.command(name="nightly_eval", description="[Admin] Toggle nightly eval scoring")
    async def cmd_nightly_eval(interaction: discord.Interaction):
        if not _is_admin_group(interaction) or not _is_anvil(interaction):
            await interaction.response.send_message("❌ Admin in #anvil only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        from eval.policy import resolve_eval_policy
        policy = resolve_eval_policy(config)
        new_state = not policy.nightly_enabled
        from config import update_config
        try:
            await update_config({"eval": {"nightly": {"enabled": new_state}}})
        except Exception as exc:
            log.error("nightly_eval update_config failed: %s", exc, exc_info=True)
            await interaction.followup.send("❌ Failed to save eval setting. Check bot logs.", ephemeral=True)
            return
        await interaction.followup.send(
            f"{'✅' if new_state else '⏸️'} Nightly eval scoring **{'enabled' if new_state else 'disabled'}**. "
            f"{_EXPLICIT_NESTED_NOTE}",
            ephemeral=True,
        )

    @tree.command(name="harness_mode", description="[Admin] Toggle shadow eval triplet harness")
    async def cmd_harness_mode(interaction: discord.Interaction):
        if not _is_admin_group(interaction) or not _is_anvil(interaction):
            await interaction.response.send_message("❌ Admin in #anvil only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        from eval.policy import resolve_eval_policy
        policy = resolve_eval_policy(config)
        new_state = not policy.harness_enabled
        from config import update_config
        try:
            await update_config({"eval": {"harness": {"enabled": new_state}}})
        except Exception as exc:
            log.error("harness_mode update_config failed: %s", exc, exc_info=True)
            await interaction.followup.send("❌ Failed to save eval setting. Check bot logs.", ephemeral=True)
            return
        await interaction.followup.send(
            f"{'✅' if new_state else '⏸️'} Shadow harness **{'enabled' if new_state else 'disabled'}**. "
            f"{_EXPLICIT_NESTED_NOTE}",
            ephemeral=True,
        )

    @tree.command(name="hitl_mode", description="[Admin] Toggle shadow eval HITL DMs")
    async def cmd_hitl_mode(interaction: discord.Interaction):
        if not _is_admin_group(interaction) or not _is_anvil(interaction):
            await interaction.response.send_message("❌ Admin in #anvil only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        from eval.policy import resolve_eval_policy
        policy = resolve_eval_policy(config)
        new_state = not policy.hitl
        from config import update_config
        try:
            await update_config({"eval": {"nightly": {"hitl": new_state}}})
        except Exception as exc:
            log.error("hitl_mode update_config failed: %s", exc, exc_info=True)
            await interaction.followup.send("❌ Failed to save eval setting. Check bot logs.", ephemeral=True)
            return
        await interaction.followup.send(
            f"{'✅' if new_state else '⏸️'} Shadow HITL DMs **{'enabled' if new_state else 'disabled'}**.", ephemeral=True
        )

    @tree.command(name="eval_scoring", description="[Admin] Toggle shadow eval scoring modes (pair/triplet/both)")
    @app_commands.describe(mode="Scoring mode")
    @app_commands.choices(mode=[
        app_commands.Choice(name="both", value="both"),
        app_commands.Choice(name="pair", value="pair"),
        app_commands.Choice(name="triplet", value="triplet"),
        app_commands.Choice(name="none", value="none"),
    ])
    async def cmd_eval_scoring(interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if not _is_admin_group(interaction) or not _is_anvil(interaction):
            await interaction.response.send_message("❌ Admin in #anvil only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        val = mode.value
        if val == "none":
            score_pairs = False
            score_triplets = False
        else:
            score_pairs = val in ("both", "pair")
            score_triplets = val in ("both", "triplet")
        from config import update_config
        try:
            await update_config({"eval": {"nightly": {"score_pairs": score_pairs, "score_triplets": score_triplets}}})
        except Exception as exc:
            log.error("eval_scoring update_config failed: %s", exc, exc_info=True)
            await interaction.followup.send("❌ Failed to save eval setting. Check bot logs.", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Eval scoring set to pairs=**{score_pairs}**, triplets=**{score_triplets}**.", ephemeral=True
        )

    def _all_model_choices(current: str) -> list[app_commands.Choice]:
        """Build autocomplete choices from all configured models, filtered by current input."""
        candidates = (
            ["off"] +
            config.get("anthropic_models", []) +
            config.get("litellm_models", []) +
            config.get("ollama_models", [])
        )
        lc = current.lower()
        filtered = [m for m in candidates if lc in m.lower()][:25]
        return [app_commands.Choice(name=m, value=m) for m in filtered]

    @tree.command(name="shadow_mode", description="[Admin] Set shadow comparison model or 'off' to disable")
    @app_commands.describe(model="Model to shadow with (type to filter), or 'off' to disable")
    async def cmd_shadow_mode(interaction: discord.Interaction, model: str):
        if not _is_admin_group(interaction):
            await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
            return
        if not _is_anvil(interaction):
            await interaction.response.send_message("❌ Use #anvil for config changes.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        from config import update_config
        try:
            if model.lower() == "off":
                await update_config({"eval": {"shadow_model": None}})
                await interaction.followup.send("⏹️ Shadow model **disabled**.", ephemeral=True)
            else:
                await update_config({"eval": {"shadow_model": model}})
                await interaction.followup.send(
                    f"🔀 Shadow model set to **{model}**.", ephemeral=True
                )
        except Exception as exc:
            log.error("shadow_mode update_config failed: %s", exc, exc_info=True)
            await interaction.followup.send("❌ Failed to save shadow model. Check bot logs.", ephemeral=True)

    @cmd_shadow_mode.autocomplete("model")
    async def shadow_mode_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice]:
        return _all_model_choices(current)


    @tree.command(name="worker_model", description="[Admin] Set the model used for background deferred tasks")
    @app_commands.describe(model="Model for background worker (type to filter)")
    async def cmd_worker_model(interaction: discord.Interaction, model: str):
        if not _is_admin_group(interaction):
            await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
            return
        if not _is_anvil(interaction):
            await interaction.response.send_message("❌ Use #anvil for config changes.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        from config import update_config
        try:
            await update_config({"eval": {"worker_model": model}})
        except Exception as exc:
            log.error("worker_model update_config failed: %s", exc, exc_info=True)
            await interaction.followup.send("❌ Failed to save worker model. Check bot logs.", ephemeral=True)
            return
        await interaction.followup.send(
            f"🔧 Background worker model set to **{model}**.", ephemeral=True
        )

    @cmd_worker_model.autocomplete("model")
    async def worker_model_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice]:
        return _all_model_choices(current)


    @tree.command(name="mode", description="[Admin] View or switch Bernie's operational mode (anvil only)")
    @app_commands.describe(mode="Mode slug (chef, tutor, debug, ops, wind-down, etc.) or 'auto'/'clear' to reset")
    async def cmd_mode(interaction: discord.Interaction, mode: str | None = None):
        if not _is_admin_group(interaction):
            await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
            return
        if not _is_anvil(interaction):
            await interaction.response.send_message("❌ Use #anvil for mode changes.", ephemeral=True)
            return

        load_all_modes()

        if mode is None:
            # Just show current state
            current = get_mode_override()
            if current:
                return await interaction.response.send_message(
                    f"Current mode override: **{current}**\n"
                    f"Use `/mode auto` or `/mode clear` to restore dynamic detection.",
                    ephemeral=True,
                )
            else:
                return await interaction.response.send_message(
                    "No manual mode override is active. Bernie is using normal auto-detection.",
                    ephemeral=True,
                )

        desired = mode.strip().lower()

        if desired in ("auto", "clear", "none", "default"):
            set_mode_override(None)
            await interaction.response.send_message(
                "✅ Mode override cleared. Bernie will now use normal auto-detection (channel + keywords + quiet hours).",
                ephemeral=True,
            )
            return

        if not get_mode(desired):
            valid = ", ".join(sorted(load_all_modes().keys()))
            await interaction.response.send_message(
                f"❌ Unknown mode '{desired}'. Valid: {valid} (or use 'auto'/'clear')",
                ephemeral=True,
            )
            return

        set_mode_override(desired)
        await interaction.response.send_message(
            f"✅ Switched to **{desired}** mode. This will stay active until you clear it or restart.",
            ephemeral=True,
        )


    @cmd_mode.autocomplete('mode')
    async def cmd_mode_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        valid_modes = list(load_all_modes().keys()) + ["auto", "clear"]
        return [
            app_commands.Choice(name=m, value=m)
            for m in valid_modes if current.lower() in m.lower()
        ][:25]


    @tree.command(name="eval_status", description="[Admin] Show shadow eval pipeline status and today's call counts")
    async def cmd_eval_status(interaction: discord.Interaction):
        if not _is_admin_group(interaction):
            await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
            return
        if not _is_anvil(interaction):
            await interaction.response.send_message("❌ Use #anvil.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        from eval.policy import resolve_eval_policy, harness_active
        policy = resolve_eval_policy(config)
        from eval_service import DEFAULT_JUDGE_MODEL as _default_judge
        eval_model = policy.eval_model or f"default ({_default_judge})"
        worker_model = config.get("eval", {}).get("worker_model") or "default"
        daily_cap = policy.shadow_daily_cap
        executor_cfg = config.get("executor", {})
        legacy_harness = executor_cfg.get("shadow_harness_enabled")
        legacy_defer = executor_cfg.get("shadow_defer_s")
        today_str = datetime.now(TZ).strftime("%Y-%m-%d")
        calls_today = await get_database().get_shadow_call_count_today(today_str)
        unscored = await get_database().get_unscored_shadow_calls(today_str)
        lines = [
            f"**Shadow Eval Status**",
            f"Capture Enabled: **{'✅ yes' if policy.capture_enabled else '⏸️ no'}**",
            f"Harness Enabled: **{'✅ yes' if policy.harness_enabled else '⏸️ no'}** (Active right now: {harness_active(policy)})",
            f"Nightly Enabled: **{'✅ yes' if policy.nightly_enabled else '⏸️ no'}**",
            f"Score pairs: **{'✅ yes' if policy.score_pairs else '⏸️ no'}**",
            f"Score triplets: **{'✅ yes' if policy.score_triplets else '⏸️ no'}**",
            f"HITL DMs: **{'✅ yes' if policy.hitl else '⏸️ no'}**",
            f"Ungrounded audit: **{'✅ yes' if policy.ungrounded_audit else '⏸️ no'}**",
            f"Shadow model: `{policy.shadow_model or 'none'}`",
            f"Worker model: `{worker_model}`",
            f"Judge model: `{eval_model}`",
            f"Legacy executor.shadow_harness_enabled: `{legacy_harness}`",
            f"Legacy executor.shadow_defer_s: `{legacy_defer}`",
            f"Daily cap: {calls_today}/{daily_cap}",
            f"Unscored (before today): {len(unscored)}",
        ]
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # Keep strong references to background tasks to prevent garbage collection
    _background_tasks = set()

    @tree.command(name="audit", description="Manually trigger the nightly system health report (admin only)")
    async def cmd_audit(interaction: discord.Interaction):
        # Use the bulletproof person_registry for identity resolution
        person_id = person_registry.resolve(interaction.user.id)
        person = person_registry.get(person_id) if person_id else None

        if not person or person.get("role") != "admin":
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        await interaction.response.send_message("🔍 Starting system audit... I will email you the report when complete.", ephemeral=True)
        try:
            from watchman import get_watchman
            wm = get_watchman()
            # Run in background with strong reference to avoid GC
            task = asyncio.create_task(wm.run_and_email())
            _background_tasks.add(task)
            def _on_audit_done(t):
                _background_tasks.discard(t)
                try:
                    t.result()
                except Exception as e:
                    log.error(f"Audit task failed with exception: {e}", exc_info=True)
            task.add_done_callback(_on_audit_done)
        except Exception as e:
            log.error(f"Manual audit failed: {e}")
            await interaction.followup.send(f"❌ Audit failed to start: {e}", ephemeral=True)


    @tree.command(name="network", description="Check homelab server IPs and recent network events (admin only)")
    @app_commands.describe(
        refresh="Poll UniFi now for fresh data (default: show last snapshot)",
        event_hours="Hours of event history to include (default 24)",
    )
    async def cmd_network(interaction: discord.Interaction, refresh: bool = False, event_hours: app_commands.Range[int, 1, 168] = 24):
        person_id = person_registry.resolve(interaction.user.id)
        person = person_registry.get(person_id) if person_id else None

        if not person or person.get("role") != "admin":
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            from network_watchman import build_network_status
            report = await build_network_status(refresh=refresh, event_hours=event_hours)
            from discord_chunk import send_chunked

            await send_chunked(interaction.followup, report, is_dm=True, ephemeral=True)
        except Exception as e:
            log.error(f"Manual network check failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Network check failed: {e}", ephemeral=True)


    # ─────────────────────────────────────────────────────────────────────────────

    @tree.command(name="email", description="Send an email via Bernie (anvil only)")
    @app_commands.describe(to="Recipient email address", subject="Subject line", body="Email body")
    async def cmd_email(interaction: discord.Interaction, to: str, subject: str, body: str):
        if not _is_anvil(interaction):
            await interaction.response.send_message("❌ This command only works in #anvil.", ephemeral=True)
            return
        if not _is_admin_group(interaction):
            await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            from email_service import send
            requester_id = (
                person_registry.resolve(interaction.user.id)
                or str(interaction.user.id)
            )
            msg_id = await send(
                to, subject, body,
                requester_id=requester_id,
                requester_role="admin",
                config=config,
            )
            await interaction.followup.send(f"✅ Sent. Message ID: `{msg_id}`", ephemeral=True)
        except Exception as e:
            log.error(f"/email failed: {e}")
            await interaction.followup.send(f"❌ Failed to send: {e}", ephemeral=True)
