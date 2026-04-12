"""Web dashboard — aiohttp server with WebSocket for real-time Bus events.

Security: All API and WebSocket endpoints require a bearer token set via
SWARM_DASHBOARD_TOKEN env var (or auto-generated on startup).
Static assets (CSS/JS) are unauthenticated.
"""
from __future__ import annotations
import asyncio, hmac, json, logging, math, os, secrets, time
from pathlib import Path
from aiohttp import web
from .core import Bus, MarketSnapshot, Signal, TradeIntent, RiskVerdict, ExecutionReport
from .report import load_from_db, generate_html_report
from .gateway import AgentGateway, ConnectedAgent
from .social_trading import SocialTradingEngine
from .swarm_network import SwarmNetwork
from .metrics import MetricsCollector

log = logging.getLogger("swarm.web")

# ── Helpers ────────────────────────────────────────────────────────

def _generate_token() -> str:
    """Generate a cryptographically random dashboard token."""
    return secrets.token_urlsafe(32)


def _safe_float(value, *, default: float = 0.0, min_val: float | None = None,
                max_val: float | None = None) -> float | None:
    """Parse a float safely, rejecting NaN/Inf and enforcing bounds.

    Returns None if the value is invalid.
    """
    try:
        f = float(value)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(f):
        return None
    if min_val is not None and f < min_val:
        return None
    if max_val is not None and f > max_val:
        return None
    return f


# ── API Rate Limiter ──────────────────────────────────────────────
class _APIRateLimiter:
    """Simple per-IP token bucket rate limiter for HTTP API endpoints."""
    def __init__(self, requests_per_minute: int = 60):
        self._rpm = requests_per_minute
        self._buckets: dict[str, list[float]] = {}

    def check(self, ip: str) -> bool:
        """Returns True if request is allowed, False if rate-limited."""
        now = time.time()
        window = self._buckets.setdefault(ip, [])
        # Prune old entries
        cutoff = now - 60.0
        self._buckets[ip] = window = [t for t in window if t > cutoff]
        if len(window) >= self._rpm:
            return False
        window.append(now)
        return True

    def cleanup(self):
        """Remove stale IPs (call periodically)."""
        now = time.time()
        cutoff = now - 120.0
        stale = [ip for ip, ts in self._buckets.items() if not ts or ts[-1] < cutoff]
        for ip in stale:
            del self._buckets[ip]

_api_limiter = _APIRateLimiter(requests_per_minute=60)
_api_critical_limiter = _APIRateLimiter(requests_per_minute=10)  # emergency controls

# Paths with stricter rate limits (emergency controls, wallet ops)
_CRITICAL_PATHS = frozenset({
    "/api/cancel-all", "/api/flatten", "/api/pause",
    "/api/wallet/deposit", "/api/wallet/withdraw",
})


