"""Agent Gateway — HTTP + WebSocket bridge for external AI agents.

Supports:
  - OpenClaw agents (tool-calling protocol)
  - Hermes agents (message-passing protocol)
  - Raw HTTP/WebSocket agents (any framework)

External agents connect, authenticate, receive market data, and publish
trading signals that flow into the Strategist via the internal Bus.

Endpoints:
  POST /api/gateway/connect      — register an external agent
  POST /api/gateway/signal       — publish a trading signal
  GET  /api/gateway/market       — current market snapshot
  GET  /api/gateway/agents       — list connected agents
  GET  /api/gateway/portfolio    — current portfolio state
  DELETE /api/gateway/disconnect — disconnect an agent
  WS   /ws/agent                 — real-time bidirectional stream
"""
from __future__ import annotations
import asyncio, hashlib, hmac, json, logging, secrets, time
from dataclasses import dataclass, field
from typing import Any
from aiohttp import web
from .core import Bus, Signal, MarketSnapshot, ExecutionReport

log = logging.getLogger("swarm.gateway")

# Default weight assigned to newly connected external agents
EXTERNAL_AGENT_DEFAULT_WEIGHT = 0.06


@dataclass
class ConnectedAgent:
    """Tracks a connected external agent."""
    agent_id: str
    name: str
    protocol: str           # "openclaw" | "hermes" | "raw"
    api_key: str
    connected_at: float = field(default_factory=time.time)
    last_signal_at: float = 0.0
    signal_count: int = 0
    weight: float = EXTERNAL_AGENT_DEFAULT_WEIGHT
    capabilities: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    # Rate limiting
    _signal_times: list[float] = field(default_factory=list)
    max_signals_per_minute: int = 30


