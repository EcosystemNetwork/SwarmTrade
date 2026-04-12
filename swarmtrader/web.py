"""Web dashboard — aiohttp server with WebSocket for real-time Bus events."""
from __future__ import annotations
import asyncio, json, logging, time, sqlite3
from pathlib import Path
from aiohttp import web
from .core import Bus, MarketSnapshot, Signal, TradeIntent, RiskVerdict, ExecutionReport

log = logging.getLogger("swarm.web")


class WebDashboard:
    """Serves the frontend and streams Bus events over WebSocket."""

    def __init__(self, bus: Bus, state: dict, db_path: Path,
                 kill_switch: Path, host: str = "0.0.0.0", port: int = 8080):
        self.bus = bus
        self.state = state
        self.db_path = db_path
        self.kill_switch = kill_switch
        self.host = host
        self.port = port
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
                       "signal.prism_volume", "signal.prism_breakout"):
            bus.subscribe(topic, self._on_signal)
        bus.subscribe("intent.new", self._on_intent)
        bus.subscribe("risk.verdict", self._on_verdict)
        bus.subscribe("exec.report", self._on_report)

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
            ("Auditor", "audit", "audit.log"),
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
                     "drawdown": "RiskAgent_Drawdown"}
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
                    "kill_switch": self.kill_switch.exists(),
                },
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
                self.kill_switch.touch()
            else:
                self.kill_switch.unlink(missing_ok=True)
            await self._broadcast("kill_switch", {"enabled": self.kill_switch.exists()})

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
            "kill_switch": self.kill_switch.exists(),
        })

    async def _handle_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(Path(__file__).parent / "static" / "index.html")

    # ── Server Lifecycle ────────────────────────────────────────────

    async def start(self):
        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/api/history", self._handle_history)
        app.router.add_get("/api/state", self._handle_state)
        app.router.add_static("/static/",
                              Path(__file__).parent / "static",
                              show_index=False)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        log.info("Dashboard running at http://%s:%d", self.host, self.port)
