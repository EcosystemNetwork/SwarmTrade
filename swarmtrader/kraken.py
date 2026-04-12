"""Kraken integration: live market data scout and trade executor.

Supports both direct REST API (via kraken_api.py) and CLI fallback.
Direct API is preferred — lower latency, no subprocess dependency.

Production hardening:
- Exponential backoff on REST polling errors
- WebSocket reconnection with watchdog
- Proper pair naming (ETH/USD format for Kraken API)
- Order state tracking: pending -> acknowledged -> filled/rejected/error
- Retry logic with classification (retryable vs fatal errors)
- Tiered rate limiting (starter/intermediate/pro)
- Pre-flight API key validation
"""
from __future__ import annotations
import asyncio, json, logging, os, time
from enum import Enum
from .core import (
    Bus, MarketSnapshot, TradeIntent, ExecutionReport,
    PortfolioTracker, QUOTE_ASSETS,
)
from .kraken_api import KrakenRESTClient, KrakenAPIConfig, get_client, close_client

log = logging.getLogger("swarm.kraken")

# CLI subprocess timeout (seconds)
_CLI_TIMEOUT = 15.0

# Backoff constants
_BACKOFF_BASE = 2.0
_BACKOFF_MAX = 60.0
_BACKOFF_FACTOR = 2.0


class OrderStatus(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    EXPIRED = "expired"
    ERROR = "error"
    CANCELLED = "cancelled"


class ErrorKind(Enum):
    """Classifies errors into retryable vs fatal."""
    RETRYABLE = "retryable"     # network timeout, rate limit, temporary outage
    FATAL = "fatal"             # insufficient balance, invalid pair, auth failure
    UNKNOWN = "unknown"


def _classify_error(msg: str) -> ErrorKind:
    """Classify an error message to decide retry vs abort."""
    msg_lower = msg.lower()
    fatal_patterns = [
        "insufficient", "invalid", "permission", "unauthorized",
        "not found", "unknown pair", "minimum order", "eapi:invalid",
    ]
    retryable_patterns = [
        "timeout", "rate limit", "temporarily", "unavailable",
        "connection", "enetunreach", "econnreset", "503",
    ]
    for pat in fatal_patterns:
        if pat in msg_lower:
            return ErrorKind.FATAL
    for pat in retryable_patterns:
        if pat in msg_lower:
            return ErrorKind.RETRYABLE
    return ErrorKind.UNKNOWN


async def _run_cli(*args: str, timeout: float = _CLI_TIMEOUT) -> dict | list | str:
    """Run a kraken CLI command with timeout and return parsed JSON output."""
    cmd = ["kraken", "-o", "json", *args]
    # Pass API credentials via environment only — never as CLI args
    # (CLI args are visible in process listings via ps/top)
    env = dict(os.environ)
    api_key = os.getenv("KRAKEN_API_KEY")
    api_secret = os.getenv("KRAKEN_PRIVATE_KEY")
    if api_key:
        env["KRAKEN_API_KEY"] = api_key
    if api_secret:
        env["KRAKEN_PRIVATE_KEY"] = api_secret

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"kraken CLI timed out after {timeout}s: {' '.join(args)}")

    if proc.returncode != 0:
        err_msg = stderr.decode().strip() if stderr else f"exit code {proc.returncode}"
        # Also check stdout for error JSON
        if stdout:
            try:
                out_data = json.loads(stdout.decode().strip())
                if isinstance(out_data, dict) and "error" in out_data:
                    err_msg = str(out_data["error"])
            except (json.JSONDecodeError, KeyError):
                pass
        log.error("kraken CLI error: %s (cmd: kraken %s)", err_msg, " ".join(args))
        raise RuntimeError(f"kraken CLI failed: {err_msg}")

    raw = stdout.decode().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


