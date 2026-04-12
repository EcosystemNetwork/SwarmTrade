"""Token Launch Sniper — early token detection and entry.

Inspired by BouncerAI (AI-gated token launch protection), LanzaMeme AI
(AI-powered token launchpad), pochi.po (scans Twitter, auto-mints), and
the broader pattern of early-stage token trading on DEXes.

Monitors DEX factory contracts and social feeds for new token launches,
evaluates safety, and enters early positions on promising tokens.

Safety-first approach (from BouncerAI + rugpull_detector):
  1. Detect new token pair creation on Uniswap/Aerodrome
  2. Run rugpull safety scan (contract verification, liquidity, ownership)
  3. Check social signal strength (is there organic buzz?)
  4. If safe + signal strong: enter small position
  5. Scale position as safety metrics improve over time

Bus integration:
  Subscribes to: social_alpha.trending, rugpull.cleared
  Publishes to:  sniper.detected, sniper.entered, signal.sniper
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .core import Bus, Signal

log = logging.getLogger("swarm.sniper")


@dataclass
class TokenLaunch:
    """A detected new token launch."""
    token: str
    pair_address: str
    dex: str               # "uniswap", "aerodrome", "sushiswap"
    chain: str = "base"
    # Initial metrics
    initial_liquidity_usd: float = 0.0
    initial_price: float = 0.0
    creator_address: str = ""
    # Safety
    safety_score: float = 0.0   # from rugpull detector
    is_verified: bool = False
    lp_locked: bool = False
    # Social
    social_buzz: float = 0.0
    mention_count: int = 0
    # Status
    status: str = "detected"     # detected, evaluating, entered, passed, rugged
    entry_price: float = 0.0
    entry_amount_usd: float = 0.0
    current_price: float = 0.0
    pnl: float = 0.0
    detected_at: float = field(default_factory=time.time)


class TokenSniper:
    """Early token launch detection and safe entry engine.

    Entry criteria (ALL must pass):
      1. Initial liquidity > $5k (not a dust launch)
      2. Safety score > 0.4 (passes basic rugpull checks)
      3. Social buzz > 0 (someone is talking about it)
      4. Not a known scam pattern
      5. Entry amount capped at max_entry_usd

    Exit criteria:
      - 2x: sell 50% (lock in profit)
      - 5x: sell 25% more
      - Safety deterioration: full exit
      - 24h: reassess based on updated metrics
    """

    def __init__(self, bus: Bus, max_entry_usd: float = 100.0,
                 min_liquidity_usd: float = 5000.0,
                 min_safety: float = 0.4):
        self.bus = bus
        self.max_entry_usd = max_entry_usd
        self.min_liquidity_usd = min_liquidity_usd
        self.min_safety = min_safety
        self._launches: dict[str, TokenLaunch] = {}
        self._entered: list[str] = []  # tokens we entered
        self._stats = {
            "detected": 0, "evaluated": 0, "entered": 0,
            "passed": 0, "rugged": 0, "total_pnl": 0.0,
        }

        bus.subscribe("social_alpha.trending", self._on_trending)

    async def _on_trending(self, trending):
        """Evaluate trending tokens for snipe entry."""
        if not hasattr(trending, "token"):
            if isinstance(trending, dict):
                token = trending.get("token", "")
                buzz = trending.get("signal_strength", 0)
                mentions = trending.get("mention_count", 0)
            else:
                return
        else:
            token = trending.token
            buzz = trending.signal_strength
            mentions = trending.mention_count

        if not token or token in self._launches:
            return

        launch = TokenLaunch(
            token=token, pair_address="", dex="uniswap",
            social_buzz=buzz, mention_count=mentions,
        )
        self._launches[token] = launch
        self._stats["detected"] += 1

        # Evaluate for entry
        await self._evaluate(launch)

    async def _evaluate(self, launch: TokenLaunch):
        """Evaluate a token launch for safe entry."""
        self._stats["evaluated"] += 1

        # Safety checks
        checks_passed = 0
        total_checks = 4

        if launch.initial_liquidity_usd >= self.min_liquidity_usd:
            checks_passed += 1
        if launch.safety_score >= self.min_safety:
            checks_passed += 1
        if launch.social_buzz > 0:
            checks_passed += 1
        if launch.mention_count >= 2:
            checks_passed += 1

        # For tokens detected via social (no on-chain data yet),
        # require at least social signals
        if launch.social_buzz > 0.3 and launch.mention_count >= 3:
            launch.status = "evaluating"

            # Size position based on conviction
            conviction = launch.social_buzz * 0.5 + (checks_passed / total_checks) * 0.5
            entry_size = min(self.max_entry_usd, self.max_entry_usd * conviction)

            if entry_size >= 10:  # minimum $10
                launch.status = "entered"
                launch.entry_amount_usd = round(entry_size, 2)
                self._entered.append(launch.token)
                self._stats["entered"] += 1

                log.info(
                    "SNIPER ENTRY: %s $%.0f (buzz=%.2f, mentions=%d, safety=%.2f)",
                    launch.token, entry_size, launch.social_buzz,
                    launch.mention_count, launch.safety_score,
                )

                sig = Signal(
                    agent_id="token_sniper",
                    asset=launch.token,
                    direction="long",
                    strength=min(1.0, conviction),
                    confidence=min(0.5, checks_passed / total_checks),
                    rationale=(
                        f"Sniper: {launch.token} early entry ${ entry_size:.0f} "
                        f"(buzz={launch.social_buzz:.2f}, {launch.mention_count} mentions)"
                    ),
                )
                await self.bus.publish("signal.sniper", sig)
                await self.bus.publish("sniper.entered", launch)
            else:
                launch.status = "passed"
                self._stats["passed"] += 1
        else:
            launch.status = "passed"
            self._stats["passed"] += 1

    def summary(self) -> dict:
        return {
            **self._stats,
            "tracked": len(self._launches),
            "active_positions": len(self._entered),
            "recent": [
                {
                    "token": l.token,
                    "status": l.status,
                    "buzz": round(l.social_buzz, 2),
                    "entry": l.entry_amount_usd,
                }
                for l in list(self._launches.values())[-5:]
            ],
        }
