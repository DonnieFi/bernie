"""Validated slices of config.json on load and after disk merge."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class SubscriptionModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["codex", "grok"]
    model: str
    capabilities: list[str]
    openrouter_fallback_model: str
    litellm_alias: str | None = None
    enabled: bool = False

    @field_validator("model", "openrouter_fallback_model")
    @classmethod
    def non_empty_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-empty")
        return value

    @field_validator("litellm_alias")
    @classmethod
    def litellm_alias_format(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        value = str(value).strip()
        if not value.startswith("or-"):
            raise ValueError("litellm_alias must start with or-")
        return value

    @field_validator("capabilities")
    @classmethod
    def non_empty_capabilities(cls, value: list[str]) -> list[str]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("must contain non-empty capability names")
        return value


class FamilyMemberEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    canonical_id: str = ""
    discord_id: int | str | None = None
    email: str | None = None


class ExecutorConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    chat: str = "native"
    workers: str = "native"
    max_steps: int = Field(default=5, ge=1)
    max_tokens: int = Field(default=4096, ge=1)
    chat_routing: str = "intent"


class EvalCaptureConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    defer_s: int = Field(default=2, ge=0)
    shed_on_backpressure: bool = True

class EvalHarnessConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    block_peak_hours: bool = True
    # Same-day window when start < end; overnight when start > end (e.g. 22→7).
    peak_start_hour: int = Field(default=15, ge=0, le=23)
    peak_end_hour: int = Field(default=21, ge=0, le=23)

class EvalNightlyConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    score_pairs: bool = True
    score_triplets: bool = True
    hitl: bool = True
    ungrounded_audit: bool = True


class EvalConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    shadow_model: str | None = None
    shadow_daily_cap: int = Field(default=10, ge=0)
    max_scored_per_night: int = Field(default=50, ge=0)
    eval_model: str = "claude-haiku-4-5-20251001"
    worker_model: str = "claude-haiku-4-5-20251001"
    capture: EvalCaptureConfig = Field(default_factory=EvalCaptureConfig)
    harness: EvalHarnessConfig = Field(default_factory=EvalHarnessConfig)
    nightly: EvalNightlyConfig = Field(default_factory=EvalNightlyConfig)


class ConfigCore(BaseModel):
    """Required top-level keys and shapes — does not model the full dynamic config."""

    model_config = ConfigDict(extra="allow")

    timezone: str
    schedule_channel_id: int | str
    guild_id: int | str
    poll_interval_minutes: int = Field(ge=1)
    family_members: dict[str, FamilyMemberEntry | dict[str, Any]]
    executor: ExecutorConfig | None = None
    eval: EvalConfig | None = None
    subscription_models: list[SubscriptionModelConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_subscription_models(self) -> "ConfigCore":
        models = [entry.model for entry in self.subscription_models]
        if len(models) != len(set(models)):
            raise ValueError("subscription model ids must be unique")
        return self


def validate_config_core(cfg: dict) -> None:
    """Raise ValueError with a clear message if critical config is invalid."""
    try:
        validated = ConfigCore.model_validate(cfg)
        subscription_ids = {entry.model for entry in validated.subscription_models}
        legacy_ids = set(cfg.get("anthropic_models") or [])
        legacy_ids.update(cfg.get("litellm_models") or [])
        legacy_ids.update(cfg.get("ollama_models") or [])
        overlap = sorted(subscription_ids & legacy_ids)
        if overlap:
            raise ValueError(
                "subscription model ids overlap legacy model pools: " + ", ".join(overlap)
            )
    except ValidationError as e:
        raise ValueError(f"Invalid config.json: {e}") from e


# --- Shared doctor / policy findings (family-bot-mu2.3 + 5hy.1) ---------------


def validate_config(cfg: dict) -> list[dict[str, str]]:
    """Return structured findings for config hygiene (shared by CORS policy + config_doctor).

    Each finding: {severity: error|warn|info, code: str, message: str}
    Does not raise — callers decide fail-closed vs report-only.
    """
    findings: list[dict[str, str]] = []
    if not isinstance(cfg, dict):
        findings.append({
            "severity": "error",
            "code": "config_not_object",
            "message": "config root must be a JSON object",
        })
        return findings

    try:
        validate_config_core(cfg)
    except ValueError as e:
        findings.append({
            "severity": "error",
            "code": "config_core",
            "message": str(e),
        })

    cors = cfg.get("cors_origins", None)
    if cors is None:
        findings.append({
            "severity": "info",
            "code": "cors_default_empty",
            "message": "cors_origins unset — API defaults to empty allowlist (homelab-first)",
        })
    elif cors == "*" or cors == ["*"]:
        findings.append({
            "severity": "error",
            "code": "cors_wildcard",
            "message": (
                "cors_origins is '*' — runtime refuses open CORS (empty allowlist); "
                "set an explicit origin list for reverse-proxy/LAN hosts"
            ),
        })
    elif isinstance(cors, list) and not cors:
        findings.append({
            "severity": "info",
            "code": "cors_empty",
            "message": "cors_origins is empty — browser cross-origin blocked (OK for same-origin dashboard)",
        })

    token = cfg.get("bernie_api_token")
    if token in (None, "", "changeme", "YOUR_TOKEN", "replace-me"):
        findings.append({
            "severity": "warn",
            "code": "api_token_placeholder",
            "message": "bernie_api_token looks unset or placeholder",
        })

    try:
        from model_slug_map import validate_subscription_slug_alignment

        for msg in validate_subscription_slug_alignment(cfg):
            findings.append({
                "severity": "warn",
                "code": "slug_alignment",
                "message": msg,
            })
    except Exception:
        pass

    return findings


def cors_origins_refused(cfg: dict) -> bool:
    """True when config uses open CORS that policy should refuse (mu2.3)."""
    cors = cfg.get("cors_origins")
    return cors == "*" or cors == ["*"]