class AgentGateway:
    """Bridge between external AI agents and the internal swarm Bus.

    Provides REST + WebSocket endpoints. External agents publish signals
    that get translated into internal Signal objects and flow through
    the Strategist's normal weighting + risk quorum pipeline.
    """

    def __init__(self, bus: Bus, strategist=None, portfolio=None,
                 master_key: str | None = None):
        self.bus = bus
        self.strategist = strategist
        self.portfolio = portfolio
        self.master_key = master_key or secrets.token_hex(32)
        self.agents: dict[str, ConnectedAgent] = {}
        self._ws_clients: dict[str, web.WebSocketResponse] = {}  # agent_id -> ws
        self._prices: dict[str, float] = {}
        self._latest_signals: dict[str, dict] = {}

        # Subscribe to bus events to relay to external agents
        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("exec.report", self._on_exec_report)

        # Subscribe strategist to our external agent signals
        for topic_suffix in ("external",):
            pass  # dynamic — we subscribe when agents connect

        log.info("Agent Gateway initialized (master_key=%s...)", self.master_key[:8])

    # ── Bus event handlers (relay to connected agents) ──────────────

    async def _on_snapshot(self, snap: MarketSnapshot):
        self._prices = dict(snap.prices)
        await self._broadcast_to_agents({
            "type": "market",
            "ts": snap.ts,
            "prices": snap.prices,
            "gas_gwei": snap.gas_gwei,
        })

    async def _on_exec_report(self, report: ExecutionReport):
        await self._broadcast_to_agents({
            "type": "execution",
            "ts": time.time(),
            "intent_id": report.intent_id,
            "status": report.status,
            "fill_price": report.fill_price,
            "pnl": report.pnl_estimate,
            "asset": report.asset,
            "side": report.side,
        })

    async def _broadcast_to_agents(self, msg: dict):
        dead = []
        payload = json.dumps(msg)
        for agent_id, ws in self._ws_clients.items():
            try:
                await ws.send_str(payload)
            except Exception:
                dead.append(agent_id)
        for agent_id in dead:
            self._ws_clients.pop(agent_id, None)

    # ── Authentication ──────────────────────────────────────────────

    def _verify_key(self, api_key: str) -> ConnectedAgent | None:
        for agent in self.agents.values():
            if hmac.compare_digest(agent.api_key, api_key):
                return agent
        return None

    def _extract_key(self, request: web.Request) -> str | None:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return request.headers.get("X-API-Key") or request.query.get("api_key")

    # ── Rate limiting ───────────────────────────────────────────────

    def _check_rate_limit(self, agent: ConnectedAgent) -> bool:
        now = time.time()
        cutoff = now - 60.0
        agent._signal_times = [t for t in agent._signal_times if t > cutoff]
        if len(agent._signal_times) >= agent.max_signals_per_minute:
            return False
        agent._signal_times.append(now)
        return True

    # ── Protocol adapters ───────────────────────────────────────────

    def _parse_openclaw_signal(self, body: dict) -> dict:
        """Parse OpenClaw tool-call format into normalized signal.

        OpenClaw agents use tool_calls with structured arguments:
        {
            "tool_calls": [{
                "name": "submit_signal",
                "arguments": {
                    "asset": "ETH",
                    "action": "buy",      // buy | sell | hold
                    "conviction": 0.8,    // 0-1
                    "reasoning": "..."
                }
            }]
        }
        """
        tool_calls = body.get("tool_calls", [])
        if not tool_calls:
            # Also accept flat format
            return self._parse_raw_signal(body)

        call = tool_calls[0]
        args = call.get("arguments", call.get("args", {}))
        action = args.get("action", "hold").lower()
        direction = {"buy": "long", "sell": "short", "hold": "flat"}.get(action, "flat")
        conviction = float(args.get("conviction", args.get("confidence", 0.5)))

        return {
            "asset": args.get("asset", args.get("symbol", "ETH")).upper().replace("USD", ""),
            "direction": direction,
            "strength": conviction if direction == "long" else -conviction if direction == "short" else 0.0,
            "confidence": conviction,
            "rationale": args.get("reasoning", args.get("rationale", "")),
        }

    def _parse_hermes_signal(self, body: dict) -> dict:
        """Parse Hermes agent message format into normalized signal.

        Hermes agents send structured messages:
        {
            "role": "assistant",
            "content": "...",           // natural language analysis
            "structured_output": {      // or "tool_output" / "output"
                "signal": "bullish",    // bullish | bearish | neutral
                "asset": "ETH",
                "confidence": 0.75,
                "target_price": 2500,
                "stop_loss": 2200,
                "timeframe": "4h"
            }
        }
        """
        structured = (body.get("structured_output")
                       or body.get("tool_output")
                       or body.get("output")
                       or body.get("signal_data")
                       or {})

        # If no structured output, try to find signal in content
        if not structured and "content" in body:
            return self._parse_raw_signal(body)

        signal_str = str(structured.get("signal", "neutral")).lower()
        direction = {"bullish": "long", "bearish": "short", "neutral": "flat",
                     "long": "long", "short": "short", "buy": "long", "sell": "short"
                     }.get(signal_str, "flat")
        confidence = float(structured.get("confidence", 0.5))

        rationale_parts = []
        if body.get("content"):
            rationale_parts.append(str(body["content"])[:200])
        if structured.get("target_price"):
            rationale_parts.append(f"target={structured['target_price']}")
        if structured.get("stop_loss"):
            rationale_parts.append(f"sl={structured['stop_loss']}")
        if structured.get("timeframe"):
            rationale_parts.append(f"tf={structured['timeframe']}")

        return {
            "asset": str(structured.get("asset", "ETH")).upper().replace("USD", ""),
            "direction": direction,
            "strength": confidence if direction == "long" else -confidence if direction == "short" else 0.0,
            "confidence": confidence,
            "rationale": " | ".join(rationale_parts) if rationale_parts else "",
        }

    def _parse_raw_signal(self, body: dict) -> dict:
        """Parse a raw/generic signal format."""
        direction = str(body.get("direction", body.get("side", "flat"))).lower()
        direction = {"buy": "long", "sell": "short", "bullish": "long",
                     "bearish": "short"}.get(direction, direction)
        if direction not in ("long", "short", "flat"):
            direction = "flat"

        strength = float(body.get("strength", body.get("score", 0.0)))
        confidence = float(body.get("confidence", 0.5))

        # Auto-derive strength from direction + confidence if not explicit
        if abs(strength) < 1e-9 and direction != "flat":
            strength = confidence if direction == "long" else -confidence

        return {
            "asset": str(body.get("asset", body.get("symbol", "ETH"))).upper().replace("USD", ""),
            "direction": direction,
            "strength": max(-1.0, min(1.0, strength)),
            "confidence": max(0.0, min(1.0, confidence)),
            "rationale": str(body.get("rationale", body.get("reasoning", body.get("content", "")))),
        }

    # ── HTTP Handlers ───────────────────────────────────────────────

    async def handle_connect(self, request: web.Request) -> web.Response:
        """POST /api/gateway/connect — register an external agent.

        Body: {
            "name": "my-hermes-agent",
            "protocol": "hermes",          // openclaw | hermes | raw
            "capabilities": ["spot", "futures"],
            "master_key": "..."            // required for first connect
        }

        Returns: { "agent_id": "...", "api_key": "...", "weight": 0.06 }
        """
        body = await request.json()
        master = body.get("master_key", "")
        if not hmac.compare_digest(master, self.master_key):
            return web.json_response({"error": "invalid master_key"}, status=401)

        name = body.get("name", f"external_{len(self.agents)}")
        protocol = body.get("protocol", "raw")
        if protocol not in ("openclaw", "hermes", "raw"):
            return web.json_response({"error": "protocol must be openclaw, hermes, or raw"}, status=400)

        agent_id = f"ext_{name.lower().replace(' ', '_').replace('-', '_')}"
        api_key = secrets.token_hex(32)
        weight = float(body.get("weight", EXTERNAL_AGENT_DEFAULT_WEIGHT))

        agent = ConnectedAgent(
            agent_id=agent_id,
            name=name,
            protocol=protocol,
            api_key=api_key,
            weight=weight,
            capabilities=body.get("capabilities", []),
            metadata=body.get("metadata", {}),
        )
        self.agents[agent_id] = agent

        # Register with Strategist weights
        if self.strategist:
            self.strategist.weights[agent_id] = weight
            # Subscribe strategist to this agent's signal topic
            self.bus.subscribe(f"signal.{agent_id}", self.strategist._on_signal)
            # Renormalize
            total = sum(self.strategist.weights.values()) or 1.0
            for k in self.strategist.weights:
                self.strategist.weights[k] /= total

        log.info("AGENT CONNECTED: %s protocol=%s weight=%.3f", agent_id, protocol, weight)

        return web.json_response({
            "agent_id": agent_id,
            "api_key": api_key,
            "weight": weight,
            "protocol": protocol,
            "bus_topic": f"signal.{agent_id}",
            "endpoints": {
                "signal": "POST /api/gateway/signal",
                "market": "GET /api/gateway/market",
                "portfolio": "GET /api/gateway/portfolio",
                "websocket": "WS /ws/agent",
            },
        })

    async def handle_signal(self, request: web.Request) -> web.Response:
        """POST /api/gateway/signal — publish a trading signal.

        Accepts OpenClaw, Hermes, or raw signal format.
        Auth via Authorization: Bearer <api_key> or X-API-Key header.
        """
        api_key = self._extract_key(request)
        if not api_key:
            return web.json_response({"error": "missing api_key"}, status=401)

        agent = self._verify_key(api_key)
        if not agent:
            return web.json_response({"error": "invalid api_key"}, status=401)

        if not self._check_rate_limit(agent):
            return web.json_response({
                "error": "rate limit exceeded",
                "limit": agent.max_signals_per_minute,
                "retry_after": 60,
            }, status=429)

        body = await request.json()

        # Parse based on protocol
        if agent.protocol == "openclaw":
            parsed = self._parse_openclaw_signal(body)
        elif agent.protocol == "hermes":
            parsed = self._parse_hermes_signal(body)
        else:
            parsed = self._parse_raw_signal(body)

        # Build internal Signal
        signal = Signal(
            agent_id=agent.agent_id,
            asset=parsed["asset"],
            direction=parsed["direction"],
            strength=parsed["strength"],
            confidence=parsed["confidence"],
            rationale=f"[{agent.name}] {parsed['rationale']}"[:500],
        )

        # Publish to bus
        await self.bus.publish(f"signal.{agent.agent_id}", signal)

        agent.last_signal_at = time.time()
        agent.signal_count += 1
        self._latest_signals[agent.agent_id] = {
            "agent_id": agent.agent_id,
            "asset": signal.asset,
            "direction": signal.direction,
            "strength": round(signal.strength, 4),
            "confidence": round(signal.confidence, 4),
            "ts": signal.ts,
        }

        log.info("SIGNAL from %s: %s %s str=%.2f conf=%.2f",
                 agent.agent_id, signal.direction, signal.asset,
                 signal.strength, signal.confidence)

        return web.json_response({
            "accepted": True,
            "signal": {
                "agent_id": signal.agent_id,
                "asset": signal.asset,
                "direction": signal.direction,
                "strength": round(signal.strength, 4),
                "confidence": round(signal.confidence, 4),
            },
            "signal_count": agent.signal_count,
        })

    async def handle_market(self, request: web.Request) -> web.Response:
        """GET /api/gateway/market — current prices + portfolio state."""
        api_key = self._extract_key(request)
        if not api_key:
            return web.json_response({"error": "missing api_key"}, status=401)
        if not self._verify_key(api_key):
            return web.json_response({"error": "invalid api_key"}, status=401)

        data: dict[str, Any] = {
            "ts": time.time(),
            "prices": self._prices,
        }
        if self.portfolio:
            data["portfolio"] = self.portfolio.summary()
        return web.json_response(data)

    async def handle_portfolio(self, request: web.Request) -> web.Response:
        """GET /api/gateway/portfolio — detailed portfolio state."""
        api_key = self._extract_key(request)
        if not api_key:
            return web.json_response({"error": "missing api_key"}, status=401)
        if not self._verify_key(api_key):
            return web.json_response({"error": "invalid api_key"}, status=401)

        if not self.portfolio:
            return web.json_response({"error": "portfolio not available"}, status=404)

        return web.json_response({
            "ts": time.time(),
            "portfolio": self.portfolio.summary(),
            "prices": self._prices,
            "active_signals": self._latest_signals,
        })

    async def handle_agents_list(self, request: web.Request) -> web.Response:
        """GET /api/gateway/agents — list connected agents."""
        agents = []
        for a in self.agents.values():
            agents.append({
                "agent_id": a.agent_id,
                "name": a.name,
                "protocol": a.protocol,
                "connected_at": a.connected_at,
                "last_signal_at": a.last_signal_at,
                "signal_count": a.signal_count,
                "weight": round(a.weight, 4),
                "capabilities": a.capabilities,
            })
        return web.json_response({"agents": agents, "count": len(agents)})

    async def handle_disconnect(self, request: web.Request) -> web.Response:
        """DELETE /api/gateway/disconnect — remove an agent."""
        api_key = self._extract_key(request)
        if not api_key:
            return web.json_response({"error": "missing api_key"}, status=401)

        agent = self._verify_key(api_key)
        if not agent:
            return web.json_response({"error": "invalid api_key"}, status=401)

        agent_id = agent.agent_id
        self.agents.pop(agent_id, None)
        self._ws_clients.pop(agent_id, None)
        self._latest_signals.pop(agent_id, None)

        # Remove from Strategist
        if self.strategist and agent_id in self.strategist.weights:
            del self.strategist.weights[agent_id]
            total = sum(self.strategist.weights.values()) or 1.0
            for k in self.strategist.weights:
                self.strategist.weights[k] /= total

        log.info("AGENT DISCONNECTED: %s", agent_id)
        return web.json_response({"disconnected": agent_id})

    # ── WebSocket handler ───────────────────────────────────────────

    async def handle_ws_agent(self, request: web.Request) -> web.WebSocketResponse:
        """WS /ws/agent — bidirectional real-time stream.

        Connect with ?api_key=... query param.

        Inbound messages (from agent):
          {"type": "signal", ...}    — same as POST /api/gateway/signal body
          {"type": "heartbeat"}      — keep-alive

        Outbound messages (to agent):
          {"type": "market", "prices": {...}}
          {"type": "execution", ...}
          {"type": "signal_ack", ...}
        """
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)

        api_key = request.query.get("api_key", "")
        agent = self._verify_key(api_key)
        if not agent:
            await ws.send_json({"type": "error", "message": "invalid api_key"})
            await ws.close()
            return ws

        self._ws_clients[agent.agent_id] = ws
        log.info("WS agent connected: %s (%d total)", agent.agent_id, len(self._ws_clients))

        # Send initial state
        await ws.send_json({
            "type": "welcome",
            "agent_id": agent.agent_id,
            "prices": self._prices,
            "portfolio": self.portfolio.summary() if self.portfolio else None,
        })

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_ws_message(agent, data, ws)
                    except json.JSONDecodeError:
                        await ws.send_json({"type": "error", "message": "invalid JSON"})
                elif msg.type == web.WSMsgType.ERROR:
                    break
        finally:
            self._ws_clients.pop(agent.agent_id, None)
            log.info("WS agent disconnected: %s", agent.agent_id)

        return ws

    async def _handle_ws_message(self, agent: ConnectedAgent, data: dict,
                                  ws: web.WebSocketResponse):
        msg_type = data.get("type", "signal")

        if msg_type == "heartbeat":
            await ws.send_json({"type": "heartbeat_ack", "ts": time.time()})
            return

        if msg_type == "signal":
            if not self._check_rate_limit(agent):
                await ws.send_json({
                    "type": "error",
                    "message": "rate limit exceeded",
                    "retry_after": 60,
                })
                return

            if agent.protocol == "openclaw":
                parsed = self._parse_openclaw_signal(data)
            elif agent.protocol == "hermes":
                parsed = self._parse_hermes_signal(data)
            else:
                parsed = self._parse_raw_signal(data)

            signal = Signal(
                agent_id=agent.agent_id,
                asset=parsed["asset"],
                direction=parsed["direction"],
                strength=parsed["strength"],
                confidence=parsed["confidence"],
                rationale=f"[{agent.name}] {parsed['rationale']}"[:500],
            )
            await self.bus.publish(f"signal.{agent.agent_id}", signal)

            agent.last_signal_at = time.time()
            agent.signal_count += 1

            await ws.send_json({
                "type": "signal_ack",
                "accepted": True,
                "signal": {
                    "asset": signal.asset,
                    "direction": signal.direction,
                    "strength": round(signal.strength, 4),
                },
            })

    # ── Route registration ──────────────────────────────────────────

    def register_routes(self, app: web.Application):
        """Add gateway routes to an aiohttp Application."""
        app.router.add_post("/api/gateway/connect", self.handle_connect)
        app.router.add_post("/api/gateway/signal", self.handle_signal)
        app.router.add_get("/api/gateway/market", self.handle_market)
        app.router.add_get("/api/gateway/portfolio", self.handle_portfolio)
        app.router.add_get("/api/gateway/agents", self.handle_agents_list)
        app.router.add_delete("/api/gateway/disconnect", self.handle_disconnect)
        app.router.add_get("/ws/agent", self.handle_ws_agent)

    def summary(self) -> dict:
        return {
            "connected_agents": len(self.agents),
            "agents": {
                a.agent_id: {
                    "name": a.name,
                    "protocol": a.protocol,
                    "signals": a.signal_count,
                    "weight": round(a.weight, 4),
                    "last_signal": a.last_signal_at,
                }
                for a in self.agents.values()
            },
        }
