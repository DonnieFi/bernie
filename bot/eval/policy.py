import logging
from dataclasses import dataclass
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

@dataclass(frozen=True)
class EvalPolicy:
    shadow_model: str | None
    eval_model: str | None
    shadow_daily_cap: int
    max_scored_per_night: int
    
    capture_enabled: bool
    defer_s: int
    shed_on_backpressure: bool
    
    harness_enabled: bool
    block_peak_hours: bool
    peak_start_hour: int
    peak_end_hour: int
    timezone: tzinfo | None
    
    nightly_enabled: bool
    score_pairs: bool
    score_triplets: bool
    hitl: bool
    ungrounded_audit: bool


def resolve_eval_policy(config: dict) -> EvalPolicy:
    eval_cfg = config.get("eval", {})
    executor_cfg = config.get("executor", {})
    
    # Base eval fallback
    base_enabled = eval_cfg.get("enabled", False)
    
    # Capture
    capture_cfg = eval_cfg.get("capture", {})
    capture_enabled = capture_cfg.get("enabled", base_enabled)
    defer_s = capture_cfg.get("defer_s", executor_cfg.get("shadow_defer_s", 2))
    shed = capture_cfg.get(
        "shed_on_backpressure",
        executor_cfg.get("llm_queue_shed_shadow_first", True)
    )
    
    # Harness
    harness_cfg = eval_cfg.get("harness", {})
    harness_enabled = harness_cfg.get(
        "enabled",
        executor_cfg.get("shadow_harness_enabled", False)
    )
    block_peak_hours = harness_cfg.get("block_peak_hours", True)
    peak_start = harness_cfg.get("peak_start_hour", 15)
    peak_end = harness_cfg.get("peak_end_hour", 21)
    
    # Timezone resolution
    tz_str = config.get("timezone", "America/Halifax")
    tz = None
    try:
        tz = ZoneInfo(tz_str)
    except Exception as e:
        log.warning("resolve_eval_policy: invalid timezone %r: %s", tz_str, e)
        
    # Nightly
    nightly_cfg = eval_cfg.get("nightly", {})
    nightly_enabled = nightly_cfg.get("enabled", base_enabled)
    score_pairs = nightly_cfg.get("score_pairs", True)
    score_triplets = nightly_cfg.get("score_triplets", True)
    hitl = nightly_cfg.get("hitl", nightly_enabled)
    ungrounded_audit = nightly_cfg.get("ungrounded_audit", True)
    
    return EvalPolicy(
        shadow_model=eval_cfg.get("shadow_model"),
        eval_model=eval_cfg.get("eval_model"),
        shadow_daily_cap=eval_cfg.get("shadow_daily_cap", 10),
        max_scored_per_night=eval_cfg.get("max_scored_per_night", 50),
        capture_enabled=capture_enabled,
        defer_s=defer_s,
        shed_on_backpressure=shed,
        harness_enabled=harness_enabled,
        block_peak_hours=block_peak_hours,
        peak_start_hour=peak_start,
        peak_end_hour=peak_end,
        timezone=tz,
        nightly_enabled=nightly_enabled,
        score_pairs=score_pairs,
        score_triplets=score_triplets,
        hitl=hitl,
        ungrounded_audit=ungrounded_audit,
    )


def _in_peak_window(hour: int, start: int, end: int) -> bool:
    """True when local hour is inside [start, end). Supports wrap past midnight (e.g. 22→7)."""
    if start < end:
        return start <= hour < end
    if start > end:
        return hour >= start or hour < end
    return False


def harness_active(policy: EvalPolicy) -> bool:
    if not policy.harness_enabled:
        return False
        
    if policy.block_peak_hours:
        if policy.timezone:
            try:
                local_time = datetime.now(policy.timezone)
                if _in_peak_window(
                    local_time.hour,
                    policy.peak_start_hour,
                    policy.peak_end_hour,
                ):
                    return False
            except Exception as e:
                log.warning("harness_active: error evaluating peak hours: %s", e)
        else:
            # If timezone is invalid/missing, we gracefully skip peak check
            pass
            
    return True
