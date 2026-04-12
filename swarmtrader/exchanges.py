"""Multi-exchange integration — unified interface for CEX trading.

Provides real API clients for Binance, Coinbase, and OKX alongside
the existing Kraken client. All exchanges implement a common interface
so the SOR and arbitrage engine can route across them.

Each exchange client handles:
  - Authentication (HMAC-SHA256/SHA512, API key headers)
  - Rate limiting (per-exchange rules)
  - Pair normalization (ETHUSDT → ETH/USDT → ETH)
  - Balance queries
  - Order submission + cancellation
  - Ticker/orderbook data

Usage:
    from swarmtrader.exchanges import get_exchange, list_exchanges
    binance = get_exchange("binance")
    await binance.connect()
    ticker = await binance.get_ticker("ETH")
    order = await binance.market_buy("ETH", quantity=0.1)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger("swarm.exchanges")


# ---------------------------------------------------------------------------
# Unified Exchange Interface
# ---------------------------------------------------------------------------

@dataclass
class Ticker:
    """Normalized ticker from any exchange."""
    exchange: str
    asset: str
    bid: float
    ask: float
    last: float
    volume_24h: float
    ts: float


@dataclass
class OrderResult:
    """Normalized order result from any exchange."""
    exchange: str
    order_id: str
    client_order_id: str
    status: str  # "filled", "partial", "open", "cancelled", "rejected"
    side: str
    asset: str
    quantity: float
    filled_qty: float
    avg_price: float
    fee: float
    fee_currency: str
    raw: dict = field(default_factory=dict)


@dataclass
class Balance:
    """Normalized balance entry."""
    asset: str
    free: float      # available for trading
    locked: float    # in open orders
    total: float     # free + locked


class ExchangeClient(ABC):
    """Base class for all exchange clients."""

    name: str = "unknown"
    fee_rate: float = 0.001  # default taker fee

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    @abstractmethod
    async def get_ticker(self, asset: str) -> Ticker:
        """Get current bid/ask/last for an asset (quoted in USD/USDT)."""

    @abstractmethod
    async def get_balances(self) -> list[Balance]:
        """Get all non-zero balances."""

    @abstractmethod
    async def market_buy(self, asset: str, quantity: float,
                         client_id: str = "") -> OrderResult:
        """Market buy a quantity of asset."""

    @abstractmethod
    async def market_sell(self, asset: str, quantity: float,
                          client_id: str = "") -> OrderResult:
        """Market sell a quantity of asset."""

    @abstractmethod
    async def cancel_all(self, asset: str | None = None) -> int:
        """Cancel all open orders. Returns count cancelled."""

    async def get_orderbook(self, asset: str, depth: int = 20) -> dict:
        """Get L2 orderbook. Override if supported."""
        return {"bids": [], "asks": []}

    def _to_pair(self, asset: str) -> str:
        """Convert asset symbol to exchange pair format. Override per exchange."""
        return f"{asset}USDT"


# ---------------------------------------------------------------------------
# Binance
# ---------------------------------------------------------------------------

class BinanceClient(ExchangeClient):
    """Binance Spot REST API client.

    Auth: HMAC-SHA256 signature on query string.
    Rate limit: 1200 requests/min (general), 10 orders/sec.
    Docs: https://binance-docs.github.io/apidocs/spot/en/
    """

    name = "binance"
    fee_rate = 0.0010  # 0.10% taker (VIP0)
    BASE = "https://api.binance.com"

    def __init__(self):
        super().__init__()
        self._api_key = os.getenv("BINANCE_API_KEY", "")
        self._api_secret = os.getenv("BINANCE_API_SECRET", "")
        self._sem = asyncio.Semaphore(10)  # max 10 concurrent requests

    def _sign(self, params: dict) -> str:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return hmac.new(
            self._api_secret.encode(), query.encode(), hashlib.sha256,
        ).hexdigest()

    async def _public(self, endpoint: str, params: dict | None = None) -> dict | list:
        async with self._sem:
            session = await self._ensure_session()
            url = f"{self.BASE}/api/v3/{endpoint}"
            async with session.get(url, params=params or {}) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Binance {endpoint} {resp.status}: {text[:200]}")
                return await resp.json()

    async def _private(self, endpoint: str, params: dict | None = None,
                       method: str = "GET") -> dict | list:
        if not self._api_key:
            raise RuntimeError("BINANCE_API_KEY not set")
        async with self._sem:
            session = await self._ensure_session()
            p = dict(params or {})
            p["timestamp"] = int(time.time() * 1000)
            p["recvWindow"] = 5000
            p["signature"] = self._sign(p)
            headers = {"X-MBX-APIKEY": self._api_key}
            url = f"{self.BASE}/api/v3/{endpoint}"
            if method == "GET":
                async with session.get(url, params=p, headers=headers) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise RuntimeError(f"Binance {endpoint} {resp.status}: {text[:200]}")
                    return await resp.json()
            elif method == "POST":
                async with session.post(url, params=p, headers=headers) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise RuntimeError(f"Binance {endpoint} {resp.status}: {text[:200]}")
                    return await resp.json()
            elif method == "DELETE":
                async with session.delete(url, params=p, headers=headers) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise RuntimeError(f"Binance {endpoint} {resp.status}: {text[:200]}")
                    return await resp.json()

    def _to_pair(self, asset: str) -> str:
        return f"{asset.upper()}USDT"

    async def get_ticker(self, asset: str) -> Ticker:
        pair = self._to_pair(asset)
        data = await self._public("ticker/bookTicker", {"symbol": pair})
        return Ticker(
            exchange=self.name, asset=asset.upper(),
            bid=float(data["bidPrice"]), ask=float(data["askPrice"]),
            last=(float(data["bidPrice"]) + float(data["askPrice"])) / 2,
            volume_24h=0, ts=time.time(),
        )

    async def get_orderbook(self, asset: str, depth: int = 20) -> dict:
        pair = self._to_pair(asset)
        data = await self._public("depth", {"symbol": pair, "limit": depth})
        return {
            "bids": [(float(p), float(q)) for p, q in data.get("bids", [])],
            "asks": [(float(p), float(q)) for p, q in data.get("asks", [])],
        }

    async def get_balances(self) -> list[Balance]:
        data = await self._private("account")
        balances = []
        for b in data.get("balances", []):
            free = float(b["free"])
            locked = float(b["locked"])
            if free + locked > 0:
                balances.append(Balance(b["asset"], free, locked, free + locked))
        return balances

    async def market_buy(self, asset: str, quantity: float,
                         client_id: str = "") -> OrderResult:
        return await self._order(asset, "BUY", quantity, client_id)

    async def market_sell(self, asset: str, quantity: float,
                          client_id: str = "") -> OrderResult:
        return await self._order(asset, "SELL", quantity, client_id)

    async def _order(self, asset: str, side: str, quantity: float,
                     client_id: str) -> OrderResult:
        pair = self._to_pair(asset)
        params: dict = {
            "symbol": pair,
            "side": side,
            "type": "MARKET",
            "quantity": f"{quantity:.8f}",
        }
        if client_id:
            params["newClientOrderId"] = client_id[:36]
        params["newOrderRespType"] = "FULL"

        data = await self._private("order", params, method="POST")

        fills = data.get("fills", [])
        total_qty = sum(float(f["qty"]) for f in fills) if fills else float(data.get("executedQty", 0))
        total_cost = sum(float(f["price"]) * float(f["qty"]) for f in fills) if fills else 0
        avg_price = total_cost / total_qty if total_qty > 0 else 0
        total_fee = sum(float(f.get("commission", 0)) for f in fills)

        return OrderResult(
            exchange=self.name, order_id=str(data.get("orderId", "")),
            client_order_id=data.get("clientOrderId", client_id),
            status="filled" if data.get("status") == "FILLED" else data.get("status", "").lower(),
            side=side.lower(), asset=asset.upper(),
            quantity=quantity, filled_qty=total_qty,
            avg_price=avg_price, fee=total_fee,
            fee_currency=fills[0].get("commissionAsset", "USDT") if fills else "USDT",
            raw=data,
        )

    async def cancel_all(self, asset: str | None = None) -> int:
        if not asset:
            return 0  # Binance requires symbol for cancel
        pair = self._to_pair(asset)
        try:
            result = await self._private("openOrders", {"symbol": pair}, method="DELETE")
            return len(result) if isinstance(result, list) else 1
        except RuntimeError:
            return 0


# ---------------------------------------------------------------------------
# Coinbase Advanced Trade
# ---------------------------------------------------------------------------

class CoinbaseClient(ExchangeClient):
    """Coinbase Advanced Trade API client.

    Auth: JWT or API key + HMAC-SHA256.
    Rate limit: 10 requests/sec per endpoint.
    Docs: https://docs.cdp.coinbase.com/advanced-trade/docs/
    """

    name = "coinbase"
    fee_rate = 0.0040  # 0.40% taker (starter)
    BASE = "https://api.coinbase.com"

    def __init__(self):
        super().__init__()
        self._api_key = os.getenv("COINBASE_API_KEY", "")
        self._api_secret = os.getenv("COINBASE_API_SECRET", "")
        self._sem = asyncio.Semaphore(8)

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        message = f"{timestamp}{method.upper()}{path}{body}"
        return hmac.new(
            self._api_secret.encode(), message.encode(), hashlib.sha256,
        ).hexdigest()

    async def _request(self, method: str, path: str,
                       body: dict | None = None) -> dict:
        if not self._api_key:
            raise RuntimeError("COINBASE_API_KEY not set")
        async with self._sem:
            session = await self._ensure_session()
            timestamp = str(int(time.time()))
            body_str = json.dumps(body) if body else ""
            sig = self._sign(timestamp, method, path, body_str)
            headers = {
                "CB-ACCESS-KEY": self._api_key,
                "CB-ACCESS-SIGN": sig,
                "CB-ACCESS-TIMESTAMP": timestamp,
                "Content-Type": "application/json",
            }
            url = f"{self.BASE}{path}"
            if method == "GET":
                async with session.get(url, headers=headers) as resp:
                    if resp.status not in (200, 201):
                        text = await resp.text()
                        raise RuntimeError(f"Coinbase {path} {resp.status}: {text[:200]}")
                    return await resp.json()
            else:
                async with session.post(url, headers=headers, data=body_str) as resp:
                    if resp.status not in (200, 201):
                        text = await resp.text()
                        raise RuntimeError(f"Coinbase {path} {resp.status}: {text[:200]}")
                    return await resp.json()

    def _to_pair(self, asset: str) -> str:
        return f"{asset.upper()}-USD"

    async def get_ticker(self, asset: str) -> Ticker:
        pair = self._to_pair(asset)
        data = await self._request("GET", f"/api/v3/brokerage/products/{pair}")
        product = data.get("product", data)
        return Ticker(
            exchange=self.name, asset=asset.upper(),
            bid=float(product.get("price", 0)) * 0.9999,
            ask=float(product.get("price", 0)) * 1.0001,
            last=float(product.get("price", 0)),
            volume_24h=float(product.get("volume_24h", 0)),
            ts=time.time(),
        )

    async def get_balances(self) -> list[Balance]:
        data = await self._request("GET", "/api/v3/brokerage/accounts")
        balances = []
        for acct in data.get("accounts", []):
            avail = float(acct.get("available_balance", {}).get("value", 0))
            hold = float(acct.get("hold", {}).get("value", 0))
            if avail + hold > 0:
                balances.append(Balance(
                    acct.get("currency", ""), avail, hold, avail + hold,
                ))
        return balances

    async def market_buy(self, asset: str, quantity: float,
                         client_id: str = "") -> OrderResult:
        return await self._order(asset, "BUY", quantity, client_id)

    async def market_sell(self, asset: str, quantity: float,
                          client_id: str = "") -> OrderResult:
        return await self._order(asset, "SELL", quantity, client_id)

    async def _order(self, asset: str, side: str, quantity: float,
                     client_id: str) -> OrderResult:
        pair = self._to_pair(asset)
        body = {
            "client_order_id": client_id or f"swarm_{int(time.time()*1000)}",
            "product_id": pair,
            "side": side,
            "order_configuration": {
                "market_market_ioc": {
                    "base_size": f"{quantity:.8f}",
                },
            },
        }
        data = await self._request("POST", "/api/v3/brokerage/orders", body)

        order = data.get("success_response", data)
        return OrderResult(
            exchange=self.name,
            order_id=order.get("order_id", ""),
            client_order_id=client_id,
            status="filled" if order.get("status") == "FILLED" else "submitted",
            side=side.lower(), asset=asset.upper(),
            quantity=quantity, filled_qty=quantity,
            avg_price=0,  # Coinbase doesn't return fill price in create response
            fee=quantity * self.fee_rate,
            fee_currency="USD",
            raw=data,
        )

    async def cancel_all(self, asset: str | None = None) -> int:
        try:
            data = await self._request("GET", "/api/v3/brokerage/orders/historical/batch",
                                       {"order_status": ["OPEN"]})
            order_ids = [o["order_id"] for o in data.get("orders", [])]
            if order_ids:
                await self._request("POST", "/api/v3/brokerage/orders/batch_cancel",
                                    {"order_ids": order_ids})
            return len(order_ids)
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# OKX
# ---------------------------------------------------------------------------

class OKXClient(ExchangeClient):
    """OKX REST API client.

    Auth: HMAC-SHA256 + base64 signature.
    Rate limit: 20 requests/2s per endpoint.
    Docs: https://www.okx.com/docs-v5/en/
    """

    name = "okx"
    fee_rate = 0.0008  # 0.08% taker (VIP0)
    BASE = "https://www.okx.com"

    def __init__(self):
        super().__init__()
        self._api_key = os.getenv("OKX_API_KEY", "")
        self._api_secret = os.getenv("OKX_API_SECRET", "")
        self._passphrase = os.getenv("OKX_PASSPHRASE", "")
        self._sem = asyncio.Semaphore(8)

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        message = f"{timestamp}{method.upper()}{path}{body}"
        mac = hmac.new(
            self._api_secret.encode(), message.encode(), hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode()

    async def _request(self, method: str, path: str,
                       body: dict | None = None, public: bool = False) -> dict:
        async with self._sem:
            session = await self._ensure_session()
            url = f"{self.BASE}{path}"
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
            body_str = json.dumps(body) if body else ""
            headers: dict = {"Content-Type": "application/json"}

            if not public:
                if not self._api_key:
                    raise RuntimeError("OKX_API_KEY not set")
                sig = self._sign(timestamp, method, path, body_str)
                headers.update({
                    "OK-ACCESS-KEY": self._api_key,
                    "OK-ACCESS-SIGN": sig,
                    "OK-ACCESS-TIMESTAMP": timestamp,
                    "OK-ACCESS-PASSPHRASE": self._passphrase,
                })

            if method == "GET":
                async with session.get(url, headers=headers) as resp:
                    data = await resp.json()
            else:
                async with session.post(url, headers=headers, data=body_str) as resp:
                    data = await resp.json()

            if data.get("code") != "0" and data.get("code") is not None:
                raise RuntimeError(f"OKX {path}: {data.get('msg', data)}")
            return data

    def _to_pair(self, asset: str) -> str:
        return f"{asset.upper()}-USDT"

    async def get_ticker(self, asset: str) -> Ticker:
        pair = self._to_pair(asset)
        data = await self._request("GET", f"/api/v5/market/ticker?instId={pair}",
                                   public=True)
        t = data.get("data", [{}])[0]
        return Ticker(
            exchange=self.name, asset=asset.upper(),
            bid=float(t.get("bidPx", 0)), ask=float(t.get("askPx", 0)),
            last=float(t.get("last", 0)),
            volume_24h=float(t.get("vol24h", 0)),
            ts=time.time(),
        )

    async def get_orderbook(self, asset: str, depth: int = 20) -> dict:
        pair = self._to_pair(asset)
        data = await self._request(
            "GET", f"/api/v5/market/books?instId={pair}&sz={depth}", public=True,
        )
        book = data.get("data", [{}])[0]
        return {
            "bids": [(float(p), float(q)) for p, q, *_ in book.get("bids", [])],
            "asks": [(float(p), float(q)) for p, q, *_ in book.get("asks", [])],
        }

    async def get_balances(self) -> list[Balance]:
        data = await self._request("GET", "/api/v5/account/balance")
        balances = []
        for detail in data.get("data", [{}])[0].get("details", []):
            avail = float(detail.get("availBal", 0))
            frozen = float(detail.get("frozenBal", 0))
            if avail + frozen > 0:
                balances.append(Balance(detail["ccy"], avail, frozen, avail + frozen))
        return balances

    async def market_buy(self, asset: str, quantity: float,
                         client_id: str = "") -> OrderResult:
        return await self._order(asset, "buy", quantity, client_id)

    async def market_sell(self, asset: str, quantity: float,
                          client_id: str = "") -> OrderResult:
        return await self._order(asset, "sell", quantity, client_id)

    async def _order(self, asset: str, side: str, quantity: float,
                     client_id: str) -> OrderResult:
        pair = self._to_pair(asset)
        body = {
            "instId": pair,
            "tdMode": "cash",
            "side": side,
            "ordType": "market",
            "sz": f"{quantity:.8f}",
        }
        if client_id:
            body["clOrdId"] = client_id[:32]

        data = await self._request("POST", "/api/v5/trade/order", body)
        order = data.get("data", [{}])[0]

        return OrderResult(
            exchange=self.name,
            order_id=order.get("ordId", ""),
            client_order_id=client_id,
            status="submitted",
            side=side.lower(), asset=asset.upper(),
            quantity=quantity, filled_qty=0,
            avg_price=0, fee=0, fee_currency="USDT",
            raw=data,
        )

    async def cancel_all(self, asset: str | None = None) -> int:
        try:
            data = await self._request("GET", "/api/v5/trade/orders-pending")
            orders = data.get("data", [])
            count = 0
            for order in orders:
                if asset and not order.get("instId", "").startswith(asset.upper()):
                    continue
                await self._request("POST", "/api/v5/trade/cancel-order", {
                    "instId": order["instId"], "ordId": order["ordId"],
                })
                count += 1
            return count
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# Bybit
# ---------------------------------------------------------------------------

class BybitClient(ExchangeClient):
    """Bybit V5 REST API client.

    Auth: HMAC-SHA256 signature.
    Rate limit: 120 requests/5s.
    Docs: https://bybit-exchange.github.io/docs/v5/
    """

    name = "bybit"
    fee_rate = 0.0010  # 0.10% taker (VIP0)
    BASE = "https://api.bybit.com"

    def __init__(self):
        super().__init__()
        self._api_key = os.getenv("BYBIT_API_KEY", "")
        self._api_secret = os.getenv("BYBIT_API_SECRET", "")
        self._sem = asyncio.Semaphore(10)

    def _sign(self, timestamp: str, params_str: str) -> str:
        payload = f"{timestamp}{self._api_key}5000{params_str}"
        return hmac.new(
            self._api_secret.encode(), payload.encode(), hashlib.sha256,
        ).hexdigest()

    async def _request(self, method: str, path: str,
                       params: dict | None = None, public: bool = False) -> dict:
        async with self._sem:
            session = await self._ensure_session()
            url = f"{self.BASE}{path}"
            timestamp = str(int(time.time() * 1000))
            headers: dict = {"Content-Type": "application/json"}

            if not public:
                if not self._api_key:
                    raise RuntimeError("BYBIT_API_KEY not set")
                if method == "GET":
                    param_str = "&".join(f"{k}={v}" for k, v in (params or {}).items())
                else:
                    param_str = json.dumps(params or {})
                sig = self._sign(timestamp, param_str)
                headers.update({
                    "X-BAPI-API-KEY": self._api_key,
                    "X-BAPI-SIGN": sig,
                    "X-BAPI-TIMESTAMP": timestamp,
                    "X-BAPI-RECV-WINDOW": "5000",
                })

            if method == "GET":
                async with session.get(url, params=params, headers=headers) as resp:
                    data = await resp.json()
            else:
                async with session.post(url, headers=headers,
                                        data=json.dumps(params or {})) as resp:
                    data = await resp.json()

            if data.get("retCode", 0) != 0:
                raise RuntimeError(f"Bybit {path}: {data.get('retMsg', data)}")
            return data

    def _to_pair(self, asset: str) -> str:
        return f"{asset.upper()}USDT"

    async def get_ticker(self, asset: str) -> Ticker:
        pair = self._to_pair(asset)
        data = await self._request("GET", "/v5/market/tickers",
                                   {"category": "spot", "symbol": pair}, public=True)
        t = data.get("result", {}).get("list", [{}])[0]
        return Ticker(
            exchange=self.name, asset=asset.upper(),
            bid=float(t.get("bid1Price", 0)), ask=float(t.get("ask1Price", 0)),
            last=float(t.get("lastPrice", 0)),
            volume_24h=float(t.get("volume24h", 0)),
            ts=time.time(),
        )

    async def get_orderbook(self, asset: str, depth: int = 20) -> dict:
        pair = self._to_pair(asset)
        data = await self._request("GET", "/v5/market/orderbook",
                                   {"category": "spot", "symbol": pair, "limit": depth},
                                   public=True)
        book = data.get("result", {})
        return {
            "bids": [(float(p), float(q)) for p, q in book.get("b", [])],
            "asks": [(float(p), float(q)) for p, q in book.get("a", [])],
        }

    async def get_balances(self) -> list[Balance]:
        data = await self._request("GET", "/v5/account/wallet-balance",
                                   {"accountType": "UNIFIED"})
        balances = []
        for acct in data.get("result", {}).get("list", []):
            for coin in acct.get("coin", []):
                free = float(coin.get("availableToWithdraw", 0))
                locked = float(coin.get("locked", 0))
                if free + locked > 0:
                    balances.append(Balance(coin["coin"], free, locked, free + locked))
        return balances

    async def market_buy(self, asset: str, quantity: float,
                         client_id: str = "") -> OrderResult:
        return await self._order(asset, "Buy", quantity, client_id)

    async def market_sell(self, asset: str, quantity: float,
                          client_id: str = "") -> OrderResult:
        return await self._order(asset, "Sell", quantity, client_id)

    async def _order(self, asset: str, side: str, quantity: float,
                     client_id: str) -> OrderResult:
        pair = self._to_pair(asset)
        params = {
            "category": "spot",
            "symbol": pair,
            "side": side,
            "orderType": "Market",
            "qty": f"{quantity:.8f}",
        }
        if client_id:
            params["orderLinkId"] = client_id[:36]

        data = await self._request("POST", "/v5/order/create", params)
        result = data.get("result", {})

        return OrderResult(
            exchange=self.name,
            order_id=result.get("orderId", ""),
            client_order_id=client_id,
            status="submitted",
            side=side.lower(), asset=asset.upper(),
            quantity=quantity, filled_qty=0,
            avg_price=0, fee=0, fee_currency="USDT",
            raw=data,
        )

    async def cancel_all(self, asset: str | None = None) -> int:
        try:
            params: dict = {"category": "spot"}
            if asset:
                params["symbol"] = self._to_pair(asset)
            data = await self._request("POST", "/v5/order/cancel-all", params)
            return len(data.get("result", {}).get("list", []))
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[ExchangeClient]] = {
    "binance": BinanceClient,
    "coinbase": CoinbaseClient,
    "okx": OKXClient,
    "bybit": BybitClient,
}

_instances: dict[str, ExchangeClient] = {}


def get_exchange(name: str) -> ExchangeClient:
    """Get or create a singleton exchange client."""
    if name not in _instances:
        cls = _REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"Unknown exchange: {name}. Available: {list(_REGISTRY)}")
        _instances[name] = cls()
    return _instances[name]


def list_exchanges() -> list[str]:
    """List all available exchange names."""
    return list(_REGISTRY.keys())


async def get_all_tickers(asset: str) -> list[Ticker]:
    """Fetch ticker from all configured exchanges concurrently."""
    tasks = []
    for name in _REGISTRY:
        client = get_exchange(name)
        tasks.append(_safe_ticker(client, asset))
    results = await asyncio.gather(*tasks)
    return [t for t in results if t is not None]


async def _safe_ticker(client: ExchangeClient, asset: str) -> Ticker | None:
    try:
        return await client.get_ticker(asset)
    except Exception as e:
        log.debug("Ticker %s/%s failed: %s", client.name, asset, e)
        return None
