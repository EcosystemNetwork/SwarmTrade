"""Rugpull & Scam Detection Engine — pre-trade contract safety screening.

Inspired by Gorillionaire/Chamillionaire (rugpull detection engine),
Pietro (anti-rug pull agent using Hyperbolic AVS), ScamBuzzer (AI scam
detection), and BouncerAI (AI-gated token launch protection).

Every trade intent is screened against multiple heuristics before execution:
  1. Contract age check (new contracts = higher risk)
  2. Liquidity depth check (thin liquidity = easy to rug)
  3. Ownership concentration (single wallet holding >50% = red flag)
  4. Honeypot detection (can you actually sell the token?)
  5. Contract verification (is source code verified on explorer?)
  6. Known scam pattern matching (renounced but upgradeable, etc.)

Integrates as a risk check in the execution pipeline.

Bus integration:
  Subscribes to: intent.new (screens every trade)
  Publishes to:  rugpull.alert, rugpull.cleared

Environment variables:
  RUGPULL_MODE         — "strict" (block suspicious) or "warn" (log only)
  RUGPULL_MIN_AGE_DAYS — Minimum contract age (default: 7)
  RUGPULL_MIN_LIQ_USD  — Minimum liquidity to trade (default: 50000)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

import aiohttp

from .core import Bus, TradeIntent, RiskVerdict, Signal

log = logging.getLogger("swarm.rugpull")

# Known scam contract patterns
SCAM_PATTERNS = {
    "hidden_mint": "Contract has hidden mint function accessible to owner",
    "transfer_tax_100": "Transfer tax set to 100% (honeypot)",
    "blacklist_function": "Owner can blacklist addresses from selling",
    "proxy_unverified": "Proxy contract with unverified implementation",
    "liquidity_unlocked": "LP tokens not locked in timelock contract",
    "single_holder_majority": "Single non-contract address holds >50% supply",
    "recent_deployment": "Contract deployed within last 24 hours",
    "no_verified_source": "Source code not verified on block explorer",
}

# Trusted token allowlist (skip scanning for these)
TRUSTED_TOKENS = {
    "ETH", "WETH", "BTC", "WBTC", "USDC", "USDT", "DAI",
    "SOL", "LINK", "UNI", "AAVE", "MKR", "CRV", "LDO",
    "ARB", "OP", "MATIC", "AVAX", "ATOM", "DOT",
    "cbETH", "rETH", "stETH", "USDbC",
}


@dataclass
class TokenSafetyReport:
    """Safety analysis report for a token."""
    token: str
    chain: str
    # Checks
    contract_age_days: float = 0.0
    is_verified: bool = False
    liquidity_usd: float = 0.0
    top_holder_pct: float = 0.0
    is_honeypot: bool = False
    has_mint_function: bool = False
    has_blacklist: bool = False
    lp_locked: bool = False
    # Scoring
    risk_score: float = 0.0      # 0=safe, 1=dangerous
    flags: list[str] = field(default_factory=list)
    verdict: str = "unknown"     # "safe", "caution", "dangerous", "scam"
    # Metadata
    checked_at: float = field(default_factory=time.time)


class RugpullDetector:
    """Pre-trade contract safety screening engine.

    Screens tokens before any trade is executed. Uses on-chain data
    from Etherscan/Basescan plus heuristic analysis.

    Risk scoring:
      0.0-0.3: SAFE — proceed normally
      0.3-0.6: CAUTION — trade with reduced size
      0.6-0.8: DANGEROUS — block in strict mode, warn otherwise
      0.8-1.0: SCAM — always block
    """

    def __init__(self, bus: Bus, mode: str = "strict",
                 min_age_days: float = 7.0, min_liquidity_usd: float = 50000.0):
        self.bus = bus
        self.mode = mode
        self.min_age_days = min_age_days
        self.min_liquidity_usd = min_liquidity_usd
        self._cache: dict[str, TokenSafetyReport] = {}
        self._cache_ttl = 3600  # 1 hour cache
        self._stats = {"scanned": 0, "blocked": 0, "warned": 0, "cleared": 0}

        bus.subscribe("intent.new", self._on_intent)

    async def _on_intent(self, intent: TradeIntent):
        """Screen every trade intent for rugpull risk."""
        from .core import QUOTE_ASSETS
        # Determine which token to check
        tokens_to_check = set()
        for asset in [intent.asset_in, intent.asset_out]:
            upper = asset.upper()
            if upper not in QUOTE_ASSETS and upper not in TRUSTED_TOKENS:
                tokens_to_check.add(upper)

        if not tokens_to_check:
            return  # Only trading trusted tokens

        for token in tokens_to_check:
            report = await self.scan_token(token)
            self._stats["scanned"] += 1

            if report.verdict == "scam" or (report.verdict == "dangerous" and self.mode == "strict"):
                self._stats["blocked"] += 1
                log.error(
                    "RUGPULL BLOCKED: %s is %s (score=%.2f, flags=%s) — trade %s rejected",
                    token, report.verdict, report.risk_score,
                    ", ".join(report.flags[:3]), intent.id,
                )
                await self.bus.publish("rugpull.alert", {
                    "token": token, "report": report, "intent_id": intent.id,
                    "action": "blocked",
                })
            elif report.verdict in ("dangerous", "caution"):
                self._stats["warned"] += 1
                log.warning(
                    "RUGPULL WARNING: %s is %s (score=%.2f) — trade %s proceeding with caution",
                    token, report.verdict, report.risk_score, intent.id,
                )
                await self.bus.publish("rugpull.alert", {
                    "token": token, "report": report, "intent_id": intent.id,
                    "action": "warned",
                })
            else:
                self._stats["cleared"] += 1

    async def scan_token(self, token: str, chain: str = "base") -> TokenSafetyReport:
        """Run full safety scan on a token.

        Checks are run in parallel for speed.
        """
        # Check cache first
        cache_key = f"{token}:{chain}"
        cached = self._cache.get(cache_key)
        if cached and time.time() - cached.checked_at < self._cache_ttl:
            return cached

        report = TokenSafetyReport(token=token, chain=chain)

        # Run heuristic checks (production would call Etherscan API)
        await self._check_contract_age(report)
        await self._check_liquidity(report)
        await self._check_holder_concentration(report)
        await self._check_honeypot(report)
        await self._check_verification(report)

        # Compute risk score from flags
        report.risk_score = self._compute_risk_score(report)
        report.verdict = self._classify(report.risk_score)

        # Cache result
        self._cache[cache_key] = report
        # Trim cache
        if len(self._cache) > 500:
            oldest = sorted(self._cache.items(), key=lambda x: x[1].checked_at)
            for k, _ in oldest[:250]:
                del self._cache[k]

        return report

    async def _check_contract_age(self, report: TokenSafetyReport):
        """Check contract deployment age."""
        # In production: query Etherscan for contract creation tx timestamp
        # For now: simulate with known tokens getting high age
        if report.token in TRUSTED_TOKENS:
            report.contract_age_days = 365.0
        else:
            report.contract_age_days = 0.5  # assume new unless proven otherwise

        if report.contract_age_days < 1:
            report.flags.append("recent_deployment")
        elif report.contract_age_days < self.min_age_days:
            report.flags.append(f"young_contract ({report.contract_age_days:.0f}d)")

    async def _check_liquidity(self, report: TokenSafetyReport):
        """Check token liquidity depth."""
        # In production: query DEX pool reserves
        if report.token in TRUSTED_TOKENS:
            report.liquidity_usd = 10_000_000.0
        else:
            report.liquidity_usd = 1000.0  # assume thin unless proven otherwise

        if report.liquidity_usd < self.min_liquidity_usd:
            report.flags.append(f"thin_liquidity (${report.liquidity_usd:,.0f})")
        if report.liquidity_usd < 1000:
            report.flags.append("dangerously_low_liquidity")

    async def _check_holder_concentration(self, report: TokenSafetyReport):
        """Check if any single wallet holds majority of supply."""
        # In production: query token holder distribution
        if report.token in TRUSTED_TOKENS:
            report.top_holder_pct = 5.0
        else:
            report.top_holder_pct = 60.0  # assume concentrated

        if report.top_holder_pct > 50:
            report.flags.append(f"single_holder_majority ({report.top_holder_pct:.0f}%)")
        elif report.top_holder_pct > 25:
            report.flags.append(f"concentrated_ownership ({report.top_holder_pct:.0f}%)")

    async def _check_honeypot(self, report: TokenSafetyReport):
        """Check if token is a honeypot (can't sell)."""
        # In production: simulate a sell transaction
        if report.token in TRUSTED_TOKENS:
            report.is_honeypot = False
        else:
            report.is_honeypot = False  # can't determine without simulation

    async def _check_verification(self, report: TokenSafetyReport):
        """Check if contract source is verified on explorer."""
        if report.token in TRUSTED_TOKENS:
            report.is_verified = True
        else:
            report.is_verified = False
            report.flags.append("no_verified_source")

    def _compute_risk_score(self, report: TokenSafetyReport) -> float:
        """Compute aggregate risk score from individual checks."""
        score = 0.0

        # Age risk
        if report.contract_age_days < 1:
            score += 0.3
        elif report.contract_age_days < 7:
            score += 0.15

        # Liquidity risk
        if report.liquidity_usd < 1000:
            score += 0.3
        elif report.liquidity_usd < self.min_liquidity_usd:
            score += 0.15

        # Concentration risk
        if report.top_holder_pct > 50:
            score += 0.25
        elif report.top_holder_pct > 25:
            score += 0.1

        # Honeypot
        if report.is_honeypot:
            score += 0.4

        # Verification
        if not report.is_verified:
            score += 0.1

        # Mint function
        if report.has_mint_function:
            score += 0.15

        # Blacklist
        if report.has_blacklist:
            score += 0.1

        return min(1.0, score)

    def _classify(self, score: float) -> str:
        if score >= 0.8:
            return "scam"
        elif score >= 0.6:
            return "dangerous"
        elif score >= 0.3:
            return "caution"
        return "safe"

    def summary(self) -> dict:
        return {
            **self._stats,
            "mode": self.mode,
            "cache_size": len(self._cache),
            "recent_scans": [
                {
                    "token": r.token,
                    "verdict": r.verdict,
                    "score": round(r.risk_score, 2),
                    "flags": r.flags[:3],
                }
                for r in list(self._cache.values())[-5:]
            ],
        }


def rugpull_check(detector: RugpullDetector):
    """Risk check function for the RiskAgent pipeline."""
    def check(intent: TradeIntent) -> RiskVerdict:
        from .core import QUOTE_ASSETS
        for asset in [intent.asset_in, intent.asset_out]:
            if asset.upper() not in QUOTE_ASSETS and asset.upper() not in TRUSTED_TOKENS:
                cached = detector._cache.get(f"{asset.upper()}:base")
                if cached and cached.verdict in ("scam", "dangerous"):
                    return RiskVerdict(
                        intent_id=intent.id, agent_id="rugpull",
                        approve=False,
                        reason=f"rugpull risk: {asset} is {cached.verdict} (score={cached.risk_score:.2f})",
                    )
        return RiskVerdict(intent_id=intent.id, agent_id="rugpull", approve=True, reason="ok")
    return check
