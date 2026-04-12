"""Strategy-as-NFT — ownable, forkable, tradeable trading strategies.

Inspired by GhostFi (ERC-7857 iNFTs), Based Agents (Commander NFTs),
and Streme.fun (AI agent token deployment).

Strategies are serialized as JSON configs, minted as on-chain NFTs,
and can be forked (creating derivative NFTs with royalty chains).

Key patterns:
  - Mint: serialize strategy -> upload metadata -> mint ERC-721
  - Fork: create child NFT pointing to parent, inherit params, customize
  - Royalty: 5% of copy-trade profits flow to NFT owner
  - Marketplace: browse by Sharpe, max drawdown, asset class
  - Composability: strategies can reference other strategies (meta-strategies)

Bus integration:
  Subscribes to: exec.report (track strategy performance)
  Publishes to:  strategy_nft.minted, strategy_nft.forked

Environment variables:
  STRATEGY_NFT_MODE       — "simulate" or "live" (default: simulate)
  STRATEGY_NFT_CONTRACT   — Deployed NFT contract address
  STRATEGY_NFT_ROYALTY_BPS — Default royalty in basis points (default: 500 = 5%)
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field, asdict

from .core import Bus, ExecutionReport

log = logging.getLogger("swarm.strategy_nft")

ROYALTY_BPS_DEFAULT = 500  # 5%


@dataclass
class StrategyConfig:
    """Serializable strategy configuration."""
    name: str
    description: str
    # Signal weights
    signal_weights: dict[str, float] = field(default_factory=dict)
    # Regime overrides
    regime_multipliers: dict[str, dict[str, float]] = field(default_factory=dict)
    # Risk parameters
    max_position_pct: float = 10.0
    max_daily_trades: int = 50
    stop_loss_pct: float = 5.0
    take_profit_pct: float = 10.0
    # Kelly sizing
    kelly_fraction: float = 0.25
    # Asset universe
    allowed_assets: list[str] = field(default_factory=lambda: ["ETH", "BTC"])
    # Timeframe
    timeframe: str = "5m"
    # Tags
    tags: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    def content_hash(self) -> str:
        """Deterministic hash of strategy config for deduplication."""
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]

    @classmethod
    def from_json(cls, data: str) -> "StrategyConfig":
        return cls(**json.loads(data))


@dataclass
class StrategyNFT:
    """On-chain NFT representing a trading strategy."""
    token_id: str
    config: StrategyConfig
    # Ownership
    owner: str                    # wallet address
    creator: str                  # original creator
    # Lineage
    parent_id: str | None = None  # forked from (None = original)
    children: list[str] = field(default_factory=list)
    fork_depth: int = 0           # 0 = original, 1 = first fork, etc.
    # Performance
    total_pnl: float = 0.0
    total_trades: int = 0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    # Economics
    royalty_bps: int = ROYALTY_BPS_DEFAULT
    total_royalties_earned: float = 0.0
    copiers: int = 0
    # Metadata
    content_hash: str = ""
    minted_at: float = field(default_factory=time.time)
    # Chain data
    chain_id: int = 8453          # Base
    contract_address: str = ""
    tx_hash: str = ""


@dataclass
class RoyaltyPayment:
    """Record of a royalty payment to strategy NFT owner."""
    token_id: str
    recipient: str           # NFT owner receiving payment
    pnl_source: float        # PnL that triggered the royalty
    royalty_amount: float     # actual payment
    ts: float = field(default_factory=time.time)


class StrategyNFTManager:
    """Manages the lifecycle of strategy NFTs.

    Mint → Fork → Track Performance → Distribute Royalties

    Simulation mode maintains an in-memory registry.
    Live mode would interact with a deployed ERC-721 contract.
    """

    def __init__(self, bus: Bus, mode: str = "simulate"):
        self.bus = bus
        self.mode = mode
        self._registry: dict[str, StrategyNFT] = {}  # token_id -> NFT
        self._royalties: list[RoyaltyPayment] = []
        self._token_counter = 0
        self._stats = {
            "minted": 0, "forked": 0, "total_royalties": 0.0,
        }

        bus.subscribe("exec.report", self._on_execution)

    # ── Mint ─────────────────────────────────────────────────────

    def mint(self, config: StrategyConfig, owner: str,
             royalty_bps: int = ROYALTY_BPS_DEFAULT) -> StrategyNFT:
        """Mint a new strategy NFT."""
        self._token_counter += 1
        token_id = f"strat-{self._token_counter:05d}"

        nft = StrategyNFT(
            token_id=token_id,
            config=config,
            owner=owner,
            creator=owner,
            royalty_bps=royalty_bps,
            content_hash=config.content_hash(),
        )
        self._registry[token_id] = nft
        self._stats["minted"] += 1

        log.info(
            "STRATEGY MINTED: %s '%s' by %s (royalty=%dbps, assets=%s)",
            token_id, config.name, owner[:10], royalty_bps,
            ",".join(config.allowed_assets),
        )

        return nft

    # ── Fork ─────────────────────────────────────────────────────

    def fork(self, parent_id: str, new_owner: str,
             overrides: dict | None = None) -> StrategyNFT:
        """Fork an existing strategy, creating a derivative NFT.

        The child inherits the parent's config with optional overrides.
        Royalties flow up the chain: child profits → parent owner.
        """
        parent = self._registry.get(parent_id)
        if not parent:
            raise ValueError(f"Parent strategy {parent_id} not found")

        # Clone config with overrides
        parent_dict = json.loads(parent.config.to_json())
        if overrides:
            parent_dict.update(overrides)
        child_config = StrategyConfig(**parent_dict)

        self._token_counter += 1
        token_id = f"strat-{self._token_counter:05d}"

        child = StrategyNFT(
            token_id=token_id,
            config=child_config,
            owner=new_owner,
            creator=new_owner,
            parent_id=parent_id,
            fork_depth=parent.fork_depth + 1,
            royalty_bps=parent.royalty_bps,
            content_hash=child_config.content_hash(),
        )
        self._registry[token_id] = child
        parent.children.append(token_id)
        self._stats["forked"] += 1

        log.info(
            "STRATEGY FORKED: %s from %s by %s (depth=%d)",
            token_id, parent_id, new_owner[:10], child.fork_depth,
        )

        return child

    # ── Performance Tracking ─────────────────────────────────────

    async def _on_execution(self, report: ExecutionReport):
        """Track performance and distribute royalties."""
        if report.status != "filled":
            return
        pnl = report.pnl_estimate or 0.0
        if abs(pnl) < 1e-9:
            return

        # Update all active strategies
        for nft in self._registry.values():
            if report.asset in nft.config.allowed_assets or not nft.config.allowed_assets:
                nft.total_trades += 1
                nft.total_pnl += pnl

                # Track max drawdown
                if pnl < 0:
                    nft.max_drawdown = min(nft.max_drawdown, nft.total_pnl)

        # Distribute royalties for profitable trades
        if pnl > 0:
            await self._distribute_royalties(report.asset, pnl)

    async def _distribute_royalties(self, asset: str, pnl: float):
        """Distribute royalties up the fork chain."""
        for nft in self._registry.values():
            if nft.copiers <= 0:
                continue
            if asset not in nft.config.allowed_assets and nft.config.allowed_assets:
                continue

            royalty = pnl * (nft.royalty_bps / 10_000)
            if royalty < 0.001:
                continue

            nft.total_royalties_earned += royalty
            self._stats["total_royalties"] += royalty

            payment = RoyaltyPayment(
                token_id=nft.token_id,
                recipient=nft.owner,
                pnl_source=pnl,
                royalty_amount=royalty,
            )
            self._royalties.append(payment)
            if len(self._royalties) > 1000:
                self._royalties = self._royalties[-500:]

            # Also distribute to parent (fork chain royalties)
            if nft.parent_id:
                parent = self._registry.get(nft.parent_id)
                if parent:
                    parent_royalty = royalty * 0.2  # 20% of child royalty to parent
                    parent.total_royalties_earned += parent_royalty

    # ── Discovery ────────────────────────────────────────────────

    def browse(self, sort_by: str = "pnl", top_n: int = 20,
               tags: list[str] | None = None) -> list[dict]:
        """Browse strategy marketplace."""
        strategies = list(self._registry.values())

        if tags:
            strategies = [s for s in strategies
                         if any(t in s.config.tags for t in tags)]

        if sort_by == "pnl":
            strategies.sort(key=lambda s: s.total_pnl, reverse=True)
        elif sort_by == "sharpe":
            strategies.sort(key=lambda s: s.sharpe_ratio, reverse=True)
        elif sort_by == "drawdown":
            strategies.sort(key=lambda s: s.max_drawdown)  # least drawdown first
        elif sort_by == "royalties":
            strategies.sort(key=lambda s: s.total_royalties_earned, reverse=True)
        elif sort_by == "copiers":
            strategies.sort(key=lambda s: s.copiers, reverse=True)

        return [
            {
                "token_id": s.token_id,
                "name": s.config.name,
                "creator": s.creator[:10] + "...",
                "total_pnl": round(s.total_pnl, 4),
                "trades": s.total_trades,
                "max_drawdown": round(s.max_drawdown, 4),
                "royalties_earned": round(s.total_royalties_earned, 4),
                "copiers": s.copiers,
                "fork_depth": s.fork_depth,
                "tags": s.config.tags,
                "assets": s.config.allowed_assets,
            }
            for s in strategies[:top_n]
        ]

    def get_lineage(self, token_id: str) -> list[str]:
        """Get the full fork chain for a strategy."""
        chain = []
        current = token_id
        while current:
            chain.append(current)
            nft = self._registry.get(current)
            if nft:
                current = nft.parent_id
            else:
                break
        chain.reverse()
        return chain

    def summary(self) -> dict:
        return {
            **self._stats,
            "total_strategies": len(self._registry),
            "originals": len([s for s in self._registry.values() if s.fork_depth == 0]),
            "forks": len([s for s in self._registry.values() if s.fork_depth > 0]),
            "top_strategies": self.browse(top_n=5),
        }
