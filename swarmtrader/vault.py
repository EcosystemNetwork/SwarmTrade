"""ERC-4626 Vault Manager — standard tokenized vault for agent-managed funds.

Inspired by Gorillionaire (ETHGlobal Agentic Ethereum), AgentForge, and
GammaHedge. All agent-managed funds flow through ERC-4626 vaults:

  - Users deposit USDC/ETH, receive vault shares
  - AI agents execute trades through the vault (not user wallets)
  - Vault enforces: token allowlist, max slippage, daily trade limits
  - NAV tracking via share price
  - Composable with DeFi (Aave/Morpho accept vault shares as collateral)

Key security patterns from Gorillionaire:
  - Allowlisted vault swaps — only pre-approved tokens
  - Agent cannot withdraw — only trade within approved parameters
  - Daily trade volume cap at contract level

Supports both on-chain deployment (Base) and simulation mode for development.

Environment variables:
  VAULT_RPC_URL          — RPC endpoint (default: Base mainnet)
  VAULT_CHAIN_ID         — Chain ID (default: 8453 = Base)
  VAULT_ADDRESS          — Deployed vault contract address
  VAULT_PRIVATE_KEY      — Vault manager private key (or shares PRIVATE_KEY)
  VAULT_MODE             — "live" or "simulate" (default: simulate)
  VAULT_DAILY_LIMIT_USD  — Daily trade volume cap (default: 50000)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field

from .core import Bus, TradeIntent, ExecutionReport, MarketSnapshot

log = logging.getLogger("swarm.vault")

# Minimal ERC-4626 ABI — deposit, withdraw, share accounting, NAV
ERC4626_ABI = json.loads("""[
  {"inputs":[],"name":"totalAssets","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"totalSupply","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"name":"assets","type":"uint256"},{"name":"receiver","type":"address"}],"name":"deposit","outputs":[{"type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[{"name":"shares","type":"uint256"},{"name":"receiver","type":"address"},{"name":"owner","type":"address"}],"name":"redeem","outputs":[{"type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[{"name":"assets","type":"uint256"}],"name":"convertToShares","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"name":"shares","type":"uint256"}],"name":"convertToAssets","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"asset","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"name":"owner","type":"address"}],"name":"maxWithdraw","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"name":"owner","type":"address"}],"name":"maxRedeem","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}
]""")

# SwarmVault extension ABI — allowlist, limits, agent management
SWARM_VAULT_ABI = json.loads("""[
  {"inputs":[{"name":"token","type":"address"}],"name":"isTokenAllowed","outputs":[{"type":"bool"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"name":"token","type":"address"}],"name":"addAllowedToken","outputs":[],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[{"name":"token","type":"address"}],"name":"removeAllowedToken","outputs":[],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[],"name":"dailyTradeVolume","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"dailyTradeLimit","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"vaultManager","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"name":"newManager","type":"address"}],"name":"setVaultManager","outputs":[],"stateMutability":"nonpayable","type":"function"}
]""")

# Well-known token addresses on Base
BASE_TOKENS = {
    "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "WETH": "0x4200000000000000000000000000000000000006",
    "DAI": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",
    "USDbC": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA",
    "cbETH": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",
}


@dataclass
class VaultState:
    """Current state of the vault."""
    total_assets: float = 0.0      # total underlying assets (USDC)
    total_shares: float = 0.0      # total vault shares outstanding
    share_price: float = 1.0       # NAV per share
    daily_volume: float = 0.0      # today's trade volume
    daily_limit: float = 50000.0   # max daily volume
    allowed_tokens: set[str] = field(default_factory=lambda: {"USDC", "WETH", "ETH"})
    depositors: dict[str, float] = field(default_factory=dict)  # address -> shares
    trade_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_rebalance: float = 0.0


@dataclass
class VaultDeposit:
    """Record of a deposit into the vault."""
    depositor: str
    assets: float          # amount of underlying deposited
    shares_received: float # vault shares received
    share_price: float     # price at time of deposit
    ts: float = field(default_factory=time.time)


@dataclass
class VaultTrade:
    """Record of a trade executed through the vault."""
    trade_id: str
    token_in: str
    token_out: str
    amount_in: float
    amount_out: float
    agent_id: str         # which agent requested
    intent_id: str        # linked TradeIntent
    ts: float = field(default_factory=time.time)


class VaultManager:
    """Manages an ERC-4626 vault for agent-controlled fund management.

    Modes:
      - simulate: In-memory vault for development/testing
      - live: Interacts with deployed contract on Base via web3

    Security model (from Gorillionaire):
      - Token allowlist: vault only swaps to pre-approved tokens
      - Daily trade limit: enforced per 24h rolling window
      - Agent sandbox: agents can trade through vault but cannot withdraw
      - Human-only withdrawals: only vault owner can redeem shares
    """

    def __init__(self, bus: Bus, mode: str = "simulate",
                 daily_limit: float = 50000.0):
        self.bus = bus
        self.mode = mode
        self.state = VaultState(daily_limit=daily_limit)
        self._deposits: list[VaultDeposit] = []
        self._trades: list[VaultTrade] = []
        self._trade_counter = 0
        self._day_start = _day_start()
        self._w3 = None
        self._contract = None
        self._account = None

        if mode == "live":
            self._init_web3()

        # Subscribe to execution reports to track vault trades
        bus.subscribe("exec.report", self._on_execution)
        bus.subscribe("market.snapshot", self._on_snapshot)

    def _init_web3(self):
        """Initialize web3 connection for live mode."""
        try:
            from web3 import Web3
            from eth_account import Account

            rpc = os.getenv("VAULT_RPC_URL", "https://mainnet.base.org")
            self._w3 = Web3(Web3.HTTPProvider(rpc))
            pk = os.getenv("VAULT_PRIVATE_KEY") or os.getenv("PRIVATE_KEY", "")
            if pk:
                self._account = Account.from_key(pk)

            vault_addr = os.getenv("VAULT_ADDRESS", "")
            if vault_addr and self._w3:
                combined_abi = ERC4626_ABI + SWARM_VAULT_ABI
                self._contract = self._w3.eth.contract(
                    address=Web3.to_checksum_address(vault_addr),
                    abi=combined_abi,
                )
                log.info("Vault connected: %s on chain %s",
                         vault_addr, os.getenv("VAULT_CHAIN_ID", "8453"))
        except ImportError:
            log.warning("web3 not installed — vault running in simulate mode")
            self.mode = "simulate"

    # ── Deposit / Withdraw ───────────────────────────────────────

    def deposit(self, depositor: str, amount: float) -> VaultDeposit:
        """Deposit assets into the vault, receive shares.

        Share price = total_assets / total_shares (or 1.0 if empty).
        """
        if amount <= 0:
            raise ValueError("Deposit amount must be positive")

        price = self.state.share_price
        shares = amount / price

        self.state.total_assets += amount
        self.state.total_shares += shares
        self.state.depositors[depositor] = (
            self.state.depositors.get(depositor, 0.0) + shares
        )
        self._update_share_price()

        record = VaultDeposit(
            depositor=depositor, assets=amount,
            shares_received=shares, share_price=price,
        )
        self._deposits.append(record)
        if len(self._deposits) > 1000:
            self._deposits = self._deposits[-500:]

        log.info("VAULT DEPOSIT: %s deposited $%.2f -> %.4f shares @ $%.4f/share",
                 depositor, amount, shares, price)

        return record

    def redeem(self, depositor: str, shares: float) -> float:
        """Redeem vault shares for underlying assets. Human-only operation."""
        held = self.state.depositors.get(depositor, 0.0)
        if shares > held:
            raise ValueError(f"Insufficient shares: has {held:.4f}, requested {shares:.4f}")

        assets = shares * self.state.share_price
        self.state.total_shares -= shares
        self.state.total_assets -= assets
        self.state.depositors[depositor] = held - shares
        if self.state.depositors[depositor] < 1e-9:
            del self.state.depositors[depositor]
        self._update_share_price()

        log.info("VAULT REDEEM: %s redeemed %.4f shares -> $%.2f",
                 depositor, shares, assets)
        return assets

    # ── Trade Execution ──────────────────────────────────────────

    def validate_trade(self, intent: TradeIntent) -> tuple[bool, str]:
        """Validate a trade against vault constraints.

        Checks:
          1. Token allowlist
          2. Daily trade volume limit
          3. Trade amount vs vault assets
        """
        self._maybe_reset_daily()

        # Token allowlist check
        tokens = {intent.asset_in.upper(), intent.asset_out.upper()}
        for token in tokens:
            if token not in self.state.allowed_tokens and token not in {"USD", "USDC", "USDT"}:
                return False, f"token {token} not in vault allowlist"

        # Daily volume check
        trade_usd = intent.amount_in
        if self.state.daily_volume + trade_usd > self.state.daily_limit:
            return False, (
                f"daily limit exceeded: ${self.state.daily_volume:.0f} + "
                f"${trade_usd:.0f} > ${self.state.daily_limit:.0f}"
            )

        # Asset sufficiency check
        if trade_usd > self.state.total_assets * 0.25:
            return False, (
                f"trade too large: ${trade_usd:.0f} > 25% of vault "
                f"(${self.state.total_assets:.0f})"
            )

        return True, "ok"

    def record_trade(self, intent: TradeIntent, fill_price: float,
                     quantity: float, agent_id: str = "") -> VaultTrade:
        """Record a trade executed through the vault."""
        self._trade_counter += 1
        trade_usd = intent.amount_in

        self.state.daily_volume += trade_usd
        self.state.trade_count += 1

        trade = VaultTrade(
            trade_id=f"vt-{self._trade_counter:05d}",
            token_in=intent.asset_in,
            token_out=intent.asset_out,
            amount_in=intent.amount_in,
            amount_out=quantity,
            agent_id=agent_id,
            intent_id=intent.id,
        )
        self._trades.append(trade)
        if len(self._trades) > 2000:
            self._trades = self._trades[-1000:]

        return trade

    # ── Token Allowlist ──────────────────────────────────────────

    def add_allowed_token(self, token: str):
        """Add a token to the vault's approved list."""
        self.state.allowed_tokens.add(token.upper())
        log.info("Vault allowlist: added %s", token.upper())

    def remove_allowed_token(self, token: str):
        """Remove a token from the vault's approved list."""
        self.state.allowed_tokens.discard(token.upper())
        log.info("Vault allowlist: removed %s", token.upper())

    # ── NAV Tracking ─────────────────────────────────────────────

    def _update_share_price(self):
        """Recalculate NAV per share."""
        if self.state.total_shares > 1e-9:
            self.state.share_price = self.state.total_assets / self.state.total_shares
        else:
            self.state.share_price = 1.0

    def update_nav(self, pnl: float):
        """Update vault NAV based on realized PnL from trades."""
        self.state.total_assets += pnl
        self._update_share_price()

    # ── Bus Integration ──────────────────────────────────────────

    async def _on_execution(self, report: ExecutionReport):
        """Track PnL from execution reports to update vault NAV."""
        if report.status != "filled":
            return
        pnl = report.pnl_estimate or 0.0
        if abs(pnl) > 1e-9:
            self.update_nav(pnl)

    async def _on_snapshot(self, snap: MarketSnapshot):
        """Periodic NAV update from market prices."""
        pass  # NAV updates on trades, not ticks (avoids noise)

    # ── On-Chain Operations (live mode) ──────────────────────────

    async def on_chain_deposit(self, amount_wei: int, receiver: str) -> str | None:
        """Execute deposit on-chain. Returns tx hash."""
        if self.mode != "live" or not self._contract or not self._account:
            return None

        try:
            from web3 import Web3
            tx = self._contract.functions.deposit(
                amount_wei,
                Web3.to_checksum_address(receiver),
            ).build_transaction({
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gas": 300_000,
                "gasPrice": self._w3.eth.gas_price,
                "chainId": int(os.getenv("VAULT_CHAIN_ID", "8453")),
            })
            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = await asyncio.to_thread(
                self._w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120
            )
            return receipt["transactionHash"].hex()
        except Exception as e:
            log.error("On-chain deposit failed: %s", e)
            return None

    async def on_chain_total_assets(self) -> int | None:
        """Read totalAssets from on-chain vault."""
        if self.mode != "live" or not self._contract:
            return None
        try:
            return await asyncio.to_thread(
                self._contract.functions.totalAssets().call
            )
        except Exception as e:
            log.error("On-chain totalAssets failed: %s", e)
            return None

    # ── Utilities ────────────────────────────────────────────────

    def _maybe_reset_daily(self):
        today = _day_start()
        if today > self._day_start:
            self._day_start = today
            self.state.daily_volume = 0.0

    def summary(self) -> dict:
        return {
            "mode": self.mode,
            "total_assets": round(self.state.total_assets, 2),
            "total_shares": round(self.state.total_shares, 4),
            "share_price": round(self.state.share_price, 6),
            "daily_volume": round(self.state.daily_volume, 2),
            "daily_limit": round(self.state.daily_limit, 2),
            "daily_utilization": round(
                self.state.daily_volume / max(self.state.daily_limit, 1), 3
            ),
            "allowed_tokens": sorted(self.state.allowed_tokens),
            "depositors": len(self.state.depositors),
            "total_trades": self.state.trade_count,
            "recent_trades": [
                {
                    "id": t.trade_id,
                    "pair": f"{t.token_in}/{t.token_out}",
                    "amount": round(t.amount_in, 2),
                    "agent": t.agent_id,
                    "ts": t.ts,
                }
                for t in self._trades[-5:]
            ],
        }


def _day_start() -> float:
    import calendar, datetime
    now = datetime.datetime.utcnow()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return calendar.timegm(midnight.timetuple())


def vault_trade_check(vault: VaultManager):
    """Risk check function for the RiskAgent pipeline."""
    from .core import RiskVerdict

    def check(intent: TradeIntent) -> RiskVerdict:
        approved, reason = vault.validate_trade(intent)
        return RiskVerdict(
            intent_id=intent.id,
            agent_id="vault",
            approve=approved,
            reason=reason,
        )
    return check
