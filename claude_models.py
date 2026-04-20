#!/usr/bin/env python3
"""
Helpers for Claude model normalization, display labels, and pricing estimates.

Pricing reference:
https://platform.claude.com/docs/en/about-claude/pricing
Checked on 2026-04-01.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

PRICING_SOURCE_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
PRICING_CHECKED_AT = "2026-04-01"


@dataclass(frozen=True)
class ModelPricing:
    label: str
    input_per_mtok: float
    cache_write_5m_per_mtok: float
    cache_write_1h_per_mtok: float
    cache_read_per_mtok: float
    output_per_mtok: float


MODEL_PRICING: dict[str, ModelPricing] = {
    "claude-opus-4-7": ModelPricing("Claude Opus 4.7", 5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-6": ModelPricing("Claude Opus 4.6", 5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-5": ModelPricing("Claude Opus 4.5", 5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-1": ModelPricing("Claude Opus 4.1", 15.0, 18.75, 30.0, 1.50, 75.0),
    "claude-opus-4": ModelPricing("Claude Opus 4", 15.0, 18.75, 30.0, 1.50, 75.0),
    "claude-sonnet-4-6": ModelPricing("Claude Sonnet 4.6", 3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-4-5": ModelPricing("Claude Sonnet 4.5", 3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-4": ModelPricing("Claude Sonnet 4", 3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-3-7": ModelPricing("Claude Sonnet 3.7", 3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-haiku-4-5": ModelPricing("Claude Haiku 4.5", 1.0, 1.25, 2.0, 0.10, 5.0),
    "claude-haiku-3-5": ModelPricing("Claude Haiku 3.5", 0.8, 1.0, 1.6, 0.08, 4.0),
    "claude-haiku-3": ModelPricing("Claude Haiku 3", 0.25, 0.30, 0.50, 0.03, 1.25),
}

MODEL_ALIASES = {
    "claude-opus-4-7-latest": "claude-opus-4-7",
    "claude-opus-4-1-latest": "claude-opus-4-1",
    "claude-sonnet-4-5-latest": "claude-sonnet-4-5",
    "claude-sonnet-4-6-latest": "claude-sonnet-4-6",
    "claude-haiku-4-5-latest": "claude-haiku-4-5",
}


def normalize_model_name(model: str | None) -> str:
    """Normalize runtime model identifiers to a stable pricing/display key."""
    if not model:
        return ""

    normalized = model.strip().lower()
    normalized = MODEL_ALIASES.get(normalized, normalized)

    if normalized == "<synthetic>":
        return normalized

    normalized = re.sub(r"-20\d{6}$", "", normalized)
    normalized = re.sub(r"-latest$", "", normalized)
    return MODEL_ALIASES.get(normalized, normalized)


def model_label(model: str | None) -> str:
    """Return a friendly label for UI surfaces."""
    normalized = normalize_model_name(model)
    if not normalized:
        return "Unknown model"
    pricing = MODEL_PRICING.get(normalized)
    if pricing:
        return pricing.label
    if normalized == "<synthetic>":
        return "Synthetic system message"

    pretty = normalized.replace("claude-", "Claude ").replace("-", " ")
    return pretty.title()


def get_model_pricing(model: str | None) -> ModelPricing | None:
    return MODEL_PRICING.get(normalize_model_name(model))


def estimate_message_cost_usd(model: str | None, usage: dict | None) -> tuple[float, bool]:
    """Estimate message cost from Claude API pricing.

    Returns (cost_usd, was_priced). Unknown models return (0.0, False).
    """
    if not usage:
        return 0.0, False

    pricing = get_model_pricing(model)
    if pricing is None:
        return 0.0, False

    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cache_creation_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)

    cache_creation = usage.get("cache_creation") or {}
    cache_write_5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
    cache_write_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)

    if cache_write_5m == 0 and cache_write_1h == 0:
        cache_write_1h = cache_creation_tokens
    else:
        assigned = cache_write_5m + cache_write_1h
        if assigned < cache_creation_tokens:
            cache_write_1h += cache_creation_tokens - assigned

    total = (
        (input_tokens * pricing.input_per_mtok)
        + (cache_write_5m * pricing.cache_write_5m_per_mtok)
        + (cache_write_1h * pricing.cache_write_1h_per_mtok)
        + (cache_read_tokens * pricing.cache_read_per_mtok)
        + (output_tokens * pricing.output_per_mtok)
    ) / 1_000_000

    return round(total, 8), True


def summarize_models(model_totals: dict[str, int]) -> dict:
    """Build conversation-level model summary metadata."""
    normalized = {
        normalize_model_name(model): int(tokens)
        for model, tokens in model_totals.items()
        if int(tokens) > 0
    }
    normalized = {model: tokens for model, tokens in normalized.items() if model}
    ranked = sorted(normalized.items(), key=lambda item: item[1], reverse=True)

    primary_model = ranked[0][0] if ranked else ""
    model_count = len(ranked)

    if not ranked:
        display = "Unknown model"
    elif len(ranked) == 1:
        display = model_label(primary_model)
    else:
        display = f"{model_label(primary_model)} +{len(ranked) - 1}"

    payload = [
        {"model": model, "label": model_label(model), "tokens": tokens}
        for model, tokens in ranked
    ]

    return {
        "primary_model": primary_model,
        "model_count": model_count,
        "model_display": display,
        "models_json": json.dumps(payload),
    }
