"""Auto-Compound Yield Aggregator — harvest and reinvest DeFi yields.

Inspired by Harvest (ETHGlobal Cannes 2026) and Beefy Finance patterns.
Monitors DeFi protocol yields, auto-harvests rewards, swaps to base
tokens via Uniswap, and re-deposits to compound returns.

Supported strategies:
  - Morpho vault deposits (lending)
  - Uniswap V3 fee harvesting
  - Aave/Compound supply rates
  - Staking yields

Data sources:
  - DeFi Llama APY API (free, no key)
  - Protocol-specific APIs
  - On-chain monitoring

Environment variables:
  PRIVATE_KEY           — wallet private key for transactions
  UNISWAP_API_KEY       — for reward token swaps
  YIELD_MIN_APY         — minimum APY to consider (default: 2.0%)
  YIELD_HARVEST_THRESHOLD — min USD to harvest (default: 10.0)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .core import Bus, Signal

log = logging.getLogger("swarm.yield")

# DeFi Llama endpoints (free, no auth)
DEFILLAMA_POOLS = "https://yields.llama.fi/pools"
DEFILLAMA_CHART = "https://yields.llama.fi/chart"

# Rate limiting for external APIs
_last_api_call: dict[str, float] = {}
_MIN_API_INTERVAL = 1.0


async def _rate_limit(api_name: str):
    now = time.time()
    last = _last_api_call.get(api_name, 0.0)
    wait = _MIN_API_INTERVAL - (now - last)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_api_call[api_name] = time.time()


@dataclass
class YieldPool:
    """A DeFi yield opportunity."""
    pool_id: str
    protocol: str          # "morpho", "aave-v3", "compound-v3", "uniswap-v3"
    chain: str             # "ethereum", "base", "arbitrum"
    asset: str             # primary asset
    tvl_usd: float
    apy: float             # current APY as percentage
    apy_7d: float          # 7-day average APY
    apy_30d: float         # 30-day average APY
    il_risk: str           # "none", "low", "medium", "high"
    audit_score: float     # 0-10 safety rating
    url: str = ""


@dataclass
class VaultPosition:
    """Our active position in a yield pool."""
    pool_id: str
    protocol: str
    chain: str
    asset: str
    deposited_usd: float
    current_value_usd: float
    pending_rewards_usd: float
    total_harvested_usd: float
    harvest_count: int
    deposited_at: float
    last_harvest: float = 0.0
    auto_compound: bool = True
    status: str = "active"


@dataclass
class HarvestAction:
    """Record of a yield harvest operation."""
    vault_id: str
    rewards_usd: float
    gas_cost_usd: float
    net_profit_usd: float
    timestamp: float = field(default_factory=time.time)
    tx_hash: str = ""
    redeposited: bool = False


class YieldAggregator:
    """Monitors DeFi yields and manages auto-compounding vault positions.

    The aggregator:
    1. Scans DeFi Llama for top yield opportunities
    2. Filters by safety (audit score, TVL, IL risk)
    3. Manages vault positions with auto-harvesting
    4. Compounds rewards by swapping and re-depositing
    5. Publishes yield intelligence for the trading swarm

    Publishes:
      signal.yield         — directional signal when yield shifts indicate market moves
      intelligence.yield   — raw yield data for dashboard
      execution.yield      — harvest/deposit execution reports
    """

    name = "yield_aggregator"

    def __init__(
        self,
        bus: Bus,
        assets: list[str] | None = None,
        chains: list[str] | None = None,
        min_apy: float = 2.0,
        min_tvl: float = 1_000_000.0,
        min_audit_score: float = 7.0,
        harvest_threshold_usd: float = 10.0,
        scan_interval: float = 600.0,
        harvest_interval: float = 3600.0,
    ):
        self.bus = bus
        self.assets = assets or ["ETH", "USDC", "WBTC"]
        self.chains = chains or ["Ethereum", "Base", "Arbitrum"]
        self.min_apy = min_apy
        self.min_tvl = min_tvl
        self.min_audit_score = min_audit_score
        self.harvest_threshold_usd = harvest_threshold_usd
        self.scan_interval = scan_interval
        self.harvest_interval = harvest_interval
        self._stop = False

        # State
        self.top_pools: list[YieldPool] = []
        self.positions: dict[str, VaultPosition] = {}
        self.harvest_history: list[HarvestAction] = []
        self._last_scan: float = 0.0
        self._last_harvest: float = 0.0
        self._total_harvested: float = 0.0
        self._total_gas: float = 0.0

    def stop(self):
        self._stop = True

    async def run(self):
        log.info(
            "YieldAggregator starting: chains=%s min_apy=%.1f%% scan=%.0fs harvest=%.0fs",
            self.chains, self.min_apy, self.scan_interval, self.harvest_interval,
        )
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                now = time.time()

                # Scan for yield opportunities
                if now - self._last_scan >= self.scan_interval:
                    await self._scan_pools(session)
                    self._last_scan = now

                # Harvest pending rewards
                if now - self._last_harvest >= self.harvest_interval:
                    await self._harvest_all()
                    self._last_harvest = now

                # Publish intelligence
                await self._publish_intelligence()

                await asyncio.sleep(min(self.scan_interval, self.harvest_interval) / 2)

    async def _scan_pools(self, session: aiohttp.ClientSession):
        """Scan DeFi Llama for top yield pools."""
        try:
            await _rate_limit("defillama")
            async with session.get(
                DEFILLAMA_POOLS,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    log.warning("DeFi Llama API error: %d", resp.status)
                    return
                data = await resp.json()

            pools = data.get("data", [])
            filtered: list[YieldPool] = []

            chain_map = {c.lower(): c for c in self.chains}
            asset_symbols = {a.upper() for a in self.assets}
            # Also match wrapped versions
            asset_symbols.update({f"W{a}" for a in self.assets})
            asset_symbols.update({"WETH", "WBTC", "stETH", "cbETH", "rETH"})

            for pool in pools:
                chain = pool.get("chain", "")
                if chain.lower() not in chain_map:
                    continue

                symbol = pool.get("symbol", "").upper()
                # Check if any of our target assets are in the pool
                pool_assets = set(symbol.replace("-", " ").replace("/", " ").split())
                if not pool_assets & asset_symbols:
                    continue

                apy = pool.get("apy", 0) or 0
                tvl = pool.get("tvlUsd", 0) or 0

                if apy < self.min_apy or tvl < self.min_tvl:
                    continue

                # Estimate IL risk based on pool type
                project = pool.get("project", "").lower()
                if "lending" in project or "aave" in project or "compound" in project or "morpho" in project:
                    il_risk = "none"
                elif "staking" in project or "lido" in project:
                    il_risk = "low"
                elif "uniswap" in project or "curve" in project:
                    il_risk = "medium"
                else:
                    il_risk = "medium"

                # Audit score heuristic (based on TVL and protocol maturity)
                audit_score = min(10.0, 5.0 + (tvl / 100_000_000) * 2)
                well_known = {"aave", "compound", "morpho", "lido", "rocket", "uniswap", "curve", "convex", "maker"}
                if any(p in project for p in well_known):
                    audit_score = min(10.0, audit_score + 2.0)

                if audit_score < self.min_audit_score:
                    continue

                primary_asset = ""
                for a in self.assets:
                    if a.upper() in pool_assets or f"W{a.upper()}" in pool_assets:
                        primary_asset = a
                        break
                if not primary_asset:
                    primary_asset = symbol.split("-")[0].split("/")[0]

                filtered.append(YieldPool(
                    pool_id=pool.get("pool", ""),
                    protocol=pool.get("project", ""),
                    chain=chain,
                    asset=primary_asset,
                    tvl_usd=tvl,
                    apy=apy,
                    apy_7d=pool.get("apyMean7d", apy) or apy,
                    apy_30d=pool.get("apyMean30d", apy) or apy,
                    il_risk=il_risk,
                    audit_score=audit_score,
                ))

            # Sort by APY descending
            filtered.sort(key=lambda p: p.apy, reverse=True)
            self.top_pools = filtered[:50]  # Keep top 50

            log.info(
                "Yield scan: %d pools found, top APY=%.1f%% (%s on %s)",
                len(self.top_pools),
                self.top_pools[0].apy if self.top_pools else 0,
                self.top_pools[0].protocol if self.top_pools else "none",
                self.top_pools[0].chain if self.top_pools else "none",
            )

            # Generate trading signal from yield changes
            await self._yield_signal()

        except Exception as e:
            log.warning("Yield scan error: %s", e)

    async def _yield_signal(self):
        """Generate trading signals based on yield environment changes."""
        if not self.top_pools:
            return

        # High yields on stablecoins = risk-off sentiment (demand for safety)
        # High yields on ETH/BTC = risk-on (demand for leverage/exposure)
        stable_yields = [p.apy for p in self.top_pools if p.asset in ("USDC", "USDT", "DAI")]
        crypto_yields = [p.apy for p in self.top_pools if p.asset in ("ETH", "BTC", "WETH", "WBTC")]

        avg_stable = sum(stable_yields) / len(stable_yields) if stable_yields else 0
        avg_crypto = sum(crypto_yields) / len(crypto_yields) if crypto_yields else 0

        # If crypto yields >> stable yields, market is risk-on (bullish)
        if avg_crypto > 0 and avg_stable > 0:
            ratio = avg_crypto / avg_stable
            if ratio > 2.0:
                direction = "long"
                strength = min(0.5, (ratio - 2.0) * 0.1)
            elif ratio < 0.5:
                direction = "short"
                strength = min(0.5, (0.5 - ratio) * 0.5)
            else:
                return  # Neutral

            sig = Signal(
                agent_id="yield_aggregator",
                asset="ETH",
                direction=direction,
                strength=strength,
                confidence=0.4,  # Yield-based signals are slower/weaker
                rationale=(
                    f"Yield environment: crypto_avg={avg_crypto:.1f}% "
                    f"stable_avg={avg_stable:.1f}% ratio={ratio:.2f}"
                ),
            )
            await self.bus.publish("signal.yield", sig)

    async def _harvest_all(self):
        """Harvest rewards from all active positions."""
        for pos_id, pos in self.positions.items():
            if pos.status != "active" or not pos.auto_compound:
                continue
            if pos.pending_rewards_usd < self.harvest_threshold_usd:
                continue

            # Simulate harvest (in production, this calls the vault contract)
            gas_cost = 0.05  # Base chain gas is cheap
            net = pos.pending_rewards_usd - gas_cost

            if net <= 0:
                continue

            action = HarvestAction(
                vault_id=pos_id,
                rewards_usd=pos.pending_rewards_usd,
                gas_cost_usd=gas_cost,
                net_profit_usd=net,
                redeposited=pos.auto_compound,
            )

            # Update position
            if pos.auto_compound:
                pos.deposited_usd += net
                pos.current_value_usd += net
            pos.total_harvested_usd += pos.pending_rewards_usd
            pos.pending_rewards_usd = 0.0
            pos.harvest_count += 1
            pos.last_harvest = time.time()

            self.harvest_history.append(action)
            self._total_harvested += action.rewards_usd
            self._total_gas += gas_cost

            log.info(
                "HARVEST %s: $%.2f rewards, $%.4f gas, net=$%.2f (redeposited=%s)",
                pos_id, action.rewards_usd, gas_cost, net, pos.auto_compound,
            )

            await self.bus.publish("execution.yield", {
                "action": "harvest",
                "vault": pos_id,
                "rewards_usd": action.rewards_usd,
                "gas_usd": gas_cost,
                "net_usd": net,
                "redeposited": pos.auto_compound,
            })

    async def _publish_intelligence(self):
        """Publish yield intelligence for dashboard and other agents."""
        if not self.top_pools:
            return

        intel = {
            "ts": time.time(),
            "top_pools": [
                {
                    "protocol": p.protocol,
                    "chain": p.chain,
                    "asset": p.asset,
                    "apy": round(p.apy, 2),
                    "tvl": round(p.tvl_usd, 0),
                    "il_risk": p.il_risk,
                    "safety": round(p.audit_score, 1),
                }
                for p in self.top_pools[:10]
            ],
            "positions": {
                pid: {
                    "protocol": pos.protocol,
                    "asset": pos.asset,
                    "deposited": round(pos.deposited_usd, 2),
                    "current": round(pos.current_value_usd, 2),
                    "pending_rewards": round(pos.pending_rewards_usd, 2),
                    "total_harvested": round(pos.total_harvested_usd, 2),
                    "harvests": pos.harvest_count,
                }
                for pid, pos in self.positions.items()
            },
            "total_harvested": round(self._total_harvested, 2),
            "total_gas": round(self._total_gas, 4),
        }
        await self.bus.publish("intelligence.yield", intel)

    def summary(self) -> dict:
        return {
            "pools_tracked": len(self.top_pools),
            "active_positions": len([p for p in self.positions.values() if p.status == "active"]),
            "total_deposited": round(sum(p.deposited_usd for p in self.positions.values()), 2),
            "total_harvested": round(self._total_harvested, 2),
            "total_gas_spent": round(self._total_gas, 4),
            "net_yield_profit": round(self._total_harvested - self._total_gas, 2),
            "top_apy": round(self.top_pools[0].apy, 2) if self.top_pools else 0,
            "top_protocol": self.top_pools[0].protocol if self.top_pools else "none",
        }
