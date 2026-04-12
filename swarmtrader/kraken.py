"""Kraken CLI integration: live market data scout and trade executor."""
from __future__ import annotations
import asyncio, json, logging, os, time
from pathlib import Path
from .core import Bus, MarketSnapshot, TradeIntent, ExecutionReport

log = logging.getLogger("swarm.kraken")


async def _run_cli(*args: str) -> dict | list | str:
    """Run a kraken CLI command and return parsed JSON output."""
    cmd = ["kraken", "-o", "json", *args]
    env = dict(os.environ)
    api_key = os.getenv("KRAKEN_API_KEY")
    api_secret = os.getenv("KRAKEN_PRIVATE_KEY")
    if api_key:
        cmd.extend(["--api-key", api_key])
    if api_secret:
        cmd.extend(["--api-secret", api_secret])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err_msg = stderr.decode().strip()
        log.error("kraken CLI error: %s (cmd: %s)", err_msg, " ".join(args))
        raise RuntimeError(f"kraken CLI failed: {err_msg}")
    raw = stdout.decode().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


# ---------------------------------------------------------------------------
# Market Data Scout — polls Kraken REST ticker
# ---------------------------------------------------------------------------
class KrakenScout:
    """Polls Kraken ticker via CLI for real market prices."""

    def __init__(self, bus: Bus, pairs: list[str] | None = None,
                 interval: float = 2.0):
        self.bus = bus
        self.pairs = pairs or ["ETHUSD"]
        self.interval = interval
        self._stop = False

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("KrakenScout starting: pairs=%s interval=%.1fs", self.pairs, self.interval)
        while not self._stop:
            try:
                data = await _run_cli("ticker", *self.pairs)
                prices = {}
                for pair_key, info in data.items():
                    # Kraken returns ask/bid arrays: [price, whole_lot_vol, lot_vol]
                    ask = float(info["a"][0])
                    bid = float(info["b"][0])
                    mid = (ask + bid) / 2.0
                    # Normalize pair name: XETHZUSD -> ETH, XXBTZUSD -> BTC
                    symbol = self._normalize_pair(pair_key)
                    prices[symbol] = mid

                snap = MarketSnapshot(
                    ts=time.time(),
                    prices=prices,
                    gas_gwei=0.0,  # not applicable for CEX
                )
                await self.bus.publish("market.snapshot", snap)
                log.debug("tick: %s", {k: f"{v:.2f}" for k, v in prices.items()})

            except Exception as e:
                log.warning("KrakenScout tick failed: %s", e)

            await asyncio.sleep(self.interval)

    @staticmethod
    def _normalize_pair(pair_key: str) -> str:
        """Convert Kraken pair names to simple symbols.
        XETHZUSD -> ETH, XXBTZUSD -> BTC, SOLUSD -> SOL, etc."""
        p = pair_key.upper()
        # Strip leading X and trailing Z+quote
        for quote in ("ZUSD", "USD", "ZUSDT", "USDT"):
            if p.endswith(quote):
                base = p[: -len(quote)]
                break
        else:
            return p  # fallback: return as-is
        # Strip legacy Kraken X-prefix
        if base.startswith("X") and len(base) == 4:
            base = base[1:]
        # Map Kraken names
        renames = {"XBT": "BTC"}
        return renames.get(base, base)


# ---------------------------------------------------------------------------
# WebSocket Scout — streams live trades for lower latency
# ---------------------------------------------------------------------------
class KrakenWSScout:
    """Streams live ticker via `kraken ws ticker` for sub-second updates."""

    def __init__(self, bus: Bus, pairs: list[str] | None = None):
        self.bus = bus
        self.pairs = pairs or ["ETH/USD"]
        self._stop = False
        self._proc: asyncio.subprocess.Process | None = None

    def stop(self):
        self._stop = True
        if self._proc:
            self._proc.terminate()

    async def run(self):
        cmd = ["kraken", "-o", "json", "ws", "ticker", *self.pairs]
        log.info("KrakenWSScout starting: %s", " ".join(cmd))

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._proc.stdout is not None
        while not self._stop:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                data = json.loads(line.decode().strip())
                prices = {}
                # WS ticker format: {"channel":"ticker","data":[{"symbol":"ETH/USD",...}]}
                for tick in data.get("data", [data]):
                    symbol = tick.get("symbol", "").split("/")[0]
                    ask = float(tick.get("ask", tick.get("a", [0])))
                    bid = float(tick.get("bid", tick.get("b", [0])))
                    if isinstance(ask, list):
                        ask = float(ask[0])
                    if isinstance(bid, list):
                        bid = float(bid[0])
                    if ask and bid:
                        prices[symbol] = (ask + bid) / 2.0

                if prices:
                    snap = MarketSnapshot(ts=time.time(), prices=prices, gas_gwei=0.0)
                    await self.bus.publish("market.snapshot", snap)
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                log.debug("WS parse skip: %s", e)

        if self._proc.returncode is None:
            self._proc.terminate()