@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    """Add security headers to all responses."""
    response = await handler(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-XSS-Protection", "1; mode=block")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none';"
    )
    return response


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Require Bearer token for all API/WS endpoints.

    Unauthenticated paths: static assets, health check.
    Gateway paths use their own API key auth (handled by gateway handlers).
    """
    path = request.path

    # Allow static assets, health check, and HTML pages without auth
    if (path.startswith("/static/") or path == "/health"
            or path == "/" or path == "/slides" or path == "/report"
            or (path.startswith("/api/social/") and request.method == "GET")):
        return await handler(request)

    # Gateway endpoints authenticate via their own API key mechanism
    # (handled inside gateway.handle_signal, handle_market, etc.)
    if (path.startswith("/api/gateway/signal") or
            path.startswith("/api/gateway/market") or
            path.startswith("/api/gateway/portfolio") or
            path.startswith("/api/gateway/agents") or
            path.startswith("/api/gateway/registry") or
            path.startswith("/api/gateway/disconnect") or
            path.startswith("/api/gateway/chat") or
            path == "/ws/agent"):
        return await handler(request)

    token = request.app.get("_dashboard_token")
    if not token:
        # No token configured — auth disabled (dev mode)
        return await handler(request)

    # Check Authorization header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        provided = auth_header[7:]
        if hmac.compare_digest(provided, token):
            return await handler(request)

    # Check query param (for WebSocket connections from browsers)
    provided = request.query.get("token", "")
    if provided and hmac.compare_digest(provided, token):
        return await handler(request)

    return web.json_response(
        {"error": "unauthorized", "hint": "Set Authorization: Bearer <token> header"},
        status=401,
    )


@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    """Rate limit API endpoints. Static assets and WebSocket are exempt."""
    path = request.path
    if not path.startswith("/api/"):
        return await handler(request)
    ip = request.remote or "unknown"
    limiter = _api_critical_limiter if path in _CRITICAL_PATHS else _api_limiter
    if not limiter.check(ip):
        return web.json_response(
            {"error": "rate limit exceeded", "retry_after": 60},
            status=429,
        )
    return await handler(request)


class WebDashboard:
    """Serves the frontend and streams Bus events over WebSocket."""

    # Rate-limit: max WebSocket commands per client per minute
    _CMD_RATE_LIMIT = 60
    _CMD_WINDOW_SECS = 60.0
    # Max concurrent dashboard WebSocket connections
    _MAX_WS_CLIENTS = 100
    # Max inbound message size (64KB — signals and commands are small JSON)
    _WS_MAX_MSG_SIZE = 64 * 1024

    def __init__(self, bus: Bus, state: dict, db=None,
                 kill_switch=None, host: str = "127.0.0.1", port: int = 8080,
                 wallet=None, gateway: AgentGateway | None = None,
                 strategist=None, capital_allocator=None,
                 memory=None, erc8004=None, uniswap=None,
                 social=None, network=None, metrics=None):
        self.bus = bus
        self.state = state
        self.db = db  # Database instance
        self.kill_switch = kill_switch
        self.host = host
        self.port = port
        self.wallet = wallet  # WalletManager instance (optional)
        self.gateway = gateway  # AgentGateway instance (optional)
        self.memory = memory  # AgentMemory instance (optional)
        self.erc8004 = erc8004  # ERC8004Pipeline instance (optional)
        self.uniswap = uniswap  # UniswapExecutor instance (optional)
        self.strategist = strategist  # Strategist instance (for NLP config)
        self.capital_allocator = capital_allocator  # CapitalAllocator (leaderboard)
        self.social = social  # SocialTradingEngine instance (optional)
        self.network = network  # SwarmNetwork instance (optional)
        self.metrics = metrics  # MetricsCollector instance (optional)
        self._privacy_mgr = None  # lazy init
        self._pyth_oracle = None  # lazy init
        self._clients: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None

        # Per-client rate limit tracking: ws -> list of timestamps
        self._cmd_times: dict[web.WebSocketResponse, list[float]] = {}

        # Cached state for new clients
        self.prices: dict[str, float] = {}
        self.signals: dict[str, dict] = {}
        self.agents: dict[str, dict] = {}
        self.recent_intents: list[dict] = []
        self.recent_reports: list[dict] = []
        self.verdicts: dict[str, list[dict]] = {}
        self.trade_count = 0
        self.wins = 0
        self.losses = 0
        self.start_time = time.time()

        # Subscribe to all bus topics
        bus.subscribe("market.snapshot", self._on_snapshot)
        for topic in ("signal.momentum", "signal.mean_rev", "signal.vol",
                       "signal.prism", "signal.orderbook", "signal.funding",
                       "signal.spread", "signal.regime",
                       "signal.prism_volume", "signal.prism_breakout",
                       "signal.news", "signal.confluence",
                       "signal.whale", "signal.correlation",
                       "signal.rsi", "signal.macd", "signal.bollinger",
                       "signal.vwap", "signal.ichimoku", "signal.mtf",
                       "signal.liquidation", "signal.atr_stop",
                       "signal.ml", "signal.hedge", "signal.rebalance"):
            bus.subscribe(topic, self._on_signal)
        # New quantitative system events
        bus.subscribe("risk.var", self._on_var_update)
        bus.subscribe("pnl.attribution", self._on_pnl_attribution)
        bus.subscribe("sor.routed", self._on_sor_routed)
        bus.subscribe("tca.update", self._on_tca_update)
        bus.subscribe("compliance.wash_warning", self._on_compliance_alert)
        bus.subscribe("compliance.margin_warning", self._on_compliance_alert)
        bus.subscribe("data.quality_alert", self._on_data_quality)
        bus.subscribe("intent.new", self._on_intent)
        bus.subscribe("risk.verdict", self._on_verdict)
        bus.subscribe("exec.report", self._on_report)
        bus.subscribe("wallet.update", self._on_wallet_update)
        bus.subscribe("wallet.low_funds", self._on_wallet_low_funds)
        bus.subscribe("wallet.rebalance", self._on_wallet_rebalance)
        bus.subscribe("memory.thought", self._on_thought)
        bus.subscribe("social.feed", self._on_social_feed)
        bus.subscribe("network.post", self._on_network_post)
        bus.subscribe("network.comment", self._on_network_comment)
        bus.subscribe("network.feed_update", self._on_network_feed_update)

        self._init_agents()

    def _gateway_snapshot(self) -> dict:
        """Build gateway state for WS snapshot."""
        from .gateway import AGENT_TYPE_REGISTRY, AGENT_CATEGORIES, AGENT_CAPABILITIES
        if not self.gateway:
            return {"enabled": False, "agents": [],
                    "types": {}, "categories": {}, "capabilities": []}
        return {
            "enabled": True,
            "master_key_configured": True,
            "agents": [
                {
                    "agent_id": a.agent_id,
                    "asn": a.asn,
                    "name": a.name,
                    "agent_type": a.agent_type,
                    "agent_type_label": AGENT_TYPE_REGISTRY.get(a.agent_type, {}).get("label", a.agent_type),
                    "category": AGENT_TYPE_REGISTRY.get(a.agent_type, {}).get("category", ""),
                    "protocol": a.protocol,
                    "status": a.status,
                    "description": a.description,
                    "signal_count": a.signal_count,
                    "weight": round(a.weight, 4),
                    "capabilities": a.capabilities,
                    "connected_at": a.connected_at,
                }
                for a in self.gateway.agents.values()
            ],
            "types": AGENT_TYPE_REGISTRY,
            "categories": AGENT_CATEGORIES,
            "capabilities": AGENT_CAPABILITIES,
        }

    def _init_agents(self):
        """Pre-populate agent list."""
        agent_defs = [
            ("DataScout", "scout", "market.snapshot"),
            ("Momentum", "analyst", "signal.momentum"),
            ("MeanReversion", "analyst", "signal.mean_rev"),
            ("Volatility", "analyst", "signal.vol"),
            ("PRISM_AI", "analyst", "signal.prism"),
            ("Strategist", "strategy", "intent.new"),
            ("RiskAgent_Size", "risk", "risk.verdict"),
            ("RiskAgent_Allowlist", "risk", "risk.verdict"),
            ("RiskAgent_Drawdown", "risk", "risk.verdict"),
            ("Coordinator", "coordinator", "exec.go"),
            ("Simulator", "execution", "exec.simulated"),
            ("Executor", "execution", "exec.report"),
            ("RiskAgent_Funds", "risk", "risk.verdict"),
            ("RiskAgent_Allocation", "risk", "risk.verdict"),
            ("Auditor", "audit", "audit.log"),
            ("WalletManager", "wallet", "wallet.update"),
            ("News", "external", "signal.news"),
            ("Whale", "external", "signal.whale"),
            ("Confluence", "meta", "signal.confluence"),
            ("Correlation", "analyst", "signal.correlation"),
            ("RSI", "ta", "signal.rsi"),
            ("MACD", "ta", "signal.macd"),
            ("Bollinger", "ta", "signal.bollinger"),
            ("VWAP", "ta", "signal.vwap"),
            ("Ichimoku", "ta", "signal.ichimoku"),
            ("MTF", "analyst", "signal.mtf"),
            ("Liquidation", "research", "signal.liquidation"),
            ("ATR_Stop", "research", "signal.atr_stop"),
            ("RiskAgent_Depth", "risk", "risk.verdict"),
            # Citadel-grade quantitative agents
            ("ML_Signal", "ml", "signal.ml"),
            ("DynamicHedger", "portfolio", "signal.hedge"),
            ("PortfolioOpt", "portfolio", "signal.rebalance"),
            ("VaR_Engine", "quant_risk", "risk.var"),
            ("StressTester", "quant_risk", "risk.stress"),
            ("SmartOrderRouter", "execution", "sor.routed"),
            ("IcebergExecutor", "execution", "exec.iceberg"),
            ("TCA_Tracker", "execution", "tca.update"),
            ("FactorModel", "quant", "pnl.attribution"),
            ("PnL_Attributor", "quant", "pnl.attribution"),
            ("WashTradeDetector", "compliance", "compliance.wash_warning"),
            ("MarginMonitor", "compliance", "compliance.margin_warning"),
            ("DataQuality", "infra", "data.quality_alert"),
            ("Reconciler", "infra", "reconciliation"),
            # Additional risk agents
            ("RiskAgent_VaR", "risk", "risk.verdict"),
            ("RiskAgent_Stress", "risk", "risk.verdict"),
            ("RiskAgent_Compliance", "risk", "risk.verdict"),
            ("RiskAgent_FactorExposure", "risk", "risk.verdict"),
            ("RiskAgent_Rebalance", "risk", "risk.verdict"),
            ("RiskAgent_SOR", "risk", "risk.verdict"),
            # Hermes LLM brain
            ("HermesBrain", "strategy", "signal.hermes"),
        ]
        for name, role, topic in agent_defs:
            self.agents[name] = {
                "name": name, "role": role, "topic": topic,
                "status": "idle", "last_tick": None, "ticks": 0,
            }

    def _set_agent_active(self, name: str):
        if name in self.agents:
            self.agents[name]["status"] = "active"
            self.agents[name]["last_tick"] = time.time()
            self.agents[name]["ticks"] += 1

    # ── Broadcasting ───────────────────────────────────────────────

    async def _broadcast(self, event_type: str, data: dict):
        """Send a message to all connected WebSocket clients concurrently."""
        msg = json.dumps({"type": event_type, "ts": time.time(), "data": data})
        clients = list(self._clients)  # snapshot to avoid mutation during iteration
        if not clients:
            return

        async def _send(ws: web.WebSocketResponse):
            try:
                await asyncio.wait_for(ws.send_str(msg), timeout=5.0)
            except Exception:
                self._clients.discard(ws)
                self._cmd_times.pop(ws, None)

        await asyncio.gather(*[_send(ws) for ws in clients])

    async def _send_to(self, ws: web.WebSocketResponse, event_type: str, data: dict):
        """Send a message to a single WebSocket client."""
        msg = json.dumps({"type": event_type, "ts": time.time(), "data": data})
        try:
            await asyncio.wait_for(ws.send_str(msg), timeout=5.0)
        except Exception:
            self._clients.discard(ws)
            self._cmd_times.pop(ws, None)

    # ── Rate limiting ──────────────────────────────────────────────

    def _check_rate_limit(self, ws: web.WebSocketResponse) -> bool:
        """Return True if the command is allowed, False if rate-limited."""
        now = time.time()
        times = self._cmd_times.setdefault(ws, [])
        cutoff = now - self._CMD_WINDOW_SECS
        # Prune old entries
        self._cmd_times[ws] = times = [t for t in times if t > cutoff]
        if len(times) >= self._CMD_RATE_LIMIT:
            return False
        times.append(now)
        return True

    # ── Bus event handlers ─────────────────────────────────────────

    async def _on_snapshot(self, snap: MarketSnapshot):
        self.prices.update(snap.prices)
        self._set_agent_active("DataScout")
        await self._broadcast("market", {
            "prices": snap.prices, "gas_gwei": snap.gas_gwei,
        })

    async def _on_signal(self, sig: Signal):
        d = {
            "agent_id": sig.agent_id, "asset": sig.asset,
            "direction": sig.direction, "strength": round(sig.strength, 4),
            "confidence": round(sig.confidence, 4), "rationale": sig.rationale,
        }
        self.signals[sig.agent_id] = d

        agent_map = {
            "momentum": "Momentum", "mean_rev": "MeanReversion",
            "vol": "Volatility", "prism": "PRISM_AI",
            "news": "News", "whale": "Whale", "confluence": "Confluence",
            "correlation": "Correlation", "rsi": "RSI", "macd": "MACD",
            "bollinger": "Bollinger", "vwap": "VWAP", "ichimoku": "Ichimoku",
            "mtf": "MTF", "ml": "ML_Signal", "hedge": "DynamicHedger",
            "rebalance": "PortfolioOpt",
            "liquidation": "Liquidation", "atr_stop": "ATR_Stop",
        }
        if sig.agent_id in agent_map:
            self._set_agent_active(agent_map[sig.agent_id])

        # Generate thought from strong signals
        if abs(sig.strength) >= 0.3 and sig.confidence >= 0.4 and self.memory:
            dir_word = "bullish" if sig.direction == "long" else "bearish" if sig.direction == "short" else "neutral"
            self.memory.record_thought(
                sig.agent_id,
                f"{dir_word} on {sig.asset} (str={sig.strength:+.2f} conf={sig.confidence:.2f}): {sig.rationale[:80]}",
                "observation",
            )

        await self._broadcast("signal", d)

    async def _on_intent(self, intent: TradeIntent):
        d = {
            "id": intent.id, "asset_in": intent.asset_in,
            "asset_out": intent.asset_out, "amount_in": round(intent.amount_in, 2),
            "min_out": round(intent.min_out, 4), "ttl": intent.ttl,
            "direction": "LONG" if intent.asset_out == "ETH" else "SHORT",
            "supporting": [
                {"agent_id": s.agent_id, "strength": round(s.strength, 4),
                 "confidence": round(s.confidence, 4)}
                for s in intent.supporting
            ],
        }
        self.recent_intents.append(d)
        if len(self.recent_intents) > 100:
            self.recent_intents = self.recent_intents[-100:]
        self.verdicts[intent.id] = []
        self._set_agent_active("Strategist")
        if self.memory:
            n_sigs = len(intent.supporting)
            self.memory.record_thought(
                "Strategist",
                f"Proposing {d['direction']} trade on {intent.asset_out if d['direction'] == 'LONG' else intent.asset_in} "
                f"(${intent.amount_in:.0f}, {n_sigs} supporting signals)",
                "decision",
            )
        await self._broadcast("intent", d)

    async def _on_verdict(self, v: RiskVerdict):
        d = {
            "intent_id": v.intent_id, "agent_id": v.agent_id,
            "approve": v.approve, "reason": v.reason,
        }
        self.verdicts.setdefault(v.intent_id, []).append(d)
        # Prevent unbounded growth — keep verdicts for recent intents only
        if len(self.verdicts) > 200:
            # Keep only the last 100 intent IDs
            recent_ids = set(i["id"] for i in self.recent_intents[-100:])
            self.verdicts = {k: v2 for k, v2 in self.verdicts.items() if k in recent_ids}

        agent_map = {"size": "RiskAgent_Size", "allowlist": "RiskAgent_Allowlist",
                     "drawdown": "RiskAgent_Drawdown",
                     "funds": "RiskAgent_Funds", "allocation": "RiskAgent_Allocation",
                     "var": "RiskAgent_VaR", "stress": "RiskAgent_Stress",
                     "compliance": "RiskAgent_Compliance",
                     "factor_exposure": "RiskAgent_FactorExposure",
                     "rebalance": "RiskAgent_Rebalance",
                     "sor_venues": "RiskAgent_SOR"}
        if v.agent_id in agent_map:
            self._set_agent_active(agent_map[v.agent_id])
        self._set_agent_active("Coordinator")

        await self._broadcast("verdict", d)

    async def _on_report(self, rep: ExecutionReport):
        d = {
            "intent_id": rep.intent_id, "status": rep.status,
            "tx_hash": rep.tx_hash, "fill_price": rep.fill_price,
            "slippage": rep.realized_slippage,
            "pnl": round(rep.pnl_estimate, 4) if rep.pnl_estimate else 0,
            "note": rep.note,
        }
        self.recent_reports.append(d)
        if len(self.recent_reports) > 200:
            self.recent_reports = self.recent_reports[-200:]
        self.trade_count += 1
        if rep.status == "filled" and (rep.pnl_estimate or 0) > 0:
            self.wins += 1
        elif rep.status == "filled":
            self.losses += 1

        # Only mark execution agents active on actual fills
        if rep.status == "filled":
            self._set_agent_active("Simulator")
            self._set_agent_active("Executor")
        self._set_agent_active("Auditor")
        await self._broadcast("report", d)

    async def _on_wallet_update(self, data: dict):
        self._set_agent_active("WalletManager")
        await self._broadcast("wallet", data)

    async def _on_wallet_low_funds(self, data: dict):
        await self._broadcast("wallet_low_funds", data)

    async def _on_wallet_rebalance(self, data: dict):
        await self._broadcast("wallet_rebalance", data)

    async def _on_thought(self, data: dict):
        await self._broadcast("thought", data)

    async def _on_social_feed(self, data: dict):
        await self._broadcast("social_feed", data)

    async def _on_network_post(self, data: dict):
        await self._broadcast("network_post", data)

    async def _on_network_comment(self, data: dict):
        await self._broadcast("network_comment", data)

    async def _on_network_feed_update(self, data: dict):
        await self._broadcast("network_feed_update", data)

    # ── Quantitative system event handlers ─────────────────────────

    async def _on_var_update(self, data: dict):
        self._set_agent_active("VaR_Engine")
        await self._broadcast("var", data)

    async def _on_pnl_attribution(self, data: dict):
        self._set_agent_active("PnL_Attributor")
        self._set_agent_active("FactorModel")
        await self._broadcast("pnl_attribution", data)

    async def _on_sor_routed(self, data: dict):
        self._set_agent_active("SmartOrderRouter")
        await self._broadcast("sor_routed", data)

    async def _on_tca_update(self, data: dict):
        self._set_agent_active("TCA_Tracker")
        await self._broadcast("tca", data)

    async def _on_compliance_alert(self, data: dict):
        self._set_agent_active("WashTradeDetector")
        self._set_agent_active("MarginMonitor")
        await self._broadcast("compliance_alert", data)

    async def _on_data_quality(self, data: dict):
        self._set_agent_active("DataQuality")
        await self._broadcast("data_quality", data)

    # ── WebSocket Handler ──────────────────────────────────────────

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        # Enforce connection limit
        if len(self._clients) >= self._MAX_WS_CLIENTS:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close(code=1013, message=b"max connections reached")
            return ws

        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=self._WS_MAX_MSG_SIZE)
        await ws.prepare(request)
        self._clients.add(ws)
        log.info("WebSocket client connected (%d total)", len(self._clients))

        # Send full state snapshot to new client (no secrets)
        snapshot = {
            "type": "snapshot",
            "ts": time.time(),
            "data": {
                "prices": self.prices,
                "signals": self.signals,
                "agents": self.agents,
                "intents": self.recent_intents[-20:],
                "reports": self.recent_reports[-50:],
                "verdicts": {k: v for k, v in list(self.verdicts.items())[-20:]},
                "state": {
                    "daily_pnl": round(self.state.get("daily_pnl", 0.0), 4),
                    "trade_count": self.trade_count,
                    "wins": self.wins,
                    "losses": self.losses,
                    "uptime": time.time() - self.start_time,
                    "kill_switch": self.kill_switch.active,
                },
                "wallet": self.wallet.summary() if self.wallet else None,
                "thoughts": self.memory.get_thoughts(30) if self.memory else [],
                "gateway": self._gateway_snapshot(),
                "erc8004": self.erc8004.status() if self.erc8004 else {"enabled": False},
                "uniswap": self.uniswap.status() if self.uniswap else {"enabled": False},
                "social": self.social.snapshot() if self.social else {"enabled": False},
            },
        }
        await ws.send_str(json.dumps(snapshot))

        try:
            # Listen for client messages (kill switch toggle, etc.)
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        cmd = json.loads(msg.data)
                        await self._handle_cmd(cmd, ws)
                    except Exception as e:
                        log.warning("Bad WS message: %s", e)
                elif msg.type == web.WSMsgType.ERROR:
                    break
        finally:
            self._clients.discard(ws)
            self._cmd_times.pop(ws, None)
            log.info("WebSocket client disconnected (%d remaining)", len(self._clients))

        return ws

    async def _handle_cmd(self, cmd: dict, ws: web.WebSocketResponse):
        # Rate limit check
        if not self._check_rate_limit(ws):
            await self._send_to(ws, "error", {"message": "rate limited, slow down"})
            return

        action = cmd.get("action")
        if action == "kill_switch":
            enabled = cmd.get("enabled", False)
            if enabled:
                self.kill_switch.engage("manual via dashboard")
            else:
                self.kill_switch.disengage()
            await self._broadcast("kill_switch", {"enabled": self.kill_switch.active})

        elif action == "wallet_deposit" and self.wallet:
            amount = _safe_float(cmd.get("amount", 0), min_val=0.01, max_val=1_000_000)
            if amount is None:
                return
            self.wallet.deposit(amount, cmd.get("note", ""))
            await self._broadcast("wallet", self.wallet.summary())

        elif action == "wallet_withdraw" and self.wallet:
            amount = _safe_float(cmd.get("amount", 0), min_val=0.01, max_val=1_000_000)
            if amount is None:
                return
            result = self.wallet.withdraw(amount, cmd.get("note", ""))
            resp = self.wallet.summary()
            if result is None:
                resp["error"] = "insufficient funds"
            await self._broadcast("wallet", resp)

        elif action == "gateway_connect" and self.gateway:
            await self._ws_gateway_connect(cmd, ws)

        elif action == "gateway_disconnect" and self.gateway:
            await self._ws_gateway_disconnect(cmd)

        elif action == "wallet_set_allocation" and self.wallet:
            asset = cmd.get("asset", "")
            target_pct = _safe_float(cmd.get("target_pct", 0), min_val=0, max_val=100)
            max_pct = _safe_float(cmd.get("max_pct", 50), min_val=0, max_val=100)
            if not asset or target_pct is None or max_pct is None:
                return
            self.wallet.set_allocation(asset, target_pct=target_pct, max_pct=max_pct)
            await self._broadcast("wallet", self.wallet.summary())

    def _connect_external_agent(self, name: str, protocol: str, weight: float,
                                agent_type: str = "signal-custom",
                                capabilities: list | None = None,
                                description: str = "",
                                metadata: dict | None = None) -> dict:
        """Shared logic for connecting an external agent via gateway.

        Returns full registration dict including agent_id, api_key, asn.
        """
        from .gateway import generate_asn, AGENT_TYPE_REGISTRY

        agent_id = f"ext_{name.lower().replace(' ', '_').replace('-', '_')}"
        api_key = secrets.token_hex(32)
        asn = generate_asn()

        if agent_type not in AGENT_TYPE_REGISTRY:
            agent_type = "signal-custom"

        agent = ConnectedAgent(
            agent_id=agent_id, name=name, protocol=protocol,
            api_key=api_key, agent_type=agent_type, asn=asn,
            description=description[:200], weight=weight,
            capabilities=capabilities or [],
            metadata=metadata or {},
        )
        self.gateway.agents[agent_id] = agent

        # Register with Strategist weights
        if self.gateway.strategist:
            self.gateway.strategist.weights[agent_id] = weight
            self.gateway.bus.subscribe(
                f"signal.{agent_id}", self.gateway.strategist._on_signal
            )
            total = sum(self.gateway.strategist.weights.values()) or 1.0
            for k in self.gateway.strategist.weights:
                self.gateway.strategist.weights[k] /= total

        # Map agent_type category to dashboard role
        type_info = AGENT_TYPE_REGISTRY.get(agent_type, {})
        cat = type_info.get("category", "external")
        role_map = {
            "signal-generators": "external", "data-research": "analyst",
            "ai-ml": "ml", "risk-compliance": "risk",
            "execution": "execution", "meta-orchestration": "meta",
        }
        role = role_map.get(cat, "external")

        # Register in the dashboard agent tree
        self.agents[name] = {
            "name": name, "role": role, "topic": f"signal.{agent_id}",
            "status": "idle", "last_tick": None, "ticks": 0,
        }
        self.bus.subscribe(f"signal.{agent_id}", self._on_signal)

        log.info("AGENT REGISTERED: %s asn=%s type=%s protocol=%s weight=%.3f",
                 agent_id, asn, agent_type, protocol, weight)

        return {
            "agent_id": agent_id,
            "asn": asn,
            "api_key": api_key,
            "name": name,
            "agent_type": agent_type,
            "agent_type_label": type_info.get("label", agent_type),
            "category": cat,
            "protocol": protocol,
            "weight": weight,
            "status": "online",
            "capabilities": capabilities or [],
            "description": description[:200],
        }

    def _disconnect_external_agent(self, agent_id: str) -> ConnectedAgent | None:
        """Shared logic for disconnecting an external agent.

        Returns the disconnected agent, or None if not found.
        """
        agent = self.gateway.agents.pop(agent_id, None)
        if not agent:
            return None

        # Clean up gateway state via its public dict attributes
        self.gateway._latest_signals.pop(agent_id, None)

        if self.gateway.strategist and agent_id in self.gateway.strategist.weights:
            del self.gateway.strategist.weights[agent_id]
            total = sum(self.gateway.strategist.weights.values()) or 1.0
            for k in self.gateway.strategist.weights:
                self.gateway.strategist.weights[k] /= total

        self.agents.pop(agent.name, None)
        log.info("AGENT DISCONNECTED: %s", agent_id)
        return agent

    async def _ws_gateway_connect(self, cmd: dict, ws: web.WebSocketResponse):
        """Handle gateway_connect command from a WebSocket client."""
        name = cmd.get("name", "").strip()
        protocol = cmd.get("protocol", "openclaw")
        if not name or protocol not in ("openclaw", "hermes", "ironclaw", "raw"):
            return

        weight = _safe_float(cmd.get("weight", 0.06), min_val=0.0, max_val=1.0)
        if weight is None:
            return

        agent_type = cmd.get("agent_type", "signal-custom")
        capabilities = cmd.get("capabilities", [])
        description = cmd.get("description", "")

        reg = self._connect_external_agent(
            name, protocol, weight,
            agent_type=agent_type,
            capabilities=capabilities,
            description=description,
        )

        # Send full registration (with API key) only to the requesting client
        await self._send_to(ws, "gateway_agent_registered", reg)
        # Broadcast agent list update (no secrets) to all clients
        safe = {k: v for k, v in reg.items() if k != "api_key"}
        await self._broadcast("gateway_agent_connected", safe)
        await self._broadcast("agents_update", {"agents": self.agents})

    async def _ws_gateway_disconnect(self, cmd: dict):
        """Handle gateway_disconnect command from a WebSocket client."""
        agent_id = cmd.get("agent_id", "")
        agent = self._disconnect_external_agent(agent_id)
        if agent:
            await self._broadcast("gateway_agent_disconnected", {"agent_id": agent_id})
            await self._broadcast("agents_update", {"agents": self.agents})

    # ── HTTP Handlers ───────────────────────────────────────────────

    async def _handle_history(self, request: web.Request) -> web.Response:
        """Query trade history from database."""
        if not self.db:
            return web.json_response({"error": "database not configured"}, status=503)
        try:
            limit = min(10000, max(1, int(request.query.get("limit", "100"))))
        except (ValueError, TypeError):
            limit = 100
        status_filter = request.query.get("status", None)

        try:
            if status_filter:
                rows = await self.db.fetch(
                    "SELECT * FROM reports WHERE status=$1 ORDER BY ts DESC LIMIT $2",
                    status_filter, limit,
                )
            else:
                rows = await self.db.fetch(
                    "SELECT * FROM reports ORDER BY ts DESC LIMIT $1", limit,
                )
            intents = await self.db.fetch(
                "SELECT * FROM intents ORDER BY ts DESC LIMIT $1", limit,
            )
            return web.json_response({"reports": rows, "intents": intents})
        except Exception:
            log.exception("History query failed")
            return web.json_response({"error": "failed to load history"}, status=500)

    async def _handle_state(self, request: web.Request) -> web.Response:
        return web.json_response({
            "prices": self.prices,
            "signals": self.signals,
            "agents": self.agents,
            "daily_pnl": round(self.state.get("daily_pnl", 0.0), 4),
            "trade_count": self.trade_count,
            "wins": self.wins,
            "losses": self.losses,
            "uptime": time.time() - self.start_time,
            "kill_switch": self.kill_switch.active,
        })

    async def _handle_wallet(self, request: web.Request) -> web.Response:
        """GET /api/wallet — full wallet state."""
        if not self.wallet:
            return web.json_response({"error": "wallet not configured"}, status=404)
        return web.json_response(self.wallet.summary())

    async def _handle_wallet_deposit(self, request: web.Request) -> web.Response:
        """POST /api/wallet/deposit {amount, note}"""
        if not self.wallet:
            return web.json_response({"error": "wallet not configured"}, status=404)
        body = await request.json()
        amount = _safe_float(body.get("amount", 0), min_val=0.01)
        if amount is None:
            return web.json_response({"error": "amount must be a positive finite number"}, status=400)
        self.wallet.deposit(amount, body.get("note", ""))
        return web.json_response(self.wallet.summary())

    async def _handle_wallet_withdraw(self, request: web.Request) -> web.Response:
        """POST /api/wallet/withdraw {amount, note}"""
        if not self.wallet:
            return web.json_response({"error": "wallet not configured"}, status=404)
        body = await request.json()
        amount = _safe_float(body.get("amount", 0), min_val=0.01)
        if amount is None:
            return web.json_response({"error": "amount must be a positive finite number"}, status=400)
        result = self.wallet.withdraw(amount, body.get("note", ""))
        if result is None:
            return web.json_response({"error": "insufficient funds",
                                      "available": self.wallet.available_cash()}, status=400)
        return web.json_response(self.wallet.summary())

    async def _handle_wallet_allocations(self, request: web.Request) -> web.Response:
        """POST /api/wallet/allocations {asset, target_pct, max_pct}"""
        if not self.wallet:
            return web.json_response({"error": "wallet not configured"}, status=404)
        body = await request.json()
        asset = body.get("asset", "")
        if not asset:
            return web.json_response({"error": "asset required"}, status=400)
        target_pct = _safe_float(body.get("target_pct", 0), min_val=0, max_val=100)
        max_pct = _safe_float(body.get("max_pct", 50), min_val=0, max_val=100)
        if target_pct is None or max_pct is None:
            return web.json_response({"error": "target_pct and max_pct must be finite numbers 0-100"}, status=400)
        if target_pct > max_pct:
            return web.json_response({"error": "target_pct cannot exceed max_pct"}, status=400)
        # Check total allocations won't exceed 100%
        existing_total = sum(
            a.target_pct for name, a in self.wallet.allocations.items() if name != asset
        )
        if existing_total + target_pct > 100.0:
            return web.json_response({
                "error": f"total allocation would be {existing_total + target_pct:.1f}% (max 100%)"
            }, status=400)
        self.wallet.set_allocation(asset, target_pct=target_pct, max_pct=max_pct)
        return web.json_response(self.wallet.summary())

    async def _handle_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(Path(__file__).parent / "static" / "index.html")

    async def _handle_report_json(self, request: web.Request) -> web.Response:
        if not self.db:
            return web.json_response({"error": "database not configured"}, status=503)
        try:
            report = await load_from_db(self.db)
            return web.json_response(report.to_dict())
        except Exception:
            log.exception("Report JSON generation failed")
            return web.json_response({"error": "failed to generate report"}, status=500)

    async def _handle_report_html(self, request: web.Request) -> web.Response:
        async def _generate():
            report = await load_from_db(self.db)
            import tempfile
            out = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
                    out = Path(f.name)
                generate_html_report(report, out)
                return out.read_text()
            finally:
                if out and out.exists():
                    out.unlink()

        if not self.db:
            return web.Response(text="Database not configured", status=503)
        try:
            html = await _generate()
            return web.Response(text=html, content_type="text/html")
        except Exception:
            log.exception("Report HTML generation failed")
            return web.Response(text="Error generating report", status=500)

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Unauthenticated health check for monitoring.

        Deep check: verifies DB connectivity and bus activity.
        Use ?deep=1 for full check, otherwise returns shallow OK.
        """
        uptime = time.time() - self.start_time
        deep = request.query.get("deep", "") == "1"

        if not deep:
            return web.json_response({"status": "ok", "uptime": round(uptime, 1)})

        checks = {"uptime": round(uptime, 1)}
        overall_ok = True

        # Database health
        if self.db:
            db_health = await self.db.check_health()
            checks["database"] = db_health
            if not db_health.get("ok"):
                overall_ok = False
        else:
            checks["database"] = {"ok": False, "backend": "none"}
            overall_ok = False

        # Bus activity — check if we've received market data recently
        has_prices = len(self.prices) > 0
        checks["market_feed"] = {"active": has_prices, "pairs": len(self.prices)}

        # Kill switch state
        if self.kill_switch:
            checks["kill_switch"] = {"active": self.kill_switch.active}

        # Agent count
        checks["agents"] = {"total": len(self.agents)}

        status = "ok" if overall_ok else "degraded"
        status_code = 200 if overall_ok else 503
        return web.json_response(
            {"status": status, **checks},
            status=status_code,
        )

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """GET /metrics — Prometheus text exposition format."""
        if not self.metrics:
            return web.Response(text="# No metrics collector configured\n",
                                content_type="text/plain")
        # Update WS connection count from live state
        self.metrics.ws_connections = len(self._clients)
        body = self.metrics.render()
        return web.Response(text=body, content_type="text/plain; version=0.0.4; charset=utf-8")

    async def _handle_migration_status(self, request: web.Request) -> web.Response:
        """GET /api/migrations — current migration state."""
        if not self.db:
            return web.json_response({"error": "no database"}, status=404)
        from .migrations import get_migration_status
        status = await get_migration_status(self.db)
        return web.json_response(status)

    # ── Phase 1-40 API Handlers ──────────────────────────────────

    def _get_component(self, name: str):
        """Safely retrieve a component from state dict."""
        return self.state.get(name)

    async def _handle_commander(self, request: web.Request) -> web.Response:
        """GET /api/commander — CommanderGate status + recent decisions."""
        c = self._get_component("commander")
        if c and hasattr(c, "summary"):
            return web.json_response(c.summary())
        return web.json_response({"status": "no commander (rule-based mode)"})

    async def _handle_vault(self, request: web.Request) -> web.Response:
        """GET /api/vault — ERC-4626 vault state, NAV, deposits."""
        v = self._get_component("vault")
        if v and hasattr(v, "summary"):
            return web.json_response(v.summary())
        return web.json_response({"status": "vault not initialized"})

    async def _handle_marketplace(self, request: web.Request) -> web.Response:
        """GET /api/marketplace — agent marketplace leaderboard + auctions."""
        m = self._get_component("marketplace")
        if m and hasattr(m, "summary"):
            return web.json_response(m.summary())
        return web.json_response({"status": "marketplace not initialized"})

    async def _handle_debates(self, request: web.Request) -> web.Response:
        """GET /api/debates — recent adversarial debate results."""
        d = self._get_component("debate_engine")
        if d and hasattr(d, "summary"):
            return web.json_response(d.summary())
        return web.json_response({"status": "debate engine not initialized"})

    async def _handle_elo(self, request: web.Request) -> web.Response:
        """GET /api/elo — agent ELO leaderboard."""
        e = self._get_component("elo_tracker")
        if e and hasattr(e, "summary"):
            return web.json_response(e.summary())
        return web.json_response({"status": "ELO tracker not initialized"})

    async def _handle_narratives(self, request: web.Request) -> web.Response:
        """GET /api/narratives — recent market narratives."""
        n = self._get_component("narrative")
        if n and hasattr(n, "summary"):
            return web.json_response(n.summary())
        return web.json_response({"status": "narrative engine not initialized"})

    async def _handle_consensus(self, request: web.Request) -> web.Response:
        """GET /api/consensus — swarm consensus voting results."""
        c = self._get_component("consensus")
        if c and hasattr(c, "summary"):
            return web.json_response(c.summary())
        return web.json_response({"status": "consensus not initialized"})

    async def _handle_observability(self, request: web.Request) -> web.Response:
        """GET /api/observability — full system health report."""
        o = self._get_component("observability")
        if o and hasattr(o, "summary"):
            return web.json_response(o.summary())
        return web.json_response({"status": "observability not initialized"})

    async def _handle_treasury(self, request: web.Request) -> web.Response:
        """GET /api/treasury — autonomous treasury state + allocations."""
        t = self._get_component("treasury")
        if t and hasattr(t, "summary"):
            return web.json_response(t.summary())
        return web.json_response({"status": "treasury not initialized"})

    async def _handle_governance(self, request: web.Request) -> web.Response:
        """GET /api/governance — DAO proposals + voting."""
        g = self._get_component("governance")
        if g and hasattr(g, "summary"):
            return web.json_response(g.summary())
        return web.json_response({"status": "governance not initialized"})

    async def _handle_evolution(self, request: web.Request) -> web.Response:
        """GET /api/evolution — genetic strategy evolution status."""
        e = self._get_component("evolution")
        if e and hasattr(e, "summary"):
            return web.json_response(e.summary())
        return web.json_response({"status": "evolution not initialized"})

    async def _handle_grid(self, request: web.Request) -> web.Response:
        """GET /api/grid — grid trading status + fills."""
        g = self._get_component("grid")
        if g and hasattr(g, "summary"):
            return web.json_response(g.summary())
        return web.json_response({"status": "grid trading not initialized"})

    async def _handle_thoughts(self, request: web.Request) -> web.Response:
        """GET /api/thoughts — recent agent thought stream."""
        if not self.memory:
            return web.json_response({"thoughts": []})
        limit = min(100, max(1, int(request.query.get("limit", "50"))))
        return web.json_response({"thoughts": self.memory.get_thoughts(limit)})

    async def _handle_memory(self, request: web.Request) -> web.Response:
        """GET /api/memory — session history and strategy notes."""
        if not self.memory:
            return web.json_response({"notes": "", "sessions": []})
        return web.json_response({
            "notes": self.memory.read_notes(),
            "sessions": self.memory.get_past_sessions(limit=10),
            "current_session": self.memory.session_summary(),
        })

    async def _handle_slides(self, request: web.Request) -> web.FileResponse:
        slides_path = Path(__file__).parent / "static" / "slides.html"
        if slides_path.exists():
            return web.FileResponse(slides_path)
        return web.Response(text="Slides not found", status=404)

    # ── Competition Features ─────────────────────────────────────────

    async def _handle_nlp_strategy(self, request: web.Request) -> web.Response:
        """POST /api/strategy/nlp — configure strategy from natural language.

        Body: {"strategy": "momentum following with RSI and whale tracking"}
        """
        if not self.strategist:
            return web.json_response({"error": "strategist not available"}, status=400)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        text = body.get("strategy", body.get("text", ""))
        if not text:
            return web.json_response({"error": "missing 'strategy' field"}, status=400)

        from .nlp_strategy import parse_strategy, apply_strategy
        config = parse_strategy(text)
        result = apply_strategy(self.strategist, config)
        await self._broadcast("strategy_update", result)
        return web.json_response({"ok": True, **result})

    async def _handle_strategy_presets(self, request: web.Request) -> web.Response:
        """GET /api/strategy/presets — list available preset strategies."""
        from .nlp_strategy import PRESETS
        return web.json_response({"presets": PRESETS})

    async def _handle_strategy_weights(self, request: web.Request) -> web.Response:
        """GET /api/strategy/weights — current strategist weights."""
        if not self.strategist:
            return web.json_response({"error": "strategist not available"}, status=400)
        return web.json_response({
            "weights": {k: round(v, 4) for k, v in self.strategist.weights.items()},
            "threshold": self.strategist.THRESHOLD,
            "regime": getattr(self.strategist, "regime", "unknown"),
            "base_size": self.strategist.base_size,
        })

    async def _handle_leaderboard(self, request: web.Request) -> web.Response:
        """GET /api/leaderboard — agent performance leaderboard."""
        if not self.capital_allocator:
            return web.json_response({"leaderboard": [], "agents_tracked": 0})
        return web.json_response(self.capital_allocator.summary())

    async def _handle_confidence(self, request: web.Request) -> web.Response:
        """GET /api/confidence — swarm confidence gauge data."""
        if not self.strategist:
            return web.json_response({"confidence": 0, "signals": 0})

        signals = self.strategist.latest
        if not signals:
            return web.json_response({"confidence": 0, "signals": 0, "direction": "flat"})

        # Compute weighted consensus
        active = {a for a in self.strategist.weights if a in signals}
        total_w = sum(self.strategist.weights[a] for a in active) or 1.0
        score = sum(
            (self.strategist.weights[a] / total_w)
            * signals[a].strength * signals[a].confidence
            for a in active
        )

        # Confidence = how much agents agree (low variance = high confidence)
        strengths = [signals[a].strength for a in active]
        if len(strengths) > 1:
            mean_s = sum(strengths) / len(strengths)
            variance = sum((s - mean_s) ** 2 for s in strengths) / len(strengths)
            agreement = max(0.0, 1.0 - variance * 4)  # 0-1 scale
        else:
            agreement = 0.5

        direction = "long" if score > 0.05 else "short" if score < -0.05 else "flat"

        return web.json_response({
            "score": round(score, 4),
            "confidence": round(agreement, 3),
            "direction": direction,
            "active_agents": len(active),
            "total_agents": len(self.strategist.weights),
            "regime": getattr(self.strategist, "regime", "unknown"),
            "vol_damp": round(getattr(self.strategist, "vol_damp", 1.0), 3),
            "top_signals": [
                {
                    "agent": a,
                    "direction": signals[a].direction,
                    "strength": round(signals[a].strength, 3),
                    "confidence": round(signals[a].confidence, 3),
                    "weight": round(self.strategist.weights.get(a, 0), 4),
                }
                for a in sorted(active,
                                key=lambda x: abs(signals[x].strength * signals[x].confidence),
                                reverse=True)[:10]
            ],
        })

    async def _handle_strategy_commit(self, request: web.Request) -> web.Response:
        """POST /api/strategy/commit — commit current strategy (privacy layer)."""
        if not self.strategist:
            return web.json_response({"error": "strategist not available"}, status=400)

        from .strategy_privacy import StrategyPrivacyManager
        if not self._privacy_mgr:
            self._privacy_mgr = StrategyPrivacyManager()

        commit = self._privacy_mgr.commit(
            self.strategist.weights,
            metadata={
                "regime": getattr(self.strategist, "regime", ""),
                "threshold": self.strategist.THRESHOLD,
            },
        )
        return web.json_response({
            "ok": True,
            "commit_hash": commit.commit_hash,
            "timestamp": commit.timestamp,
            "message": "Strategy committed. Weights are now hidden until reveal.",
        })

    async def _handle_strategy_reveal(self, request: web.Request) -> web.Response:
        """POST /api/strategy/reveal — reveal a committed strategy."""
        if not self._privacy_mgr:
            return web.json_response({"error": "no strategies committed"}, status=400)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        commit_hash = body.get("commit_hash", "")
        if not commit_hash:
            # Reveal the active commit
            active = self._privacy_mgr.active_commit
            if not active:
                return web.json_response({"error": "no active commit"}, status=400)
            commit_hash = active.commit_hash

        try:
            reveal = self._privacy_mgr.reveal(commit_hash)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=404)

        return web.json_response({
            "ok": True,
            "valid": reveal.valid,
            "commit_hash": reveal.commit_hash,
            "weights": reveal.weights,
            "committed_at": reveal.timestamp_committed,
            "revealed_at": reveal.timestamp_revealed,
            "hidden_duration_s": round(reveal.timestamp_revealed - reveal.timestamp_committed),
        })

    async def _handle_pyth_prices(self, request: web.Request) -> web.Response:
        """GET /api/pyth — fetch decentralized price from Pyth oracle."""
        try:
            from .pyth_oracle import PythOracle, PYTH_FEEDS
            if not self._pyth_oracle:
                self._pyth_oracle = PythOracle(self.bus, assets=["ETH", "BTC", "SOL"])
            feed_ids = [fid for sym, fid in PYTH_FEEDS.items()
                        if sym in ("ETH", "BTC", "SOL")]
            prices = await self._pyth_oracle._fetch_prices(feed_ids)
            return web.json_response({"source": "pyth_hermes", "prices": prices})
        except Exception as e:
            return web.json_response({"error": str(e), "source": "pyth_hermes"}, status=500)

    # ── Emergency Controls ─────────────────────────────────────────

    async def _handle_cancel_all(self, request: web.Request) -> web.Response:
        """POST /api/cancel-all — Cancel ALL open orders on Kraken."""
        try:
            from .kraken_api import get_client
            client = get_client()
            if client._cfg.api_key and client._cfg.api_secret:
                result = await client.cancel_all()
                count = result.get("count", 0)
                await self._broadcast("dashboard.emergency",
                                       {"action": "cancel_all", "count": count})
                return web.json_response({"ok": True, "cancelled": count})
            return web.json_response({"ok": False, "error": "No API keys configured"}, status=400)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _handle_flatten(self, request: web.Request) -> web.Response:
        """POST /api/flatten — Cancel all orders + engage kill switch."""
        try:
            # Engage kill switch first
            self.kill_switch.engage("dashboard_flatten")
            # Cancel all orders
            from .kraken_api import get_client
            client = get_client()
            count = 0
            if client._cfg.api_key and client._cfg.api_secret:
                result = await client.cancel_all()
                count = result.get("count", 0)
            await self._broadcast("dashboard.emergency",
                                   {"action": "flatten", "count": count})
            await self._broadcast("kill_switch",
                                   {"active": True, "reason": "dashboard_flatten"})
            return web.json_response({"ok": True, "cancelled": count, "kill_switch": True})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _handle_pause(self, request: web.Request) -> web.Response:
        """POST /api/pause — Toggle kill switch (pause/resume trading)."""
        if self.kill_switch.active:
            self.kill_switch.disengage()
            await self._broadcast("kill_switch", {"active": False})
            return web.json_response({"ok": True, "paused": False})
        else:
            self.kill_switch.engage("dashboard_pause")
            await self._broadcast("kill_switch",
                                   {"active": True, "reason": "dashboard_pause"})
            return web.json_response({"ok": True, "paused": True})

    # ── ERC-8004 On-Chain Status ─────────────────────────────────────

    async def _handle_erc8004_status(self, request: web.Request) -> web.Response:
        """GET /api/erc8004 — on-chain identity, reputation, and validation status."""
        if not self.erc8004:
            return web.json_response({"enabled": False})
        status = self.erc8004.status()
        return web.json_response({"enabled": True, **status})

    async def _handle_uniswap_status(self, request: web.Request) -> web.Response:
        """GET /api/uniswap — Uniswap DEX execution status."""
        if not self.uniswap:
            return web.json_response({"enabled": False})
        return web.json_response(self.uniswap.status())

    # ── Gateway Management (frontend-facing) ───────────────────────

    async def _handle_gateway_status(self, request: web.Request) -> web.Response:
        """GET /api/gateway/status — gateway config + connected agents (no secrets)."""
        if not self.gateway:
            return web.json_response({"enabled": False})
        return web.json_response({
            "enabled": True,
            "agents": [
                {
                    "agent_id": a.agent_id,
                    "name": a.name,
                    "protocol": a.protocol,
                    "connected_at": a.connected_at,
                    "last_signal_at": a.last_signal_at,
                    "signal_count": a.signal_count,
                    "weight": round(a.weight, 4),
                }
                for a in self.gateway.agents.values()
            ],
        })

    async def _handle_gateway_connect_agent(self, request: web.Request) -> web.Response:
        """POST /api/gateway/ui/connect — connect an external agent from the UI.

        Body: { "name": "...", "protocol": "openclaw|hermes|raw", "weight": 0.06 }
        No master_key needed — the dashboard is already authenticated.
        """
        if not self.gateway:
            return web.json_response({"error": "gateway not enabled"}, status=404)

        body = await request.json()
        name = body.get("name", "").strip()
        if not name:
            return web.json_response({"error": "name is required"}, status=400)

        protocol = body.get("protocol", "openclaw")
        if protocol not in ("openclaw", "hermes", "ironclaw", "raw"):
            return web.json_response({"error": "protocol must be openclaw, hermes, or raw"}, status=400)

        weight = _safe_float(body.get("weight", 0.06), min_val=0.0, max_val=1.0)
        if weight is None:
            return web.json_response({"error": "weight must be a finite number 0-1"}, status=400)

        reg = self._connect_external_agent(
            name, protocol, weight,
            agent_type=body.get("agent_type", "signal-custom"),
            capabilities=body.get("capabilities", []),
            description=body.get("description", ""),
            metadata=body.get("metadata", {}),
        )

        safe = {k: v for k, v in reg.items() if k != "api_key"}
        await self._broadcast("gateway_agent_connected", safe)
        await self._broadcast("agents_update", {"agents": self.agents})

        reg["endpoints"] = {
            "signal": "POST /api/gateway/signal",
            "market": "GET /api/gateway/market",
            "websocket": "WS /ws/agent (use Authorization: Bearer <api_key> header)",
        }
        return web.json_response(reg)

    async def _handle_gateway_disconnect_agent(self, request: web.Request) -> web.Response:
        """POST /api/gateway/ui/disconnect — disconnect an agent by agent_id."""
        if not self.gateway:
            return web.json_response({"error": "gateway not enabled"}, status=404)

        body = await request.json()
        agent_id = body.get("agent_id", "")
        if not agent_id:
            return web.json_response({"error": "agent_id is required"}, status=400)

        agent = self._disconnect_external_agent(agent_id)
        if not agent:
            return web.json_response({"error": "agent not found"}, status=404)

        await self._broadcast("agents_update", {"agents": self.agents})
        return web.json_response({"disconnected": agent_id})

    # ── Social Trading Endpoints ─────────────────────────────────────

    async def _handle_social_profiles(self, request: web.Request) -> web.Response:
        """GET /api/social/profiles — ranked public agent profiles."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        sort_by = request.query.get("sort", "total_pnl")
        limit = min(100, max(1, int(request.query.get("limit", "50"))))
        return web.json_response({
            "profiles": self.social.get_public_profiles(sort_by=sort_by, limit=limit),
        })

    async def _handle_social_profile(self, request: web.Request) -> web.Response:
        """GET /api/social/profile/{agent_id} — single agent profile."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        agent_id = request.match_info["agent_id"]
        profile = self.social.get_profile(agent_id)
        if not profile:
            return web.json_response({"error": "profile not found"}, status=404)
        return web.json_response({
            "profile": profile.public_dict(),
            "achievements": self.social.get_achievements(agent_id),
            "followers": list(self.social.followers.get(agent_id, set()))[:100],
            "following": list(self.social.following.get(agent_id, set()))[:100],
            "copiers": self.social.get_copiers(agent_id),
            "copying": self.social.get_copying(agent_id),
        })

    async def _handle_social_create_profile(self, request: web.Request) -> web.Response:
        """POST /api/social/profile — create or update an agent profile."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        body = await request.json()
        agent_id = body.get("agent_id", "").strip()[:64]
        display_name = body.get("display_name", "").strip()[:100]
        if not agent_id or not display_name:
            return web.json_response({"error": "agent_id and display_name required"}, status=400)
        bio = str(body.get("bio", ""))[:500]
        tags = body.get("strategy_tags", [])
        if isinstance(tags, list):
            tags = [str(t)[:50] for t in tags[:20]]  # max 20 tags, 50 chars each
        else:
            tags = []
        strategy_desc = str(body.get("strategy_description", ""))[:1000]
        visibility = body.get("visibility", "public")
        if visibility not in ("public", "private"):
            visibility = "public"
        profile = self.social.create_profile(
            agent_id=agent_id,
            display_name=display_name,
            bio=bio,
            strategy_tags=tags,
            strategy_description=strategy_desc,
            visibility=visibility,
        )
        return web.json_response({"ok": True, "profile": profile.public_dict()})

    async def _handle_social_follow(self, request: web.Request) -> web.Response:
        """POST /api/social/follow — follow an agent."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        body = await request.json()
        follower_id = body.get("follower_id", "").strip()
        leader_id = body.get("leader_id", "").strip()
        if not follower_id or not leader_id:
            return web.json_response({"error": "follower_id and leader_id required"}, status=400)
        ok = self.social.follow(follower_id, leader_id)
        return web.json_response({"ok": ok})

    async def _handle_social_unfollow(self, request: web.Request) -> web.Response:
        """POST /api/social/unfollow — unfollow an agent."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        body = await request.json()
        follower_id = body.get("follower_id", "").strip()
        leader_id = body.get("leader_id", "").strip()
        if not follower_id or not leader_id:
            return web.json_response({"error": "follower_id and leader_id required"}, status=400)
        ok = self.social.unfollow(follower_id, leader_id)
        return web.json_response({"ok": ok})

    async def _handle_social_copy_start(self, request: web.Request) -> web.Response:
        """POST /api/social/copy — start copying an agent's trades."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        body = await request.json()
        copier_id = body.get("copier_id", "").strip()
        leader_id = body.get("leader_id", "").strip()
        if not copier_id or not leader_id:
            return web.json_response({"error": "copier_id and leader_id required"}, status=400)

        allocation = _safe_float(body.get("allocation", 1000), min_val=10, max_val=1_000_000)
        if allocation is None:
            return web.json_response({"error": "allocation must be $10-$1M"}, status=400)

        rel = self.social.start_copying(
            copier_id=copier_id,
            leader_id=leader_id,
            allocation=allocation,
            size_multiplier=_safe_float(body.get("size_multiplier", 1.0), min_val=0.01, max_val=10.0) or 1.0,
            max_trade_size=_safe_float(body.get("max_trade_size", 500), min_val=1, max_val=100_000) or 500.0,
            max_daily_loss=_safe_float(body.get("max_daily_loss", 100), min_val=1, max_val=100_000) or 100.0,
            min_confidence=_safe_float(body.get("min_confidence", 0.3), min_val=0, max_val=1.0) or 0.3,
            management_fee_pct=_safe_float(body.get("management_fee_pct", 2.0), min_val=0, max_val=10) or 2.0,
            performance_fee_pct=_safe_float(body.get("performance_fee_pct", 20.0), min_val=0, max_val=50) or 20.0,
            copy_longs=body.get("copy_longs", True),
            copy_shorts=body.get("copy_shorts", True),
            referral_code=body.get("referral_code", ""),
        )
        if not rel:
            return web.json_response({"error": "cannot copy (same agent or already copying)"}, status=400)

        await self._broadcast("social_copy", {
            "action": "started",
            "copier_id": copier_id,
            "leader_id": leader_id,
            "allocation": allocation,
        })
        return web.json_response({"ok": True, "relation": rel.to_dict()})

    async def _handle_social_copy_stop(self, request: web.Request) -> web.Response:
        """POST /api/social/copy/stop — stop copying an agent."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        body = await request.json()
        copier_id = body.get("copier_id", "").strip()
        leader_id = body.get("leader_id", "").strip()
        if not copier_id or not leader_id:
            return web.json_response({"error": "copier_id and leader_id required"}, status=400)
        ok = self.social.stop_copying(copier_id, leader_id)
        if ok:
            await self._broadcast("social_copy", {
                "action": "stopped",
                "copier_id": copier_id,
                "leader_id": leader_id,
            })
        return web.json_response({"ok": ok})

    async def _handle_social_feed(self, request: web.Request) -> web.Response:
        """GET /api/social/feed — global social feed."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        limit = min(100, max(1, int(request.query.get("limit", "50"))))
        event_type = request.query.get("type", None)
        agent_id = request.query.get("agent_id", None)
        return web.json_response({
            "feed": self.social.get_feed(limit=limit, event_type=event_type, agent_id=agent_id),
        })

    async def _handle_social_feed_personal(self, request: web.Request) -> web.Response:
        """GET /api/social/feed/{agent_id} — personalized feed for an agent."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        agent_id = request.match_info["agent_id"]
        limit = min(100, max(1, int(request.query.get("limit", "50"))))
        return web.json_response({
            "feed": self.social.get_personalized_feed(agent_id, limit=limit),
        })

    async def _handle_social_leaderboard(self, request: web.Request) -> web.Response:
        """GET /api/social/leaderboard — enhanced social leaderboard."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        return web.json_response(self.social.social_leaderboard())

    async def _handle_social_referral(self, request: web.Request) -> web.Response:
        """GET /api/social/referral/{agent_id} — referral stats for an agent."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        agent_id = request.match_info["agent_id"]
        return web.json_response(self.social.get_referral_stats(agent_id))

    async def _handle_social_search(self, request: web.Request) -> web.Response:
        """GET /api/social/search — search agent profiles."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        query = request.query.get("q", "")
        tags = request.query.get("tags", "").split(",") if request.query.get("tags") else None
        min_trades = int(request.query.get("min_trades", "0"))
        min_win_rate = float(request.query.get("min_win_rate", "0"))
        sort_by = request.query.get("sort", "reputation")
        return web.json_response({
            "results": self.social.search_agents(
                query=query, tags=tags, min_trades=min_trades,
                min_win_rate=min_win_rate, sort_by=sort_by,
            ),
        })

    async def _handle_social_stats(self, request: web.Request) -> web.Response:
        """GET /api/social/stats — global platform stats."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        return web.json_response(self.social.platform_stats())

    async def _handle_social_comment(self, request: web.Request) -> web.Response:
        """POST /api/social/comment — add a comment to a feed event."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        body = await request.json()
        event_id = body.get("event_id", "").strip()[:64]
        agent_id = body.get("agent_id", "").strip()[:64]
        text = body.get("text", "").strip()[:500]
        if not event_id or not agent_id or not text:
            return web.json_response({"error": "event_id, agent_id, and text required"}, status=400)
        ok = self.social.add_comment(event_id, agent_id, text)
        return web.json_response({"ok": ok})

    async def _handle_social_like(self, request: web.Request) -> web.Response:
        """POST /api/social/like — like a feed event."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        body = await request.json()
        event_id = body.get("event_id", "").strip()
        if not event_id:
            return web.json_response({"error": "event_id required"}, status=400)
        ok = self.social.like_event(event_id)
        return web.json_response({"ok": ok})

    async def _handle_social_achievements(self, request: web.Request) -> web.Response:
        """GET /api/social/achievements/{agent_id} — agent achievements."""
        if not self.social:
            return web.json_response({"error": "social trading not enabled"}, status=404)
        agent_id = request.match_info["agent_id"]
        return web.json_response({
            "achievements": self.social.get_achievements(agent_id),
        })

    # ── SwarmNetwork Endpoints ─────────────────────────────────────

    def _require_network(self):
        if not self.network:
            return web.json_response({"error": "swarm network not enabled"}, status=404)
        return None

    async def _handle_network_home(self, request: web.Request) -> web.Response:
        """GET /api/network/home — one-call dashboard for an agent."""
        err = self._require_network()
        if err:
            return err
        agent_id = request.query.get("agent_id", "")
        if not agent_id:
            return web.json_response({"error": "agent_id query param required"}, status=400)
        return web.json_response(self.network.home(agent_id))

    async def _handle_network_posts_create(self, request: web.Request) -> web.Response:
        """POST /api/network/posts — create a post."""
        err = self._require_network()
        if err:
            return err
        body = await request.json()
        author_id = body.get("author_id", "").strip()[:64]
        title = body.get("title", "").strip()
        if not author_id or not title:
            return web.json_response({"error": "author_id and title required"}, status=400)
        post = self.network.create_post(
            author_id=author_id,
            title=title,
            body=str(body.get("body", "")),
            community_id=body.get("community_id", "general"),
            post_type=body.get("post_type", "analysis"),
            structured_data=body.get("structured_data"),
            tags=body.get("tags"),
            assets=body.get("assets"),
        )
        if not post:
            return web.json_response({"error": "failed to create post"}, status=400)
        return web.json_response({"ok": True, "post": post.to_dict()})

    async def _handle_network_post_get(self, request: web.Request) -> web.Response:
        """GET /api/network/posts/{post_id} — get a single post with full body."""
        err = self._require_network()
        if err:
            return err
        post = self.network.get_post(request.match_info["post_id"])
        if not post:
            return web.json_response({"error": "post not found"}, status=404)
        return web.json_response({"post": post.full_dict()})

    async def _handle_network_post_delete(self, request: web.Request) -> web.Response:
        """DELETE /api/network/posts/{post_id} — delete a post."""
        err = self._require_network()
        if err:
            return err
        body = await request.json()
        ok = self.network.delete_post(request.match_info["post_id"], body.get("agent_id", ""))
        if not ok:
            return web.json_response({"error": "not found or not authorized"}, status=404)
        return web.json_response({"ok": True})

    async def _handle_network_feed_global(self, request: web.Request) -> web.Response:
        """GET /api/network/feed — global feed across all communities."""
        err = self._require_network()
        if err:
            return err
        sort = request.query.get("sort", "hot")
        limit = min(100, max(1, int(request.query.get("limit", "25"))))
        return web.json_response({
            "posts": self.network.get_global_feed(sort=sort, limit=limit),
        })

    async def _handle_network_feed_personal(self, request: web.Request) -> web.Response:
        """GET /api/network/feed/{agent_id} — personalized feed for an agent."""
        err = self._require_network()
        if err:
            return err
        agent_id = request.match_info["agent_id"]
        sort = request.query.get("sort", "hot")
        limit = min(100, max(1, int(request.query.get("limit", "25"))))
        return web.json_response({
            "posts": self.network.get_feed(agent_id, sort=sort, limit=limit),
        })

    async def _handle_network_vote(self, request: web.Request) -> web.Response:
        """POST /api/network/vote — upvote or downvote a post or comment."""
        err = self._require_network()
        if err:
            return err
        body = await request.json()
        agent_id = body.get("agent_id", "").strip()
        target_id = body.get("target_id", "").strip()
        direction = body.get("direction", "up")
        target_type = body.get("target_type", "post")  # "post" or "comment"
        if not agent_id or not target_id:
            return web.json_response({"error": "agent_id and target_id required"}, status=400)
        if direction not in ("up", "down"):
            return web.json_response({"error": "direction must be 'up' or 'down'"}, status=400)
        if target_type == "comment":
            ok = self.network.vote_comment(target_id, agent_id, direction)
        else:
            ok = self.network.vote_post(target_id, agent_id, direction)
        return web.json_response({"ok": ok})

    async def _handle_network_comments_get(self, request: web.Request) -> web.Response:
        """GET /api/network/posts/{post_id}/comments — get comments on a post."""
        err = self._require_network()
        if err:
            return err
        post_id = request.match_info["post_id"]
        sort = request.query.get("sort", "best")
        return web.json_response({
            "comments": self.network.get_comments(post_id, sort=sort),
        })

    async def _handle_network_comments_create(self, request: web.Request) -> web.Response:
        """POST /api/network/posts/{post_id}/comments — add a comment."""
        err = self._require_network()
        if err:
            return err
        post_id = request.match_info["post_id"]
        body = await request.json()
        author_id = body.get("author_id", "").strip()[:64]
        text = body.get("body", "").strip()
        parent_id = body.get("parent_id")
        if not author_id or not text:
            return web.json_response({"error": "author_id and body required"}, status=400)
        comment = self.network.add_comment(post_id, author_id, text, parent_id=parent_id)
        if not comment:
            return web.json_response({"error": "failed to add comment"}, status=400)
        return web.json_response({"ok": True, "comment": comment.to_dict()})

    async def _handle_network_communities_list(self, request: web.Request) -> web.Response:
        """GET /api/network/communities — list all communities."""
        err = self._require_network()
        if err:
            return err
        sort = request.query.get("sort", "members")
        return web.json_response({
            "communities": self.network.list_communities(sort=sort),
        })

    async def _handle_network_community_get(self, request: web.Request) -> web.Response:
        """GET /api/network/communities/{id} — get community details + posts."""
        err = self._require_network()
        if err:
            return err
        cid = request.match_info["community_id"]
        community = self.network.get_community(cid)
        if not community:
            return web.json_response({"error": "community not found"}, status=404)
        sort = request.query.get("sort", "hot")
        limit = min(100, max(1, int(request.query.get("limit", "25"))))
        return web.json_response({
            "community": community.to_dict(),
            "posts": self.network.get_community_posts(cid, sort=sort, limit=limit),
            "members": self.network.get_community_members(cid)[:100],
        })

    async def _handle_network_community_create(self, request: web.Request) -> web.Response:
        """POST /api/network/communities — create a community."""
        err = self._require_network()
        if err:
            return err
        body = await request.json()
        creator_id = body.get("creator_id", "").strip()[:64]
        community_id = body.get("id", "").strip()[:30]
        display_name = body.get("display_name", "").strip()[:100]
        if not creator_id or not community_id or not display_name:
            return web.json_response({"error": "creator_id, id, and display_name required"}, status=400)
        community = self.network.create_community(
            creator_id=creator_id,
            community_id=community_id,
            display_name=display_name,
            description=body.get("description", ""),
            community_type=body.get("community_type", "general"),
            tags=body.get("tags"),
            assets=body.get("assets"),
        )
        if not community:
            return web.json_response({"error": "failed to create (duplicate id or limit reached)"}, status=400)
        return web.json_response({"ok": True, "community": community.to_dict()})

    async def _handle_network_community_join(self, request: web.Request) -> web.Response:
        """POST /api/network/communities/{id}/join — join a community."""
        err = self._require_network()
        if err:
            return err
        body = await request.json()
        ok = self.network.join_community(body.get("agent_id", ""), request.match_info["community_id"])
        return web.json_response({"ok": ok})

    async def _handle_network_community_leave(self, request: web.Request) -> web.Response:
        """POST /api/network/communities/{id}/leave — leave a community."""
        err = self._require_network()
        if err:
            return err
        body = await request.json()
        ok = self.network.leave_community(body.get("agent_id", ""), request.match_info["community_id"])
        return web.json_response({"ok": ok})

    async def _handle_network_data_feeds_list(self, request: web.Request) -> web.Response:
        """GET /api/network/data-feeds — list available data feeds."""
        err = self._require_network()
        if err:
            return err
        feed_type = request.query.get("type")
        sort = request.query.get("sort", "subscribers")
        return web.json_response({
            "feeds": self.network.list_data_feeds(feed_type=feed_type, sort=sort),
        })

    async def _handle_network_data_feed_create(self, request: web.Request) -> web.Response:
        """POST /api/network/data-feeds — create a data feed."""
        err = self._require_network()
        if err:
            return err
        body = await request.json()
        publisher_id = body.get("publisher_id", "").strip()[:64]
        name = body.get("name", "").strip()
        if not publisher_id or not name:
            return web.json_response({"error": "publisher_id and name required"}, status=400)
        feed = self.network.create_data_feed(
            publisher_id=publisher_id,
            name=name,
            description=body.get("description", ""),
            feed_type=body.get("feed_type", "custom"),
            assets=body.get("assets"),
            public=body.get("public", True),
        )
        if not feed:
            return web.json_response({"error": "failed to create feed"}, status=400)
        return web.json_response({"ok": True, "feed": feed.to_dict()})

    async def _handle_network_data_feed_publish(self, request: web.Request) -> web.Response:
        """POST /api/network/data-feeds/{feed_id}/publish — publish an update."""
        err = self._require_network()
        if err:
            return err
        feed_id = request.match_info["feed_id"]
        body = await request.json()
        publisher_id = body.get("publisher_id", "").strip()
        data = body.get("data", {})
        summary = body.get("summary", "")
        if not publisher_id or not data:
            return web.json_response({"error": "publisher_id and data required"}, status=400)
        ok = self.network.publish_feed_update(feed_id, publisher_id, data, summary)
        if not ok:
            return web.json_response({"error": "feed not found or not authorized"}, status=404)
        return web.json_response({"ok": True})

    async def _handle_network_data_feed_subscribe(self, request: web.Request) -> web.Response:
        """POST /api/network/data-feeds/{feed_id}/subscribe — subscribe."""
        err = self._require_network()
        if err:
            return err
        body = await request.json()
        ok = self.network.subscribe_to_feed(body.get("agent_id", ""), request.match_info["feed_id"])
        return web.json_response({"ok": ok})

    async def _handle_network_data_feed_unsubscribe(self, request: web.Request) -> web.Response:
        """DELETE /api/network/data-feeds/{feed_id}/subscribe — unsubscribe."""
        err = self._require_network()
        if err:
            return err
        body = await request.json()
        ok = self.network.unsubscribe_from_feed(body.get("agent_id", ""), request.match_info["feed_id"])
        return web.json_response({"ok": ok})

    async def _handle_network_data_feed_updates(self, request: web.Request) -> web.Response:
        """GET /api/network/data-feeds/{feed_id}/updates — get recent updates."""
        err = self._require_network()
        if err:
            return err
        feed_id = request.match_info["feed_id"]
        limit = min(100, max(1, int(request.query.get("limit", "20"))))
        return web.json_response({
            "updates": self.network.get_feed_updates(feed_id, limit=limit),
        })

    async def _handle_network_dm_send(self, request: web.Request) -> web.Response:
        """POST /api/network/dm — send a direct message."""
        err = self._require_network()
        if err:
            return err
        body = await request.json()
        sender_id = body.get("sender_id", "").strip()[:64]
        receiver_id = body.get("receiver_id", "").strip()[:64]
        text = body.get("body", "").strip()
        if not sender_id or not receiver_id or not text:
            return web.json_response({"error": "sender_id, receiver_id, and body required"}, status=400)
        msg = self.network.send_dm(sender_id, receiver_id, text,
                                   structured_data=body.get("structured_data"))
        if not msg:
            return web.json_response({"error": "failed to send (same agent or limit reached)"}, status=400)
        return web.json_response({"ok": True, "message": msg.to_dict()})

    async def _handle_network_dm_accept(self, request: web.Request) -> web.Response:
        """POST /api/network/dm/accept — accept a DM request."""
        err = self._require_network()
        if err:
            return err
        body = await request.json()
        ok = self.network.accept_dm(body.get("agent_id", ""), body.get("other_id", ""))
        return web.json_response({"ok": ok})

    async def _handle_network_dm_thread(self, request: web.Request) -> web.Response:
        """GET /api/network/dm/{agent_id}/{other_id} — get DM thread."""
        err = self._require_network()
        if err:
            return err
        agent_id = request.match_info["agent_id"]
        other_id = request.match_info["other_id"]
        thread = self.network.get_dm_thread(agent_id, other_id)
        if not thread:
            return web.json_response({"error": "no thread found"}, status=404)
        return web.json_response(thread)

    async def _handle_network_dm_threads(self, request: web.Request) -> web.Response:
        """GET /api/network/dm/{agent_id} — all DM threads for an agent."""
        err = self._require_network()
        if err:
            return err
        agent_id = request.match_info["agent_id"]
        return web.json_response({
            "threads": self.network.get_dm_threads(agent_id),
            "pending_requests": self.network.get_dm_requests(agent_id),
        })

    async def _handle_network_search(self, request: web.Request) -> web.Response:
        """GET /api/network/search — search posts."""
        err = self._require_network()
        if err:
            return err
        query = request.query.get("q", "")
        community_id = request.query.get("community")
        post_type = request.query.get("type")
        assets = request.query.get("assets", "").split(",") if request.query.get("assets") else None
        limit = min(50, max(1, int(request.query.get("limit", "25"))))
        return web.json_response({
            "results": self.network.search_posts(query, community_id=community_id,
                                                  post_type=post_type, assets=assets, limit=limit),
        })

    async def _handle_network_stats(self, request: web.Request) -> web.Response:
        """GET /api/network/stats — platform stats."""
        err = self._require_network()
        if err:
            return err
        return web.json_response(self.network.platform_stats())

    # ── Server Lifecycle ────────────────────────────────────────────

    async def start(self):
        # Auth token: from env, or auto-generate and print to console
        token = os.environ.get("SWARM_DASHBOARD_TOKEN", "")
        mode = os.environ.get("SWARM_MODE", "mock")
        if not token:
            if mode == "live":
                raise RuntimeError(
                    "SWARM_DASHBOARD_TOKEN must be set in live mode — "
                    "auto-generated tokens are not allowed when trading real money"
                )
            token = _generate_token()
            log.warning("No SWARM_DASHBOARD_TOKEN set — auto-generated (set env var to persist)")
            import sys
            print(f"\n  Dashboard token: {token}\n", file=sys.stderr)

        app = web.Application(middlewares=[
            security_headers_middleware, rate_limit_middleware, auth_middleware,
        ])
        app["_dashboard_token"] = token
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/metrics", self._handle_metrics)
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/api/history", self._handle_history)
        app.router.add_get("/api/state", self._handle_state)
        app.router.add_get("/api/wallet", self._handle_wallet)
        app.router.add_post("/api/wallet/deposit", self._handle_wallet_deposit)
        app.router.add_post("/api/wallet/withdraw", self._handle_wallet_withdraw)
        app.router.add_post("/api/wallet/allocations", self._handle_wallet_allocations)
        app.router.add_get("/api/report", self._handle_report_json)
        app.router.add_get("/api/thoughts", self._handle_thoughts)
        app.router.add_get("/api/memory", self._handle_memory)
        app.router.add_get("/report", self._handle_report_html)
        app.router.add_get("/slides", self._handle_slides)
        # ── Competition features ───────────────────────────────────
        app.router.add_post("/api/strategy/nlp", self._handle_nlp_strategy)
        app.router.add_get("/api/strategy/presets", self._handle_strategy_presets)
        app.router.add_get("/api/strategy/weights", self._handle_strategy_weights)
        app.router.add_get("/api/leaderboard", self._handle_leaderboard)
        app.router.add_get("/api/confidence", self._handle_confidence)
        app.router.add_post("/api/strategy/commit", self._handle_strategy_commit)
        app.router.add_post("/api/strategy/reveal", self._handle_strategy_reveal)
        app.router.add_get("/api/pyth", self._handle_pyth_prices)
        # ── Emergency controls ─────────────────────────────────────
        app.router.add_post("/api/cancel-all", self._handle_cancel_all)
        app.router.add_post("/api/flatten", self._handle_flatten)
        app.router.add_post("/api/pause", self._handle_pause)
        # ── ERC-8004 on-chain status ──────────────────────────────
        app.router.add_get("/api/erc8004", self._handle_erc8004_status)
        app.router.add_get("/api/uniswap", self._handle_uniswap_status)
        # ── Gateway management (frontend-facing) ───────────────────
        app.router.add_get("/api/gateway/status", self._handle_gateway_status)
        app.router.add_post("/api/gateway/ui/connect", self._handle_gateway_connect_agent)
        app.router.add_post("/api/gateway/ui/disconnect", self._handle_gateway_disconnect_agent)
        # ── Social trading ────────────────────────────────────────
        app.router.add_get("/api/social/profiles", self._handle_social_profiles)
        app.router.add_get("/api/social/profile/{agent_id}", self._handle_social_profile)
        app.router.add_post("/api/social/profile", self._handle_social_create_profile)
        app.router.add_post("/api/social/follow", self._handle_social_follow)
        app.router.add_post("/api/social/unfollow", self._handle_social_unfollow)
        app.router.add_post("/api/social/copy", self._handle_social_copy_start)
        app.router.add_post("/api/social/copy/stop", self._handle_social_copy_stop)
        app.router.add_get("/api/social/feed", self._handle_social_feed)
        app.router.add_get("/api/social/feed/{agent_id}", self._handle_social_feed_personal)
        app.router.add_get("/api/social/leaderboard", self._handle_social_leaderboard)
        app.router.add_get("/api/social/referral/{agent_id}", self._handle_social_referral)
        app.router.add_get("/api/social/search", self._handle_social_search)
        app.router.add_get("/api/social/stats", self._handle_social_stats)
        app.router.add_post("/api/social/comment", self._handle_social_comment)
        app.router.add_post("/api/social/like", self._handle_social_like)
        app.router.add_get("/api/social/achievements/{agent_id}", self._handle_social_achievements)
        # ── SwarmNetwork (agent social network) ─────────────────────
        app.router.add_get("/api/network/home", self._handle_network_home)
        app.router.add_get("/api/network/feed", self._handle_network_feed_global)
        app.router.add_get("/api/network/feed/{agent_id}", self._handle_network_feed_personal)
        app.router.add_post("/api/network/posts", self._handle_network_posts_create)
        app.router.add_get("/api/network/posts/{post_id}", self._handle_network_post_get)
        app.router.add_delete("/api/network/posts/{post_id}", self._handle_network_post_delete)
        app.router.add_get("/api/network/posts/{post_id}/comments", self._handle_network_comments_get)
        app.router.add_post("/api/network/posts/{post_id}/comments", self._handle_network_comments_create)
        app.router.add_post("/api/network/vote", self._handle_network_vote)
        app.router.add_get("/api/network/communities", self._handle_network_communities_list)
        app.router.add_get("/api/network/communities/{community_id}", self._handle_network_community_get)
        app.router.add_post("/api/network/communities", self._handle_network_community_create)
        app.router.add_post("/api/network/communities/{community_id}/join", self._handle_network_community_join)
        app.router.add_post("/api/network/communities/{community_id}/leave", self._handle_network_community_leave)
        app.router.add_get("/api/network/data-feeds", self._handle_network_data_feeds_list)
        app.router.add_post("/api/network/data-feeds", self._handle_network_data_feed_create)
        app.router.add_post("/api/network/data-feeds/{feed_id}/publish", self._handle_network_data_feed_publish)
        app.router.add_post("/api/network/data-feeds/{feed_id}/subscribe", self._handle_network_data_feed_subscribe)
        app.router.add_delete("/api/network/data-feeds/{feed_id}/subscribe", self._handle_network_data_feed_unsubscribe)
        app.router.add_get("/api/network/data-feeds/{feed_id}/updates", self._handle_network_data_feed_updates)
        app.router.add_post("/api/network/dm", self._handle_network_dm_send)
        app.router.add_post("/api/network/dm/accept", self._handle_network_dm_accept)
        app.router.add_get("/api/network/dm/{agent_id}", self._handle_network_dm_threads)
        app.router.add_get("/api/network/dm/{agent_id}/{other_id}", self._handle_network_dm_thread)
        app.router.add_get("/api/network/search", self._handle_network_search)
        app.router.add_get("/api/network/stats", self._handle_network_stats)
        # ── Phase 1-40 endpoints ─────────────────────────────────
        app.router.add_get("/api/commander", self._handle_commander)
        app.router.add_get("/api/vault", self._handle_vault)
        app.router.add_get("/api/marketplace", self._handle_marketplace)
        app.router.add_get("/api/debates", self._handle_debates)
        app.router.add_get("/api/elo", self._handle_elo)
        app.router.add_get("/api/narratives", self._handle_narratives)
        app.router.add_get("/api/consensus", self._handle_consensus)
        app.router.add_get("/api/observability", self._handle_observability)
        app.router.add_get("/api/treasury", self._handle_treasury)
        app.router.add_get("/api/governance", self._handle_governance)
        app.router.add_get("/api/evolution", self._handle_evolution)
        app.router.add_get("/api/grid", self._handle_grid)
        # ── Operational endpoints ──────────────────────────────────
        app.router.add_get("/api/migrations", self._handle_migration_status)
        # ── Agent Gateway routes (external agent API) ──────────────
        if self.gateway:
            self.gateway.register_routes(app)

        # ── API v1 aliases ─────────────────────────────────────────
        # All /api/* routes are also available under /api/v1/* for
        # forward compatibility. External agents should use /api/v1/.
        _v1_redirect = {}
        for resource in list(app.router.resources()):
            info = resource.get_info()
            fmt = info.get("formatter", info.get("path", ""))
            if isinstance(fmt, str) and fmt.startswith("/api/") and not fmt.startswith("/api/v1/"):
                v1_path = fmt.replace("/api/", "/api/v1/", 1)
                _v1_redirect[fmt] = v1_path
        for resource in list(app.router.resources()):
            info = resource.get_info()
            fmt = info.get("formatter", info.get("path", ""))
            if fmt in _v1_redirect:
                v1_path = _v1_redirect[fmt]
                for route in resource:
                    method = route.method
                    handler = route.handler
                    if method == "GET":
                        app.router.add_get(v1_path, handler)
                    elif method == "POST":
                        app.router.add_post(v1_path, handler)
                    elif method == "DELETE":
                        app.router.add_delete(v1_path, handler)
                    elif method == "PATCH":
                        app.router.add_patch(v1_path, handler)

        app.router.add_static("/static/",
                              Path(__file__).parent / "static",
                              show_index=False)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("Dashboard running at http://%s:%d", self.host, self.port)

    async def stop(self):
        """Gracefully shut down the web server and close all WebSocket clients."""
        # Close all WebSocket connections with a proper close frame
        for ws in list(self._clients):
            try:
                await ws.close(code=1001, message=b"server shutting down")
            except Exception:
                pass
        self._clients.clear()
        self._cmd_times.clear()

        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        log.info("Dashboard stopped")