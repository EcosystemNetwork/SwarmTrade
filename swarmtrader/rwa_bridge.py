"""RWA Bridge — tokenized real-world asset integration.

Inspired by Radegast (AI agents trade tokenized US stocks on 0G Chain),
VERA (municipal bonds on-chain), Equinox (synthetic stocks + perps),
snatch.xyz (tokenized physical assets), and the RWA trend in DeFi.

Enables SwarmTrader agents to discover and trade tokenized real-world
assets alongside crypto:
  1. Stock tokens (tokenized equities via RWA protocols)
  2. Treasury bills (T-bill yield products like Ondo/Backed)
  3. Commodity tokens (gold, oil, real estate)
  4. FX pairs (EUR/USD via stablecoin pairs)
  5. Credit products (tokenized bonds, structured products)

Bus integration:
  Subscribes to: market.snapshot (price data for RWA tokens)
  Publishes to:  rwa.opportunity, signal.rwa
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .core import Bus, Signal

log = logging.getLogger("swarm.rwa")


@dataclass
class RWAsset:
    """A tokenized real-world asset."""
    symbol: str
    name: str
    asset_class: str        # "equity", "treasury", "commodity", "fx", "credit"
    # On-chain representation
    token_address: str = ""
    chain_id: int = 8453    # Base
    protocol: str = ""      # "ondo", "backed", "swarm_markets", "centrifuge"
    # Market data
    price_usd: float = 0.0
    yield_apy: float = 0.0
    market_cap: float = 0.0
    daily_volume: float = 0.0
    # Risk
    credit_rating: str = ""  # "AAA", "AA", "A", "BBB", etc.
    maturity_date: str = ""  # for bonds/bills
    risk_score: float = 0.3  # 0=safe, 1=risky


# Known RWA tokens on Base and Ethereum
KNOWN_RWA_TOKENS = {
    "USDY": RWAsset("USDY", "Ondo US Dollar Yield", "treasury",
                    protocol="ondo", yield_apy=4.5, credit_rating="AAA"),
    "OUSG": RWAsset("OUSG", "Ondo Short-Term US Gov", "treasury",
                    protocol="ondo", yield_apy=4.8, credit_rating="AAA"),
    "bIB01": RWAsset("bIB01", "Backed IB01 Treasury ETF", "treasury",
                     protocol="backed", yield_apy=4.2, credit_rating="AAA"),
    "PAXG": RWAsset("PAXG", "Pax Gold", "commodity",
                    protocol="paxos", risk_score=0.2),
    "wstETH": RWAsset("wstETH", "Wrapped Staked ETH", "credit",
                      protocol="lido", yield_apy=3.5, risk_score=0.15),
}


@dataclass
class RWAOpportunity:
    """A detected opportunity in RWA markets."""
    asset: RWAsset
    opportunity_type: str    # "yield", "arbitrage", "momentum", "rebalance"
    expected_return: float
    risk_adjusted_return: float
    rationale: str
    ts: float = field(default_factory=time.time)


class RWABridge:
    """Integrates tokenized real-world assets into the trading swarm.

    Monitors RWA token prices and yields, identifies opportunities
    (yield differentials, arb between tokenized and native assets,
    risk-off rotations into T-bills during crypto drawdowns).
    """

    def __init__(self, bus: Bus):
        self.bus = bus
        self._assets: dict[str, RWAsset] = dict(KNOWN_RWA_TOKENS)
        self._opportunities: list[RWAOpportunity] = []
        self._stats = {"scans": 0, "opportunities": 0}

        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("signal.regime", self._on_regime)

        self._regime = "normal"
        self._crypto_drawdown = False

    async def _on_regime(self, sig: Signal):
        if isinstance(sig, Signal):
            regime = sig.rationale.lower()
            self._regime = regime
            self._crypto_drawdown = "crisis" in regime or "volatile" in regime

    async def _on_snapshot(self, snap):
        self._stats["scans"] += 1
        # In production: update RWA token prices from oracles
        await self._scan_opportunities()

    async def _scan_opportunities(self):
        """Scan for RWA investment opportunities."""
        for symbol, asset in self._assets.items():
            # Risk-off rotation: during crypto crisis, T-bills are attractive
            if self._crypto_drawdown and asset.asset_class == "treasury" and asset.yield_apy > 3:
                opp = RWAOpportunity(
                    asset=asset,
                    opportunity_type="yield",
                    expected_return=asset.yield_apy / 100,
                    risk_adjusted_return=asset.yield_apy / 100 * (1 - asset.risk_score),
                    rationale=f"Risk-off: {asset.name} yielding {asset.yield_apy}% during crypto drawdown",
                )
                self._opportunities.append(opp)
                self._stats["opportunities"] += 1

                sig = Signal(
                    agent_id="rwa_bridge",
                    asset=symbol,
                    direction="long",
                    strength=min(1.0, asset.yield_apy / 10),
                    confidence=0.7,
                    rationale=opp.rationale,
                )
                await self.bus.publish("signal.rwa", sig)
                await self.bus.publish("rwa.opportunity", opp)

        if len(self._opportunities) > 200:
            self._opportunities = self._opportunities[-100:]

    def register_asset(self, asset: RWAsset):
        """Register a new RWA token for monitoring."""
        self._assets[asset.symbol] = asset
        log.info("RWA: registered %s (%s, %s)", asset.symbol, asset.name, asset.asset_class)

    def summary(self) -> dict:
        return {
            **self._stats,
            "assets": len(self._assets),
            "by_class": {
                cls: len([a for a in self._assets.values() if a.asset_class == cls])
                for cls in set(a.asset_class for a in self._assets.values())
            },
            "opportunities": [
                {
                    "asset": o.asset.symbol,
                    "type": o.opportunity_type,
                    "return": round(o.expected_return * 100, 2),
                    "rationale": o.rationale[:60],
                }
                for o in self._opportunities[-5:]
            ],
        }