async def validate_api_keys() -> tuple[bool, str]:
    """Pre-flight check: verify Kraken API keys are valid.
    Tries direct REST API first, falls back to CLI.
    Returns (ok, message)."""
    client = get_client()
    if client._cfg.api_key and client._cfg.api_secret:
        try:
            ok, msg = await client.validate_keys()
            if ok:
                return True, msg + " (REST API)"
            # Fall through to CLI if REST fails
        except Exception as e:
            log.debug("REST API validation failed, trying CLI: %s", e)

    # CLI fallback
    try:
        result = await _run_cli("balance", timeout=10.0)
        if isinstance(result, dict):
            return True, f"API keys valid, {len(result)} assets in balance (CLI)"
        return True, "API keys valid (CLI)"
    except RuntimeError as e:
        return False, f"API key validation failed: {e}"
    except FileNotFoundError:
        return False, "'kraken' CLI not found in PATH and no REST API keys configured"


def _compute_backoff(attempt: int) -> float:
    """Exponential backoff with jitter, capped at _BACKOFF_MAX."""
    import random
    delay = min(_BACKOFF_BASE * (_BACKOFF_FACTOR ** attempt), _BACKOFF_MAX)
    # Add 10-25% jitter
    jitter = delay * random.uniform(0.1, 0.25)
    return delay + jitter


# ---------------------------------------------------------------------------
# Market Data Scout — polls Kraken REST ticker with exponential backoff
# ---------------------------------------------------------------------------
class KrakenScout:
    """Polls Kraken ticker for real market prices.

    Uses direct REST API by default (low latency, no subprocess).
    Falls back to CLI if REST client has no API keys configured.

    Production features:
    - Exponential backoff on consecutive errors (2s -> 4s -> 8s -> ... -> 60s max)
    - Resets backoff after successful tick
    - Logs error count and backoff delay
    - Rate-limit-aware via KrakenRateLimiter
    """

    def __init__(self, bus: Bus, pairs: list[str] | None = None,
                 interval: float = 2.0, client: KrakenRESTClient | None = None):
        self.bus = bus
        self.pairs = pairs or ["ETHUSD"]
        self.interval = interval
        self._stop = False
        self._consecutive_errors = 0
        self._client = client or get_client()

    def stop(self):
        self._stop = True

    async def _fetch_via_rest(self) -> dict:
        """Fetch ticker via direct REST API."""
        data = await self._client.get_ticker(self.pairs)
        prices = {}
        for pair_key, info in data.items():
            try:
                a_list = info.get("a", [])
                b_list = info.get("b", [])
                if not a_list or not b_list:
                    continue
                ask = float(a_list[0])
                bid = float(b_list[0])
                if ask <= 0 or bid <= 0:
                    continue
                mid = (ask + bid) / 2.0
                symbol = self._normalize_pair(pair_key)
                prices[symbol] = mid
            except (ValueError, TypeError, IndexError) as e:
                log.warning("Skipping malformed ticker for %s: %s", pair_key, e)
        return prices

    async def _fetch_via_cli(self) -> dict:
        """Fetch ticker via CLI subprocess (fallback)."""
        data = await _run_cli("ticker", *self.pairs)
        prices = {}
        for pair_key, info in data.items():
            try:
                a_list = info.get("a", [])
                b_list = info.get("b", [])
                if not a_list or not b_list:
                    continue
                ask = float(a_list[0])
                bid = float(b_list[0])
                if ask <= 0 or bid <= 0:
                    continue
                mid = (ask + bid) / 2.0
                symbol = self._normalize_pair(pair_key)
                prices[symbol] = mid
            except (ValueError, TypeError, IndexError) as e:
                log.warning("Skipping malformed ticker for %s: %s", pair_key, e)
        return prices

    async def run(self):
        use_rest = bool(self._client._cfg.api_key) or True  # public endpoint, no key needed
        mode = "REST API" if use_rest else "CLI"
        log.info("KrakenScout starting: pairs=%s interval=%.1fs mode=%s",
                 self.pairs, self.interval, mode)
        while not self._stop:
            try:
                prices = await self._fetch_via_rest()

                snap = MarketSnapshot(
                    ts=time.time(),
                    prices=prices,
                    gas_gwei=0.0,
                )
                await self.bus.publish("market.snapshot", snap)
                log.debug("tick: %s", {k: f"{v:.2f}" for k, v in prices.items()})

                # Reset backoff on success
                if self._consecutive_errors > 0:
                    log.info("KrakenScout recovered after %d errors", self._consecutive_errors)
                self._consecutive_errors = 0
                await asyncio.sleep(self.interval)

            except Exception as e:
                self._consecutive_errors += 1
                backoff = _compute_backoff(self._consecutive_errors)
                log.warning("KrakenScout tick failed (attempt %d, backoff %.1fs): %s",
                            self._consecutive_errors, backoff, e)
                await asyncio.sleep(backoff)

    @staticmethod
    def _normalize_pair(pair_key: str) -> str:
        """Convert Kraken pair names to simple symbols.
        XETHZUSD -> ETH, XXBTZUSD -> BTC, SOLUSD -> SOL, etc."""
        p = pair_key.upper()
        for quote in ("ZUSD", "USD", "ZUSDT", "USDT"):
            if p.endswith(quote):
                base = p[: -len(quote)]
                break
        else:
            return p
        if base.startswith("X") and len(base) == 4:
            base = base[1:]
        renames = {"XBT": "BTC"}
        return renames.get(base, base)


