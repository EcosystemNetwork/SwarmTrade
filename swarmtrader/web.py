"""Web dashboard — aiohttp server with WebSocket for real-time Bus events.

Security: All API and WebSocket endpoints require a bearer token set via
SWARM_DASHBOARD_TOKEN env var (or auto-generated on startup).
Static assets (CSS/JS) are unauthenticated.
"""
from __future__ import annotations
import asyncio, hashlib, json, logging, os, secrets, time, sqlite3
from pathlib import Path
from aiohttp import web
from .core import Bus, MarketSnapshot, Signal, TradeIntent, RiskVerdict, ExecutionReport
from .report import load_from_db, generate_html_report
from .gateway import AgentGateway

log = logging.getLogger("swarm.web")


def _generate_token() -> str:
    """Generate a cryptographically random dashboard token."""
    return secrets.token_urlsafe(32)


def _constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    return hashlib.sha256(a.encode()).digest() == hashlib.sha256(b.encode()).digest()


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Require Bearer token for all API/WS endpoints.

    Unauthenticated paths: static assets, health check.
    """
    path = request.path

    # Allow static assets and health check without auth
    if path.startswith("/static/") or path == "/health":
        return await handler(request)

    token = request.app.get("_dashboard_token")
    if not token:
        # No token configured — auth disabled (dev mode)
        return await handler(request)

    # Check Authorization header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        provided = auth_header[7:]
        if _constant_time_compare(provided, token):
            return await handler(request)

    # Check query param (for WebSocket connections from browsers)
    provided = request.query.get("token", "")
    if provided and _constant_time_compare(provided, token):
        return await handler(request)

    return web.json_response(
        {"error": "unauthorized", "hint": "Set Authorization: Bearer <token> header"},
        status=401,
    )


class WebDashboard:
    """Serves the frontend and streams Bus events over WebSocket."""

    def __init__(self, bus: Bus, state: dict, db_path: Path,
                 kill_switch, host: str = "0.0.0.0", port: int = 8080,
                 wallet=None, gateway: AgentGateway | None = None):
        self.bus = bus
        self.state = state
        self.db_path = db_path
        self.kill_switch = kill_switch
        self.host = host
        self.port = port
        self.wallet = wallet  # WalletManager instance (optional)
        self.gateway = gateway  # AgentGateway instance (optional)
        self._clients: set[web.WebSocketResponse] = set()

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

        self._init_agents()

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

    async def _broadcast(self, event_type: str, data: dict):
        msg = json.dumps({"type": event_type, "ts": time.time(), "data": data})
        dead = set()
        for ws in self._clients:
            try:
                await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

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
        await self._broadcast("intent", d)

    async def _on_verdict(self, v: RiskVerdict):
        d = {
            "intent_id": v.intent_id, "agent_id": v.agent_id,
            "approve": v.approve, "reason": v.reason,
        }
        self.verdicts.setdefault(v.intent_id, []).append(d)

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

    # ── HTTP Handlers ───────────────────────────────────────────────

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        log.info("WebSocket client connected (%d total)", len(self._clients))

        # Send full state snapshot to new client
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
            },
        }
        await ws.send_str(json.dumps(snapshot))

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

        self._clients.discard(ws)
        log.info("WebSocket client disconnected (%d remaining)", len(self._clients))
        return ws

    async def _handle_cmd(self, cmd: dict, ws: web.WebSocketResponse):
        action = cmd.get("action")
        if action == "kill_switch":
            enabled = cmd.get("enabled", False)
            if enabled:
                self.kill_switch.engage("manual via dashboard")
            else:
                self.kill_switch.disengage()
            await self._broadcast("kill_switch", {"enabled": self.kill_switch.active})
        elif action == "wallet_deposit" and self.wallet:
            amount = float(cmd.get("amount", 0))
            if amount > 0:
                self.wallet.deposit(amount, cmd.get("note", ""))
                await self._broadcast("wallet", self.wallet.summary())
        elif action == "wallet_withdraw" and self.wallet:
            amount = float(cmd.get("amount", 0))
            if amount > 0:
                result = self.wallet.withdraw(amount, cmd.get("note", ""))
                resp = self.wallet.summary()
                if result is None:
                    resp["error"] = "insufficient funds"
                await self._broadcast("wallet", resp)
        elif action == "wallet_set_allocation" and self.wallet:
            asset = cmd.get("asset", "")
            if asset:
                self.wallet.set_allocation(
                    asset,
                    target_pct=float(cmd.get("target_pct", 0)),
                    max_pct=float(cmd.get("max_pct", 50)),
                )
                await self._broadcast("wallet", self.wallet.summary())

    async def _handle_history(self, request: web.Request) -> web.Response:
        """Query trade history from SQLite."""
        limit = int(request.query.get("limit", "100"))
        status = request.query.get("status", None)
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            if status:
                rows = conn.execute(
                    "SELECT * FROM reports WHERE status=? ORDER BY ts DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM reports ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
            intents = conn.execute(
                "SELECT * FROM intents ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            conn.close()
            return web.json_response({
                "reports": [dict(r) for r in rows],
                "intents": [dict(i) for i in intents],
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

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
        amount = float(body.get("amount", 0))
        if amount <= 0:
            return web.json_response({"error": "amount must be positive"}, status=400)
        self.wallet.deposit(amount, body.get("note", ""))
        return web.json_response(self.wallet.summary())

    async def _handle_wallet_withdraw(self, request: web.Request) -> web.Response:
        """POST /api/wallet/withdraw {amount, note}"""
        if not self.wallet:
            return web.json_response({"error": "wallet not configured"}, status=404)
        body = await request.json()
        amount = float(body.get("amount", 0))
        if amount <= 0:
            return web.json_response({"error": "amount must be positive"}, status=400)
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
        self.wallet.set_allocation(
            asset,
            target_pct=float(body.get("target_pct", 0)),
            max_pct=float(body.get("max_pct", 50)),
        )
        return web.json_response(self.wallet.summary())

    async def _handle_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(Path(__file__).parent / "static" / "index.html")

    async def _handle_report_json(self, request: web.Request) -> web.Response:
        try:
            report = load_from_db(self.db_path)
            return web.json_response(report.to_dict())
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_report_html(self, request: web.Request) -> web.Response:
        try:
            report = load_from_db(self.db_path)
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
                out = Path(f.name)
            generate_html_report(report, out)
            html = out.read_text()
            out.unlink()
            return web.Response(text=html, content_type="text/html")
        except Exception as e:
            return web.Response(text=f"Error: {e}", status=500)

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Unauthenticated health check for monitoring."""
        return web.json_response({"status": "ok", "uptime": time.time() - self.start_time})

    async def _handle_slides(self, request: web.Request) -> web.FileResponse:
        slides_path = Path(__file__).parent / "static" / "slides.html"
        if slides_path.exists():
            return web.FileResponse(slides_path)
        return web.Response(text="Slides not found", status=404)

    # ── Server Lifecycle ────────────────────────────────────────────

    async def start(self):
        # Auth token: from env, or auto-generate and print to console
        token = os.environ.get("SWARM_DASHBOARD_TOKEN", "")
        if not token:
            token = _generate_token()
            log.warning("No SWARM_DASHBOARD_TOKEN set — generated: %s", token)
            log.warning("Set this env var to persist across restarts.")

        app = web.Application(middlewares=[auth_middleware])
        app["_dashboard_token"] = token
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/api/history", self._handle_history)
        app.router.add_get("/api/state", self._handle_state)
        app.router.add_get("/api/wallet", self._handle_wallet)
        app.router.add_post("/api/wallet/deposit", self._handle_wallet_deposit)
        app.router.add_post("/api/wallet/withdraw", self._handle_wallet_withdraw)
        app.router.add_post("/api/wallet/allocations", self._handle_wallet_allocations)
        app.router.add_get("/api/report", self._handle_report_json)
        app.router.add_get("/report", self._handle_report_html)
        app.router.add_get("/slides", self._handle_slides)
        # ── Agent Gateway routes ────────────────────────────────────
        if self.gateway:
            self.gateway.register_routes(app)

        app.router.add_static("/static/",
                              Path(__file__).parent / "static",
                              show_index=False)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        log.info("Dashboard running at http://%s:%d", self.host, self.port)
