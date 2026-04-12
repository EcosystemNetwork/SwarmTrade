"""State checkpointing for crash recovery.

Periodically serializes portfolio positions, agent weights, ML model state,
and wallet balances to a JSON file. On restart, the swarm can resume from
the last checkpoint instead of starting fresh.
"""
from __future__ import annotations
import asyncio, json, logging, time
from pathlib import Path
from .core import Bus, PortfolioTracker

log = logging.getLogger("swarm.checkpoint")


class Checkpoint:
    """Periodic state serializer with atomic writes."""

    def __init__(self, bus: Bus, portfolio: PortfolioTracker,
                 state: dict, path: Path = Path("swarm_checkpoint.json"),
                 interval: float = 30.0, wallet=None, strategist=None):
        self.bus = bus
        self.portfolio = portfolio
        self.state = state
        self.path = path
        self.interval = interval
        self.wallet = wallet
        self.strategist = strategist
        self._stop = False
        self._last_save = 0.0

    def stop(self):
        self._stop = True

    async def run(self):
        """Periodically save checkpoint."""
        while not self._stop:
            try:
                self.save()
            except Exception as e:
                log.error("Checkpoint save failed: %s", e)
            await asyncio.sleep(self.interval)

    def save(self):
        """Atomic write: write to tmp then rename."""
        data = {
            "ts": time.time(),
            "version": 1,
            "state": {
                "daily_pnl": self.state.get("daily_pnl", 0.0),
                "trade_count": self.state.get("trade_count", 0),
                "total_fees": self.state.get("total_fees", 0.0),
            },
            "portfolio": self._serialize_portfolio(),
        }
        if self.wallet:
            data["wallet"] = self.wallet.summary()
        if self.strategist and hasattr(self.strategist, "weights"):
            data["strategist_weights"] = dict(self.strategist.weights)

        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.rename(self.path)
        self._last_save = time.time()
        log.debug("Checkpoint saved: %d positions, pnl=%.4f",
                  len(self.portfolio.positions), self.state.get("daily_pnl", 0))

    def _serialize_portfolio(self) -> dict:
        positions = {}
        for asset, pos in self.portfolio.positions.items():
            if pos.quantity > 1e-9 or abs(pos.realized_pnl) > 1e-9:
                positions[asset] = {
                    "quantity": pos.quantity,
                    "cost_basis": pos.cost_basis,
                    "realized_pnl": pos.realized_pnl,
                    "total_fees": pos.total_fees,
                    "trade_count": pos.trade_count,
                }
        return {
            "positions": positions,
            "last_prices": dict(self.portfolio.last_prices),
        }

    def restore(self) -> bool:
        """Load checkpoint and restore state. Returns True if restored."""
        if not self.path.exists():
            log.info("No checkpoint file found at %s", self.path)
            return False

        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Checkpoint file corrupt: %s", e)
            return False

        age = time.time() - data.get("ts", 0)
        if age > 86400:  # Older than 24h — stale
            log.info("Checkpoint too old (%.0fh), starting fresh", age / 3600)
            return False

        # Restore state
        saved_state = data.get("state", {})
        self.state["daily_pnl"] = saved_state.get("daily_pnl", 0.0)
        self.state["trade_count"] = saved_state.get("trade_count", 0)
        self.state["total_fees"] = saved_state.get("total_fees", 0.0)

        # Restore portfolio positions
        port_data = data.get("portfolio", {})
        for asset, pdata in port_data.get("positions", {}).items():
            pos = self.portfolio.get(asset)
            pos.quantity = pdata.get("quantity", 0.0)
            pos.cost_basis = pdata.get("cost_basis", 0.0)
            pos.realized_pnl = pdata.get("realized_pnl", 0.0)
            pos.total_fees = pdata.get("total_fees", 0.0)
            pos.trade_count = pdata.get("trade_count", 0)
        self.portfolio.last_prices.update(port_data.get("last_prices", {}))

        # Restore strategist weights
        if self.strategist and hasattr(self.strategist, "weights"):
            saved_weights = data.get("strategist_weights", {})
            if saved_weights:
                self.strategist.weights.update(saved_weights)
                log.info("Restored %d strategist weights", len(saved_weights))

        log.info("Checkpoint restored: age=%.0fs, pnl=%.4f, %d positions",
                 age, self.state["daily_pnl"], len(port_data.get("positions", {})))
        return True