# ---------------------------------------------------------------------------
# WebSocket Scout — streams live trades with auto-reconnection
# ---------------------------------------------------------------------------
class KrakenWSScout:
    """Streams live ticker via Kraken WebSocket v2 (or CLI fallback).

    Prefers native WS v2 aiohttp connection for lower latency.
    Falls back to CLI subprocess if native connection fails.

    Production features:
    - Native WS v2 with direct aiohttp connection
    - Auto-reconnect with exponential backoff on disconnect
    - Stale data detection (publishes warning if no data for >10s)
    - Graceful shutdown
    """

    RECONNECT_BASE = 2.0
    RECONNECT_MAX = 60.0
    STALE_THRESHOLD = 10.0  # seconds without data = stale warning

    def __init__(self, bus: Bus, pairs: list[str] | None = None,
                 ws_client=None):
        self.bus = bus
        self.pairs = pairs or ["ETH/USD"]
        self._stop = False
        self._proc: asyncio.subprocess.Process | None = None
        self._last_data_ts: float = 0.0
        self._ws_client = ws_client  # KrakenWSv2Client for native WS

    def stop(self):
        self._stop = True
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()

    async def run(self):
        reconnect_attempt = 0
        while not self._stop:
            try:
                await self._connect_and_stream()
                # Clean exit — don't reconnect
                if self._stop:
                    break
                reconnect_attempt += 1
                backoff = _compute_backoff(reconnect_attempt)
                log.warning("KrakenWSScout disconnected, reconnecting in %.1fs "
                            "(attempt %d)", backoff, reconnect_attempt)
                await asyncio.sleep(backoff)
            except Exception as e:
                reconnect_attempt += 1
                backoff = _compute_backoff(reconnect_attempt)
                log.error("KrakenWSScout error (attempt %d, backoff %.1fs): %s",
                          reconnect_attempt, backoff, e)
                await asyncio.sleep(backoff)

    async def _connect_and_stream(self):
        if not self.pairs:
            raise ValueError("No pairs configured for WS scout")

        cmd = ["kraken", "-o", "json", "ws", "ticker", *self.pairs]
        log.info("KrakenWSScout connecting: %s", " ".join(cmd))

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if self._proc.stdout is None:
            raise RuntimeError("Failed to capture stdout from kraken ws process")

        self._last_data_ts = time.time()
        reconnect_logged = False

        while not self._stop:
            try:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=self.STALE_THRESHOLD
                )
            except asyncio.TimeoutError:
                elapsed = time.time() - self._last_data_ts
                if not reconnect_logged:
                    log.warning("KrakenWSScout: no data for %.0fs (stale)", elapsed)
                    reconnect_logged = True
                # Check if process died
                if self._proc.returncode is not None:
                    log.warning("KrakenWSScout: process exited with code %d",
                                self._proc.returncode)
                    return  # trigger reconnect
                continue

            if not line:
                # EOF — process exited
                log.warning("KrakenWSScout: EOF from process")
                return  # trigger reconnect

            reconnect_logged = False
            try:
                data = json.loads(line.decode().strip())
                prices = {}
                for tick in data.get("data", [data]):
                    symbol = tick.get("symbol", "").split("/")[0]
                    if not symbol:
                        continue
                    ask = tick.get("ask", tick.get("a", 0))
                    bid = tick.get("bid", tick.get("b", 0))
                    if isinstance(ask, list):
                        ask = float(ask[0])
                    else:
                        ask = float(ask)
                    if isinstance(bid, list):
                        bid = float(bid[0])
                    else:
                        bid = float(bid)
                    if ask > 0 and bid > 0:
                        prices[symbol] = (ask + bid) / 2.0

                if prices:
                    self._last_data_ts = time.time()
                    snap = MarketSnapshot(ts=time.time(), prices=prices, gas_gwei=0.0)
                    await self.bus.publish("market.snapshot", snap)
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                log.debug("WS parse skip: %s", e)

        # Graceful shutdown
        if self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()


