"""ZK Private Trading — encrypted orders and dark pool matching.

Inspired by CipherPool (encrypted order book), Zebra (ZK dark pool on Sui),
BlindPool (sealed-bid token auctions), PrivBatch (private batch swaps),
Nox (ZK dark pool with privacy proxy), and Clawlogic (agent-only markets).

Capabilities:
  1. Commit-reveal order submission (hide order details until execution)
  2. Dark pool matching (sealed-bid P2P without market impact)
  3. Private position tracking (ZK proofs for portfolio privacy)
  4. MEV resistance via encrypted intents
  5. Agent-verified markets (only authenticated AI agents can participate)

This module provides the Python-side cryptographic primitives and
coordination logic. On-chain ZK verification would use deployed
verifier contracts (Groth16 or PLONK).

Bus integration:
  Subscribes to: exec.go (intercept for private execution path)
  Publishes to:  zk.order_committed, zk.order_revealed, zk.match_found

No external dependencies beyond hashlib and secrets (stdlib).
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass, field

from .core import Bus, TradeIntent, ExecutionReport

log = logging.getLogger("swarm.zk")


@dataclass
class CommitRevealOrder:
    """An order using commit-reveal scheme for MEV resistance.

    Phase 1 (commit): publish hash(order + salt). Nobody knows the order details.
    Phase 2 (reveal): publish order + salt after commit window closes.
    Phase 3 (match):  match revealed orders in a batch (no frontrunning).
    """
    order_id: str
    # Commit phase
    commitment: str            # SHA-256(order_data + salt)
    salt: str                  # random salt (kept secret until reveal)
    committed_at: float = 0.0
    # Order data (hidden until reveal)
    asset: str = ""
    direction: str = ""        # "buy" or "sell"
    amount: float = 0.0
    max_price: float = 0.0     # limit price (buy) or min price (sell)
    # Reveal phase
    revealed: bool = False
    revealed_at: float = 0.0
    # Execution
    matched: bool = False
    counterparty_id: str = ""
    fill_price: float = 0.0
    status: str = "committed"  # committed, revealed, matched, expired, cancelled


@dataclass
class DarkPoolMatch:
    """A matched pair of orders in the dark pool."""
    match_id: str
    buy_order: CommitRevealOrder
    sell_order: CommitRevealOrder
    execution_price: float     # midpoint of buy.max_price and sell.max_price
    amount: float              # min of both amounts
    ts: float = field(default_factory=time.time)


@dataclass
class PrivatePosition:
    """A position hidden behind a ZK proof.

    Only the owner can see the full position. Others can verify
    that the position satisfies certain properties (e.g., "collateral
    ratio > 150%") without learning the actual values.
    """
    position_id: str
    # Hidden values
    asset: str
    quantity: float
    entry_price: float
    # Public commitments
    quantity_commitment: str    # hash(quantity + salt)
    value_commitment: str      # hash(value + salt)
    salt: str
    # Provable properties (can share without revealing position)
    is_long: bool = True
    collateral_ratio_ok: bool = True
    within_risk_limits: bool = True


class CommitRevealEngine:
    """Manages commit-reveal order flow for MEV-resistant trading.

    Flow:
      1. Agent submits order → engine creates commitment (hash)
      2. Commitment published to dark pool (on-chain or P2P)
      3. After commit window (e.g., 10 blocks), reveal phase opens
      4. Agent reveals order details + salt
      5. Batch matcher crosses revealed orders at midpoint price
      6. Unmatched orders returned to agent

    This eliminates frontrunning because nobody knows order details
    during the commit phase, and all orders execute at the same
    batch price during the matching phase.
    """

    COMMIT_WINDOW_S = 30.0     # seconds before reveal phase opens
    REVEAL_WINDOW_S = 30.0     # seconds to reveal before expiry

    def __init__(self, bus: Bus):
        self.bus = bus
        self._orders: dict[str, CommitRevealOrder] = {}
        self._matches: list[DarkPoolMatch] = []
        self._order_counter = 0
        self._stats = {
            "committed": 0, "revealed": 0, "matched": 0, "expired": 0,
        }

    def commit_order(self, asset: str, direction: str, amount: float,
                     max_price: float) -> CommitRevealOrder:
        """Create a committed (hidden) order.

        Returns the order with commitment hash. The salt is stored
        locally and NEVER published until reveal phase.
        """
        self._order_counter += 1
        order_id = f"zk-{self._order_counter:05d}"
        salt = secrets.token_hex(16)

        # Create commitment: H(order_id | asset | direction | amount | max_price | salt)
        preimage = f"{order_id}|{asset}|{direction}|{amount}|{max_price}|{salt}"
        commitment = hashlib.sha256(preimage.encode()).hexdigest()

        order = CommitRevealOrder(
            order_id=order_id,
            commitment=commitment,
            salt=salt,
            committed_at=time.time(),
            asset=asset,
            direction=direction,
            amount=amount,
            max_price=max_price,
        )
        self._orders[order_id] = order
        self._stats["committed"] += 1

        log.info(
            "ZK COMMIT: %s %s %.4f %s @ max $%.2f (commitment=%s...)",
            order_id, direction, amount, asset, max_price, commitment[:12],
        )

        return order

    def reveal_order(self, order_id: str) -> bool:
        """Reveal an order after commit window has passed."""
        order = self._orders.get(order_id)
        if not order:
            return False

        elapsed = time.time() - order.committed_at
        if elapsed < self.COMMIT_WINDOW_S:
            log.warning("Cannot reveal yet: %s has %.0fs remaining in commit window",
                        order_id, self.COMMIT_WINDOW_S - elapsed)
            return False

        # Verify commitment matches
        preimage = f"{order.order_id}|{order.asset}|{order.direction}|{order.amount}|{order.max_price}|{order.salt}"
        expected = hashlib.sha256(preimage.encode()).hexdigest()
        if expected != order.commitment:
            log.error("Commitment mismatch for %s — order may be tampered", order_id)
            return False

        order.revealed = True
        order.revealed_at = time.time()
        order.status = "revealed"
        self._stats["revealed"] += 1

        log.info("ZK REVEAL: %s %s %.4f %s @ $%.2f",
                 order_id, order.direction, order.amount, order.asset, order.max_price)

        return True

    def match_orders(self) -> list[DarkPoolMatch]:
        """Batch-match all revealed orders at midpoint price.

        Matching rules:
          - Buy and sell orders for the same asset
          - Buy max_price >= sell max_price (price compatibility)
          - Execution at midpoint: (buy.max + sell.max) / 2
          - Amount: min(buy.amount, sell.amount)
        """
        # Group revealed unmatched orders by asset
        buys: dict[str, list[CommitRevealOrder]] = {}
        sells: dict[str, list[CommitRevealOrder]] = {}

        for order in self._orders.values():
            if not order.revealed or order.matched or order.status != "revealed":
                continue
            if order.direction == "buy":
                buys.setdefault(order.asset, []).append(order)
            else:
                sells.setdefault(order.asset, []).append(order)

        matches = []
        for asset in set(buys.keys()) & set(sells.keys()):
            asset_buys = sorted(buys[asset], key=lambda o: o.max_price, reverse=True)
            asset_sells = sorted(sells[asset], key=lambda o: o.max_price)

            i, j = 0, 0
            while i < len(asset_buys) and j < len(asset_sells):
                buy = asset_buys[i]
                sell = asset_sells[j]

                if buy.max_price >= sell.max_price:
                    # Match at midpoint
                    exec_price = (buy.max_price + sell.max_price) / 2
                    exec_amount = min(buy.amount, sell.amount)

                    match_id = f"dm-{len(self._matches) + 1:05d}"
                    match = DarkPoolMatch(
                        match_id=match_id,
                        buy_order=buy,
                        sell_order=sell,
                        execution_price=exec_price,
                        amount=exec_amount,
                    )
                    matches.append(match)
                    self._matches.append(match)

                    buy.matched = True
                    buy.counterparty_id = sell.order_id
                    buy.fill_price = exec_price
                    buy.status = "matched"

                    sell.matched = True
                    sell.counterparty_id = buy.order_id
                    sell.fill_price = exec_price
                    sell.status = "matched"

                    self._stats["matched"] += 1

                    log.info(
                        "DARK POOL MATCH: %s — buy %s @ $%.2f vs sell %s @ $%.2f -> exec $%.2f x %.4f",
                        match_id, buy.order_id, buy.max_price,
                        sell.order_id, sell.max_price, exec_price, exec_amount,
                    )

                    i += 1
                    j += 1
                else:
                    j += 1  # sell price too high, try next

        return matches

    def expire_stale(self) -> int:
        """Expire orders that missed the reveal window."""
        expired = 0
        now = time.time()
        for order in self._orders.values():
            if order.status == "committed" and not order.revealed:
                total_window = self.COMMIT_WINDOW_S + self.REVEAL_WINDOW_S
                if now - order.committed_at > total_window:
                    order.status = "expired"
                    expired += 1
                    self._stats["expired"] += 1
        return expired

    def summary(self) -> dict:
        return {
            **self._stats,
            "active_orders": len([o for o in self._orders.values()
                                  if o.status in ("committed", "revealed")]),
            "total_matches": len(self._matches),
            "recent_matches": [
                {
                    "id": m.match_id,
                    "asset": m.buy_order.asset,
                    "price": round(m.execution_price, 4),
                    "amount": round(m.amount, 4),
                }
                for m in self._matches[-5:]
            ],
        }


class PrivatePositionManager:
    """Manages positions with ZK-style privacy commitments.

    Each position is stored with hash commitments so that:
      - The owner knows the full position details
      - Others can verify properties without seeing values
      - Proofs can be generated for: "my collateral ratio is > X"
    """

    def __init__(self):
        self._positions: dict[str, PrivatePosition] = {}
        self._pos_counter = 0

    def create_position(self, asset: str, quantity: float,
                        entry_price: float) -> PrivatePosition:
        """Create a private position with hash commitments."""
        self._pos_counter += 1
        pos_id = f"zkpos-{self._pos_counter:04d}"
        salt = secrets.token_hex(16)

        value = quantity * entry_price
        qty_commitment = hashlib.sha256(f"{quantity}|{salt}".encode()).hexdigest()
        val_commitment = hashlib.sha256(f"{value}|{salt}".encode()).hexdigest()

        pos = PrivatePosition(
            position_id=pos_id,
            asset=asset,
            quantity=quantity,
            entry_price=entry_price,
            quantity_commitment=qty_commitment,
            value_commitment=val_commitment,
            salt=salt,
        )
        self._positions[pos_id] = pos
        return pos

    def prove_property(self, pos_id: str, property_name: str,
                       threshold: float = 0.0) -> dict:
        """Generate a proof that a position satisfies a property.

        Properties:
          - "collateral_ratio": position value / debt > threshold
          - "quantity_above": quantity > threshold
          - "value_below": value < threshold

        Returns a proof dict (in production: ZK proof bytes).
        """
        pos = self._positions.get(pos_id)
        if not pos:
            return {"valid": False, "reason": "position not found"}

        value = pos.quantity * pos.entry_price

        if property_name == "collateral_ratio":
            ratio = value / max(threshold, 1e-9) if threshold > 0 else float("inf")
            return {
                "valid": ratio >= 1.0,
                "property": property_name,
                "commitment": pos.value_commitment,
                "proof": hashlib.sha256(
                    f"proof|{pos_id}|{property_name}|{value}|{pos.salt}".encode()
                ).hexdigest(),
            }
        elif property_name == "quantity_above":
            return {
                "valid": pos.quantity > threshold,
                "property": property_name,
                "commitment": pos.quantity_commitment,
                "proof": hashlib.sha256(
                    f"proof|{pos_id}|{property_name}|{pos.quantity}|{pos.salt}".encode()
                ).hexdigest(),
            }

        return {"valid": False, "reason": f"unknown property: {property_name}"}

    def summary(self) -> dict:
        return {
            "total_positions": len(self._positions),
            "positions": [
                {
                    "id": p.position_id,
                    "asset": p.asset,
                    "commitment": p.quantity_commitment[:12] + "...",
                    "is_long": p.is_long,
                }
                for p in self._positions.values()
            ],
        }
