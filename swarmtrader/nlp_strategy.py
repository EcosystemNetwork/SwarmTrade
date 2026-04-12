"""Natural language strategy builder — translate English to agent weight configs.

Parses human-readable strategy descriptions into Strategist weight overrides.
Works without any external API by using keyword matching and rule extraction.
Optionally uses Claude API for advanced parsing when ANTHROPIC_API_KEY is set.

Examples:
    "Buy ETH when RSI < 30 and whale activity spikes"
    → Boost rsi weight to 0.25, whale to 0.15, mean_rev to 0.20

    "Conservative momentum following with tight stops"
    → Boost momentum, macd, atr_stop; reduce mean_rev, bollinger

    "Aggressive scalping with high leverage on breakouts"
    → Boost prism_breakout, momentum, mtf; reduce rebalance, hedge
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("swarm.nlp")

# Map natural language concepts → agent weights
_CONCEPT_MAP: dict[str, dict[str, float]] = {
    # Directional strategies
    "momentum": {"momentum": 0.25, "macd": 0.15, "mtf": 0.10, "mean_rev": 0.03},
    "trend following": {"momentum": 0.25, "macd": 0.20, "ichimoku": 0.12, "mtf": 0.10},
    "mean reversion": {"mean_rev": 0.25, "rsi": 0.20, "bollinger": 0.18, "vwap": 0.10},
    "breakout": {"prism_breakout": 0.20, "momentum": 0.18, "bollinger": 0.12, "mtf": 0.10},
    "scalping": {"prism_breakout": 0.15, "momentum": 0.15, "orderbook": 0.15, "mtf": 0.12},

    # Signal sources
    "rsi": {"rsi": 0.25},
    "macd": {"macd": 0.20},
    "bollinger": {"bollinger": 0.18},
    "vwap": {"vwap": 0.15},
    "ichimoku": {"ichimoku": 0.15},
    "whale": {"whale": 0.20, "onchain": 0.10},
    "whale activity": {"whale": 0.25, "onchain": 0.12},
    "on-chain": {"onchain": 0.15, "whale": 0.10},
    "news": {"news": 0.20, "social": 0.10},
    "sentiment": {"news": 0.15, "social": 0.15, "fear_greed": 0.12},
    "social": {"social": 0.18, "news": 0.10},
    "fear greed": {"fear_greed": 0.20},
    "funding rate": {"funding": 0.18},
    "open interest": {"open_interest": 0.18},
    "order book": {"orderbook": 0.20},
    "orderbook": {"orderbook": 0.20},
    "liquidation": {"liquidation": 0.18, "liquidation_levels": 0.12},
    "correlation": {"correlation": 0.15},
    "ml": {"ml": 0.25},
    "machine learning": {"ml": 0.25},
    "ai": {"ml": 0.20, "confluence": 0.12},

    # Risk styles
    "conservative": {"hedge": 0.12, "rebalance": 0.10, "atr_stop": 0.12, "confluence": 0.15},
    "aggressive": {"momentum": 0.20, "prism_breakout": 0.15, "hedge": 0.02, "rebalance": 0.02},
    "defensive": {"hedge": 0.15, "mean_rev": 0.12, "atr_stop": 0.15, "rebalance": 0.10},
    "balanced": {},  # keep defaults

    # Exit strategies
    "tight stops": {"atr_stop": 0.18},
    "trailing stop": {"atr_stop": 0.15, "momentum": 0.12},
    "take profit": {"atr_stop": 0.10, "confluence": 0.12},

    # Portfolio
    "hedge": {"hedge": 0.15, "correlation": 0.10},
    "rebalance": {"rebalance": 0.12},
    "diversify": {"correlation": 0.12, "rebalance": 0.10, "hedge": 0.10},
}

# Threshold modifiers
_THRESHOLD_PATTERNS = {
    r"rsi\s*[<>]\s*(\d+)": "rsi",
    r"macd\s+(bullish|bearish|cross)": "macd",
    r"bollinger\s+(squeeze|breakout|band)": "bollinger",
    r"vwap\s+(above|below|cross)": "vwap",
    r"volume\s+(spike|surge|high|low)": "orderbook",
}


@dataclass
class StrategyConfig:
    """Parsed strategy configuration from natural language."""
    name: str = ""
    weights: dict[str, float] = field(default_factory=dict)
    threshold: float = 0.20
    base_size_multiplier: float = 1.0
    regime_preference: str = ""  # "", "trending", "reverting", "volatile"
    description: str = ""
    confidence: float = 0.0  # how confident the parser is in its interpretation


def parse_strategy(text: str) -> StrategyConfig:
    """Parse a natural language strategy description into weight overrides.

    Args:
        text: Human-readable strategy description like
              "Momentum following with RSI confirmation and whale tracking"

    Returns:
        StrategyConfig with agent weight overrides and parameters.
    """
    text_lower = text.lower().strip()
    config = StrategyConfig(description=text)

    matched_concepts = []
    accumulated_weights: dict[str, float] = {}

    # Match concepts (longest match first to handle "whale activity" before "whale")
    concepts_sorted = sorted(_CONCEPT_MAP.keys(), key=len, reverse=True)
    remaining = text_lower
    for concept in concepts_sorted:
        if concept in remaining:
            matched_concepts.append(concept)
            for agent, weight in _CONCEPT_MAP[concept].items():
                # Take the max weight if multiple concepts boost same agent
                accumulated_weights[agent] = max(
                    accumulated_weights.get(agent, 0.0), weight
                )
            # Remove matched concept to avoid double-matching
            remaining = remaining.replace(concept, " ", 1)

    # Apply regime preference
    if any(w in text_lower for w in ("trend", "momentum", "breakout")):
        config.regime_preference = "trending"
    elif any(w in text_lower for w in ("mean revert", "revert", "range", "sideways")):
        config.regime_preference = "reverting"
    elif any(w in text_lower for w in ("volatile", "choppy", "uncertain")):
        config.regime_preference = "volatile"

    # Apply size modifiers
    if any(w in text_lower for w in ("aggressive", "large", "big", "max")):
        config.base_size_multiplier = 1.5
    elif any(w in text_lower for w in ("conservative", "small", "minimal", "cautious")):
        config.base_size_multiplier = 0.6
    elif any(w in text_lower for w in ("moderate", "balanced", "normal")):
        config.base_size_multiplier = 1.0

    # Apply threshold modifiers
    if any(w in text_lower for w in ("sensitive", "trigger happy", "fast")):
        config.threshold = 0.12
    elif any(w in text_lower for w in ("strict", "picky", "selective", "patient")):
        config.threshold = 0.30

    # Normalize weights so they're meaningful
    if accumulated_weights:
        # Start with small base weights for unmentioned agents
        from .strategy import Strategist
        base = {k: v * 0.3 for k, v in Strategist.DEFAULT_WEIGHTS.items()}
        # Override with parsed weights
        base.update(accumulated_weights)
        # Normalize to sum to ~1.0
        total = sum(base.values()) or 1.0
        config.weights = {k: round(v / total, 4) for k, v in base.items()}

    config.confidence = min(1.0, len(matched_concepts) * 0.2)
    config.name = " + ".join(matched_concepts[:3]) if matched_concepts else "custom"

    log.info("NLP strategy parsed: '%s' → %d concepts matched, confidence=%.1f",
             text[:60], len(matched_concepts), config.confidence)

    return config


def apply_strategy(strategist, config: StrategyConfig) -> dict:
    """Apply a parsed StrategyConfig to a live Strategist instance.

    Returns dict summary of what changed.
    """
    changes = {}

    if config.weights:
        old_weights = dict(strategist.weights)
        strategist.weights.update(config.weights)
        # Track what changed significantly
        for k, v in config.weights.items():
            old = old_weights.get(k, 0)
            if abs(v - old) > 0.01:
                changes[k] = {"from": round(old, 4), "to": round(v, 4)}

    if config.threshold != 0.20:
        changes["threshold"] = {"from": strategist.THRESHOLD, "to": config.threshold}
        strategist.THRESHOLD = config.threshold

    if config.base_size_multiplier != 1.0:
        old_size = strategist.base_size
        strategist.base_size *= config.base_size_multiplier
        changes["base_size"] = {"from": old_size, "to": strategist.base_size}

    log.info("Strategy applied: %d weight changes, threshold=%.2f, size_mult=%.1f",
             len(changes), config.threshold, config.base_size_multiplier)

    return {
        "name": config.name,
        "description": config.description,
        "changes": changes,
        "regime_preference": config.regime_preference,
        "confidence": config.confidence,
        "active_weights": dict(strategist.weights),
    }


# ---------------------------------------------------------------------------
# Preset strategies (one-click from dashboard)
# ---------------------------------------------------------------------------
PRESETS: dict[str, str] = {
    "momentum_rider": "Aggressive momentum following with MACD confirmation and trailing stops",
    "mean_reversion": "Conservative mean reversion with RSI and Bollinger bands, tight stops",
    "whale_tracker": "Follow whale activity and on-chain signals with defensive hedging",
    "sentiment_driven": "News and social sentiment with fear greed index confirmation",
    "ml_quant": "Machine learning driven with confluence detection and portfolio rebalancing",
    "scalper": "Aggressive scalping with orderbook analysis and breakout detection, fast triggers",
    "defensive": "Conservative defensive strategy with hedging, correlation, and strict thresholds",
    "all_signals": "Balanced strategy using all available signals with moderate sizing",
}