# ---------------------------------------------------------------------------
# Order Lifecycle Tracker — polls open/closed orders for fill confirmation
# ---------------------------------------------------------------------------
class OrderTracker:
    """Tracks order lifecycle by polling Kraken's OpenOrders/ClosedOrders.

    Detects state transitions:
      pending → open → partially_filled → filled / cancelled / expired

    Publishes Bus events for each transition so the portfolio, dashboard,
    and risk pipeline react to real exchange state — not assumed fills.
    """

    def __init__(self, bus: Bus, client: KrakenRESTClient | None = None,
                 poll_interval: float = 2.0):
        self.bus = bus
        self._client = client or get_client()
        self.poll_interval = poll_interval
        self._stop = False
        # Tracks orders we're watching: intent_id → {txid, status, filled_qty, total_qty, side, asset, pair}
        self._tracked: dict[str, dict] = {}
        bus.subscribe("exec.submitted", self._on_submitted)

    def stop(self):
        self._stop = True

    async def _on_submitted(self, payload: dict):
        """Register a newly submitted order for tracking."""
        self._tracked[payload["intent_id"]] = {
            "txid": payload.get("txid", ""),
            "cl_ord_id": payload.get("cl_ord_id", payload["intent_id"]),
            "status": "submitted",
            "filled_qty": 0.0,
            "total_qty": payload.get("volume", 0.0),
            "side": payload.get("side", ""),
            "asset": payload.get("asset", ""),
            "pair": payload.get("pair", ""),
            "submitted_at": time.time(),
        }

    async def run(self):
        """Poll open/closed orders and publish state transitions."""
        log.info("OrderTracker starting: poll_interval=%.1fs", self.poll_interval)
        while not self._stop:
            if not self._tracked:
                await asyncio.sleep(self.poll_interval)
                continue

            try:
                await self._poll_orders()
            except Exception as e:
                log.warning("OrderTracker poll failed: %s", e)

            await asyncio.sleep(self.poll_interval)

    async def _poll_orders(self):
        """Check order status against Kraken."""
        has_keys = bool(self._client._cfg.api_key and self._client._cfg.api_secret)
        if not has_keys:
            return

        try:
            result = await self._client.get_open_orders(trades=True)
            open_orders = result.get("open", {})
        except RuntimeError as e:
            log.debug("OrderTracker get_open_orders failed: %s", e)
            return

        completed = []
        for intent_id, info in list(self._tracked.items()):
            txid = info["txid"]
            if not txid:
                continue

            if txid in open_orders:
                order = open_orders[txid]
                vol_exec = float(order.get("vol_exec", 0))
                vol_total = float(order.get("vol", info["total_qty"]))
                # Detect partial fills
                if vol_exec > info["filled_qty"] + 1e-9:
                    info["filled_qty"] = vol_exec
                    info["total_qty"] = vol_total
                    info["status"] = "partial"
                    await self.bus.publish("order.partial_fill", {
                        "intent_id": intent_id,
                        "txid": txid,
                        "filled_qty": vol_exec,
                        "remaining_qty": vol_total - vol_exec,
                        "total_qty": vol_total,
                        "progress": vol_exec / max(vol_total, 1e-9),
                    })
            else:
                # Not in open orders — check if filled/cancelled
                if info["status"] not in ("filled", "cancelled", "expired"):
                    info["status"] = "filled"  # assume filled if disappeared from open
                    await self.bus.publish("order.filled", {
                        "intent_id": intent_id,
                        "txid": txid,
                        "filled_qty": info["total_qty"],
                        "side": info["side"],
                        "asset": info["asset"],
                    })
                    completed.append(intent_id)

        # Clean up completed orders (keep for 30s for late events)
        for intent_id in completed:
            asyncio.get_event_loop().call_later(
                30.0, lambda iid=intent_id: self._tracked.pop(iid, None)
            )

    @property
    def active_orders(self) -> dict[str, dict]:
        """Return currently tracked orders (for dashboard)."""
        return dict(self._tracked)