# ---------------------------------------------------------------------------
# Trade Executor — paper or live via Kraken CLI
# ---------------------------------------------------------------------------
class KrakenExecutor:
    """Executes trades via Kraken CLI (paper or live mode)."""

    def __init__(self, bus: Bus, kill_switch: Path, paper: bool = True):
        self.bus = bus
        self.kill_switch = kill_switch
        self.paper = paper
        bus.subscribe("exec.simulated", self._on_sim)

    async def _on_sim(self, payload):
        intent, eff_price = payload
        if self.kill_switch.exists():
            await self._report(intent, "rejected", None, None, None, "kill_switch")
            return
        if eff_price is None:
            await self._report(intent, "rejected", None, None, None, "no_quote")
            return
        if time.time() > intent.ttl:
            await self._report(intent, "expired", None, None, None, "ttl")
            return

        try:
            result = await self._submit(intent, eff_price)
            await self._report(
                intent, "filled",
                result.get("txid", result.get("order_id", "paper")),
                eff_price,
                0.001,
                f"{'paper' if self.paper else 'live'}"
            )
        except Exception as e:
            log.error("Execution failed: %s", e)
            await self._report(intent, "error", None, None, None, str(e))

    async def _submit(self, intent: TradeIntent, eff_price: float) -> dict:
        """Submit order via Kraken CLI."""
        going_long = intent.asset_out in ("ETH", "BTC", "SOL")
        pair = self._to_kraken_pair(intent.asset_in, intent.asset_out)

        if going_long:
            # Buying: volume is how much of the base asset to buy
            volume = intent.amount_in / eff_price
            side = "buy"
        else:
            # Selling: volume is how much of the base asset to sell
            volume = intent.amount_in
            side = "sell"

        volume_str = f"{volume:.6f}"

        if self.paper:
            result = await _run_cli("paper", side, pair, volume_str, "--type", "market")
        else:
            result = await _run_cli("order", side, pair, volume_str, "--type", "market", "--yes")

        log.info("ORDER %s %s %s %s -> %s", side, pair, volume_str,
                 "PAPER" if self.paper else "LIVE", result)
        return result if isinstance(result, dict) else {"txid": str(result)}

    @staticmethod
    def _to_kraken_pair(asset_in: str, asset_out: str) -> str:
        """Convert our internal asset names to Kraken pair format."""
        renames = {"BTC": "XBT"}
        assets = {asset_in, asset_out}
        # Figure out which is the quote (USD/USDC/USDT)
        quotes = {"USD", "USDC", "USDT"}
        base_assets = assets - quotes
        quote_assets = assets & quotes
        if not base_assets or not quote_assets:
            # Fallback
            return f"{asset_in}{asset_out}"
        base = renames.get(list(base_assets)[0], list(base_assets)[0])
        quote = list(quote_assets)[0]
        return f"{base}{quote}"

    async def _report(self, intent, status, tx, price, slip, note):
        edge = sum(s.strength * s.confidence for s in intent.supporting) / max(1, len(intent.supporting))
        sign = 1 if intent.asset_out in ("ETH", "BTC", "SOL") else -1
        pnl = sign * edge * intent.amount_in * 0.001
        rep = ExecutionReport(intent.id, status, tx, price, slip, pnl, note)
        await self.bus.publish("exec.report", rep)
        contribs = {s.agent_id: s.strength * s.confidence * sign for s in intent.supporting}
        await self.bus.publish("audit.attribution", {"pnl": pnl, "contribs": contribs})