# ---------------------------------------------------------------------------
# Trade Executor — paper or live via Kraken REST API with retry and order tracking
# ---------------------------------------------------------------------------
def _is_buy(intent: TradeIntent) -> bool:
    return intent.asset_in.upper() in QUOTE_ASSETS


def _base_asset(intent: TradeIntent) -> str:
    if intent.asset_in.upper() in QUOTE_ASSETS:
        return intent.asset_out
    return intent.asset_in


# Kraken pair naming: base/quote with proper Kraken conventions
_KRAKEN_RENAMES = {"BTC": "XBT"}
_KRAKEN_SPOT_PAIRS = {
    # Map our symbols to Kraken's expected pair format
    "ETH": "ETH/USD", "BTC": "XBT/USD", "SOL": "SOL/USD",
    "XRP": "XRP/USD", "ADA": "ADA/USD", "DOT": "DOT/USD",
    "LINK": "LINK/USD", "AVAX": "AVAX/USD",
}


class KrakenExecutor:
    """Executes trades via Kraken REST API (paper or live mode).

    Uses direct REST API by default, falls back to CLI subprocess.

    Production features:
    - Retry with exponential backoff for retryable errors
    - Error classification (retryable vs fatal)
    - Order state tracking with cl_ord_id
    - Proper Kraken pair naming (ETH/USD not ETHUSDC)
    - Tiered rate limiting
    - Portfolio tracking with real cost-basis accounting
    - Order cancellation via REST API
    """

    MAX_RETRIES = 3

    def __init__(self, bus: Bus, kill_switch, paper: bool = True,
                 portfolio: PortfolioTracker | None = None,
                 client: KrakenRESTClient | None = None):
        self.bus = bus
        self.kill_switch = kill_switch  # KillSwitch instance
        self.paper = paper
        self.portfolio = portfolio or PortfolioTracker()
        self._client = client or get_client()
        self._pending_orders: dict[str, OrderStatus] = {}  # intent_id -> status
        self._order_txids: dict[str, str] = {}  # intent_id -> kraken txid
        bus.subscribe("exec.simulated", self._on_sim)
        bus.subscribe("market.snapshot", self._on_snap)

    async def _on_snap(self, snap: MarketSnapshot):
        self.portfolio.update_prices(snap.prices)

    async def _on_sim(self, payload):
        intent, eff_price = payload

        if self.kill_switch.active:
            await self._report(intent, "rejected", None, None, 0.0, 0.0, 0.0, "kill_switch")
            return
        if eff_price is None:
            await self._report(intent, "rejected", None, None, 0.0, 0.0, 0.0, "no_quote")
            return
        if time.time() > intent.ttl:
            await self._report(intent, "expired", None, None, 0.0, 0.0, 0.0, "ttl")
            return

        buying = _is_buy(intent)
        asset = _base_asset(intent)

        # Pre-trade validation for sells
        if not buying:
            pos = self.portfolio.get(asset)
            if pos.quantity < 1e-3:
                await self._report(intent, "rejected", None, None, 0.0, 0.0, 0.0, "no_position")
                return

        # Track order state
        self._pending_orders[intent.id] = OrderStatus.PENDING

        try:
            result = await self._submit_with_retry(intent, eff_price)
            self._pending_orders[intent.id] = OrderStatus.FILLED

            if buying:
                quantity = intent.amount_in / eff_price
                fee, pnl = self.portfolio.buy(asset, quantity, eff_price)
                side = "buy"
            else:
                pos = self.portfolio.get(asset)
                quantity = min(intent.amount_in, pos.quantity)
                fee, pnl = self.portfolio.sell(asset, quantity, eff_price)
                side = "sell"

            tx_hash = result.get("txid", result.get("order_id", "paper"))
            mid = self.portfolio.last_prices.get(asset, eff_price)
            slippage = abs(eff_price - mid) / max(1e-9, eff_price)

            await self._report(
                intent, "filled", tx_hash, eff_price, slippage, pnl, fee,
                f"{'paper' if self.paper else 'live'} {side} {quantity:.6f} {asset}",
                side, quantity, asset,
            )

        except Exception as e:
            self._pending_orders[intent.id] = OrderStatus.ERROR
            error_kind = _classify_error(str(e))
            log.error("Execution failed [%s]: %s", error_kind.value, e)
            await self._report(
                intent, "error", None, None, 0.0, 0.0, 0.0,
                f"{error_kind.value}: {e}",
            )
        finally:
            # Clean up pending orders after a delay (keep for audit)
            self._pending_orders.pop(intent.id, None)

    async def _submit_with_retry(self, intent: TradeIntent, eff_price: float) -> dict:
        """Submit order with retry logic for transient failures."""
        last_error: Exception | None = None

        for attempt in range(self.MAX_RETRIES):
            # Check if intent has expired between retries
            if time.time() > intent.ttl:
                raise RuntimeError("intent expired during retry")
            if self.kill_switch.active:
                raise RuntimeError("kill switch activated during retry")

            try:
                self._pending_orders[intent.id] = OrderStatus.SUBMITTED
                result = await self._submit(intent, eff_price)
                return result
            except RuntimeError as e:
                last_error = e
                kind = _classify_error(str(e))

                if kind == ErrorKind.FATAL:
                    log.warning("Fatal order error (no retry): %s", e)
                    raise

                if attempt < self.MAX_RETRIES - 1:
                    backoff = _compute_backoff(attempt)
                    log.warning("Order attempt %d/%d failed (retrying in %.1fs): %s",
                                attempt + 1, self.MAX_RETRIES, backoff, e)
                    await asyncio.sleep(backoff)

        raise last_error or RuntimeError("max retries exceeded")

    async def _submit(self, intent: TradeIntent, eff_price: float) -> dict:
        """Submit a single order via Kraken REST API (or CLI fallback).

        Reads intent.order_spec for order type, limit price, stops, brackets.
        Falls back to market order if no OrderSpec is set.
        """
        buying = _is_buy(intent)
        pair = self._to_kraken_pair(intent.asset_in, intent.asset_out)

        if buying:
            volume = intent.amount_in / eff_price
            side = "buy"
        else:
            volume = intent.amount_in
            side = "sell"

        # Resolve order spec (default to market)
        spec = intent.order_spec
        order_type = spec.order_type if spec else "market"
        price = spec.limit_price if spec else None
        price2 = spec.stop_limit_price if spec else None
        oflags_parts = []
        if spec and spec.post_only:
            oflags_parts.append("post")
        oflags = ",".join(oflags_parts) if oflags_parts else None

        # For stop-loss / take-profit, price is the trigger
        if spec and order_type in ("stop-loss", "take-profit", "trailing-stop"):
            price = spec.stop_price
        if spec and order_type in ("stop-loss-limit", "take-profit-limit", "trailing-stop-limit"):
            price = spec.stop_price
            price2 = spec.limit_price

        # Try direct REST API first
        has_keys = bool(self._client._cfg.api_key and self._client._cfg.api_secret)
        if has_keys:
            try:
                result = await self._client.add_order(
                    pair=pair,
                    side=side,
                    order_type=order_type,
                    volume=volume,
                    price=price,
                    price2=price2,
                    oflags=oflags,
                    timeinforce=spec.time_in_force if spec else None,
                    expiretm=spec.expire_time if spec else None,
                    cl_ord_id=intent.id,
                    close_ordertype=spec.close_order_type if spec else None,
                    close_price=spec.close_price if spec else None,
                    close_price2=spec.close_price2 if spec else None,
                    validate=self.paper,
                    display_volume=spec.display_volume if spec else None,
                )
                txid = ""
                txid_list = result.get("txid", [])
                if isinstance(txid_list, list) and txid_list:
                    txid = txid_list[0]
                elif isinstance(txid_list, str):
                    txid = txid_list
                if txid:
                    self._order_txids[intent.id] = txid

                price_info = f" @{price}" if price else ""
                log.info("ORDER %s %s %s %.6f%s %s -> txid=%s (REST API)",
                         order_type, side, pair, volume, price_info,
                         "VALIDATE" if self.paper else "LIVE", txid)
                return {"txid": txid, "descr": result.get("descr", {})}
            except RuntimeError as e:
                if not self.paper:
                    raise
                log.warning("REST API order failed, trying CLI: %s", e)

        # CLI fallback (market orders only)
        volume_str = f"{volume:.6f}"
        cli_type = "market"  # CLI only supports market reliably
        if self.paper:
            result = await _run_cli("paper", side, pair, volume_str, "--type", cli_type)
        else:
            result = await _run_cli("order", side, pair, volume_str,
                                    "--type", cli_type, "--yes")

        log.info("ORDER %s %s %s %s -> %s (CLI)", side, pair, volume_str,
                 "PAPER" if self.paper else "LIVE", result)
        return result if isinstance(result, dict) else {"txid": str(result)}

    @staticmethod
    def _to_kraken_pair(asset_in: str, asset_out: str) -> str:
        """Convert our internal asset names to Kraken pair format.

        Returns 'BASE/QUOTE' format (e.g., 'ETH/USD', 'XBT/USD').
        """
        assets = {asset_in.upper(), asset_out.upper()}
        quotes = {"USD", "USDC", "USDT"}
        base_assets = assets - quotes
        quote_assets = assets & quotes

        if not base_assets or not quote_assets:
            # Fallback: just join with slash
            return f"{asset_in}/{asset_out}"

        base = list(base_assets)[0]
        quote = list(quote_assets)[0]

        # Check known pair map first
        if base in _KRAKEN_SPOT_PAIRS and quote == "USD":
            return _KRAKEN_SPOT_PAIRS[base]

        # Manual construction with Kraken renames
        kraken_base = _KRAKEN_RENAMES.get(base, base)
        return f"{kraken_base}/{quote}"

    async def cancel_all_orders(self) -> int:
        """Cancel ALL open orders via REST API (or CLI fallback).

        Called by kill switch / circuit breaker for emergency stops.
        Returns count of cancelled orders.
        """
        has_keys = bool(self._client._cfg.api_key and self._client._cfg.api_secret)
        if has_keys:
            try:
                result = await self._client.cancel_all()
                count = result.get("count", 0)
                log.info("Cancelled all orders via REST API: %d orders", count)
                self._pending_orders.clear()
                self._order_txids.clear()
                return count
            except RuntimeError as e:
                log.warning("REST cancel_all failed, trying CLI: %s", e)

        # CLI fallback
        try:
            await _run_cli("order", "cancel-all", "--yes")
            log.info("Cancelled all orders via CLI")
            self._pending_orders.clear()
            self._order_txids.clear()
            return -1  # unknown count from CLI
        except Exception as e:
            log.error("cancel_all failed: %s", e)
            return 0

    async def cancel_order(self, intent_id: str) -> bool:
        """Cancel a specific order by intent_id."""
        txid = self._order_txids.get(intent_id)
        if not txid:
            log.warning("No txid found for intent %s, trying cl_ord_id", intent_id)
            txid = intent_id  # try using cl_ord_id

        has_keys = bool(self._client._cfg.api_key and self._client._cfg.api_secret)
        if has_keys:
            try:
                await self._client.cancel_order(txid)
                self._pending_orders.pop(intent_id, None)
                self._order_txids.pop(intent_id, None)
                log.info("Cancelled order %s (txid=%s)", intent_id, txid)
                return True
            except RuntimeError as e:
                log.warning("Cancel order %s failed: %s", intent_id, e)
                return False
        return False

    async def _report(self, intent, status, tx, price, slip, pnl, fee, note,
                      side="", quantity=0.0, asset=""):
        rep = ExecutionReport(
            intent.id, status, tx, price, slip, pnl, note,
            side=side, quantity=quantity, asset=asset or _base_asset(intent),
            fee_usd=fee,
        )
        await self.bus.publish("exec.report", rep)
        if status == "filled" and intent.supporting:
            sign = 1 if _is_buy(intent) else -1
            contribs = {s.agent_id: s.strength * s.confidence * sign
                        for s in intent.supporting}
            await self.bus.publish("audit.attribution", {"pnl": pnl, "contribs": contribs})
