"""Agent Gateway — HTTP + WebSocket bridge for external AI agents.

Supports:
  - OpenClaw agents (tool-calling protocol)
  - Hermes agents (message-passing protocol)
  - IronClaw agents (action-chain protocol)
  - Raw HTTP/WebSocket agents (any framework)

External agents connect, authenticate, receive market data, and publish
trading signals that flow into the Strategist via the internal Bus.

Registration follows SwarmProtocol conventions: agents declare a type
from a categorised registry, list capabilities, and receive an ASN
(Agent Swarm Number) on successful registration.

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
import asyncio, hashlib, hmac, json, logging, random, secrets, string, time
from dataclasses import dataclass, field
from typing import Any
from aiohttp import web
from .core import Bus, Signal, MarketSnapshot, ExecutionReport

log = logging.getLogger("swarm.gateway")

# Default weight assigned to newly connected external agents
EXTERNAL_AGENT_DEFAULT_WEIGHT = 0.06

# ── Agent Type Registry ─────────────────────────────────────────
# Mirrors SwarmProtocol's categorised type system, scoped to trading.
AGENT_TYPE_REGISTRY: dict[str, dict] = {
    # ── Signal Generators ──
    "signal-momentum": {
        "label": "Momentum Signal",
        "category": "signal-generators",
        "description": "Generates buy/sell signals from momentum indicators",
        "tags": ["momentum", "trend", "signal"],
    },
    "signal-mean-reversion": {
        "label": "Mean Reversion Signal",
        "category": "signal-generators",
        "description": "Detects mean-reversion opportunities",
        "tags": ["mean-reversion", "statistical", "signal"],
    },
    "signal-breakout": {
        "label": "Breakout Detector",
        "category": "signal-generators",
        "description": "Identifies price breakouts from consolidation",
        "tags": ["breakout", "volatility", "signal"],
    },
    "signal-sentiment": {
        "label": "Sentiment Analyzer",
        "category": "signal-generators",
        "description": "NLP-driven sentiment from news/social feeds",
        "tags": ["sentiment", "nlp", "news"],
    },
    "signal-arbitrage": {
        "label": "Arbitrage Scanner",
        "category": "signal-generators",
        "description": "Cross-exchange or cross-pair arbitrage signals",
        "tags": ["arbitrage", "spread", "multi-exchange"],
    },
    "signal-custom": {
        "label": "Custom Signal Agent",
        "category": "signal-generators",
        "description": "Custom signal logic not covered by other types",
        "tags": ["custom", "signal"],
    },
    # ── Data & Research ──
    "data-market": {
        "label": "Market Data Feed",
        "category": "data-research",
        "description": "Provides real-time or historical market data",
        "tags": ["data", "market", "feed"],
    },
    "data-onchain": {
        "label": "On-Chain Analyst",
        "category": "data-research",
        "description": "Tracks on-chain metrics (whale moves, TVL, flows)",
        "tags": ["onchain", "blockchain", "data"],
    },
    "data-orderbook": {
        "label": "Order Book Analyst",
        "category": "data-research",
        "description": "Analyses order book depth and imbalances",
        "tags": ["orderbook", "depth", "microstructure"],
    },
    "data-funding": {
        "label": "Funding Rate Tracker",
        "category": "data-research",
        "description": "Monitors perpetual futures funding rates",
        "tags": ["funding", "futures", "derivatives"],
    },
    # ── AI / ML Models ──
    "ml-classifier": {
        "label": "ML Classifier",
        "category": "ai-ml",
        "description": "ML model that classifies market regimes or signals",
        "tags": ["ml", "classification", "model"],
    },
    "ml-forecaster": {
        "label": "Price Forecaster",
        "category": "ai-ml",
        "description": "Time-series forecasting model for price prediction",
        "tags": ["ml", "forecast", "prediction"],
    },
    "ml-reinforcement": {
        "label": "RL Trading Agent",
        "category": "ai-ml",
        "description": "Reinforcement learning agent for trade decisions",
        "tags": ["rl", "reinforcement", "adaptive"],
    },
    "ml-llm": {
        "label": "LLM Analyst",
        "category": "ai-ml",
        "description": "Large language model performing market analysis",
        "tags": ["llm", "language-model", "analysis"],
    },
    # ── Risk & Compliance ──
    "risk-position": {
        "label": "Position Risk Monitor",
        "category": "risk-compliance",
        "description": "Monitors position sizing and exposure limits",
        "tags": ["risk", "position", "exposure"],
    },
    "risk-portfolio": {
        "label": "Portfolio Risk Analyzer",
        "category": "risk-compliance",
        "description": "VaR, stress testing, and portfolio-level risk",
        "tags": ["risk", "portfolio", "var"],
    },
    "risk-compliance": {
        "label": "Compliance Checker",
        "category": "risk-compliance",
        "description": "Regulatory and wash-trade compliance monitoring",
        "tags": ["compliance", "regulatory", "audit"],
    },
    # ── Execution ──
    "exec-router": {
        "label": "Smart Order Router",
        "category": "execution",
        "description": "Routes orders across venues for best execution",
        "tags": ["execution", "routing", "order"],
    },
    "exec-twap": {
        "label": "TWAP/VWAP Executor",
        "category": "execution",
        "description": "Time/volume-weighted execution algorithms",
        "tags": ["execution", "twap", "vwap", "algo"],
    },
    # ── Meta / Orchestration ──
    "meta-coordinator": {
        "label": "Swarm Coordinator",
        "category": "meta-orchestration",
        "description": "Orchestrates other agents, manages consensus",
        "tags": ["coordinator", "orchestration", "meta"],
    },
    "meta-aggregator": {
        "label": "Signal Aggregator",
        "category": "meta-orchestration",
        "description": "Aggregates signals from multiple sub-agents",
        "tags": ["aggregator", "ensemble", "meta"],
    },
    "meta-brain": {
        "label": "Trading Brain (LLM)",
        "category": "meta-orchestration",
        "description": "LLM-based trading brain that synthesizes signals and makes autonomous trade decisions",
        "tags": ["brain", "llm", "decision-making", "orchestration"],
    },
}

AGENT_CATEGORIES = {
    "signal-generators":  {"label": "Signal Generators",  "icon": "sensors",          "color": "#22c55e"},
    "data-research":      {"label": "Data & Research",     "icon": "query_stats",      "color": "#3b82f6"},
    "ai-ml":              {"label": "AI / ML Models",      "icon": "psychology",        "color": "#a855f7"},
    "risk-compliance":    {"label": "Risk & Compliance",   "icon": "shield",           "color": "#f59e0b"},
    "execution":          {"label": "Execution",           "icon": "bolt",             "color": "#06b6d4"},
    "meta-orchestration": {"label": "Meta / Orchestration","icon": "hub",              "color": "#ec4899"},
}

AGENT_CAPABILITIES = [
    "spot-trading", "futures-trading", "options-analysis",
    "multi-exchange", "real-time-streaming", "historical-backtest",
    "sentiment-analysis", "on-chain-data", "order-book-analysis",
    "risk-scoring", "portfolio-optimization", "execution-algo",
    "cross-chain", "defi-protocols", "nft-markets",
]

# ── ASN Generator ───────────────────────────────────────────────
_asn_counter = 0

def generate_asn() -> str:
    """Generate an Agent Swarm Number in SwarmProtocol format: ASN-SWT-YYYY-XXXX-XXXX-XX"""
    global _asn_counter
    _asn_counter += 1
    year = time.strftime("%Y")
    seg1 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    seg2 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    chk = ''.join(random.choices(string.digits, k=2))
    return f"ASN-SWT-{year}-{seg1}-{seg2}-{chk}"


@dataclass
class ConnectedAgent:
    """Tracks a connected external agent."""
    agent_id: str
    name: str
    protocol: str           # "openclaw" | "hermes" | "ironclaw" | "raw"
    api_key: str
    agent_type: str = "signal-custom"
    asn: str = ""
    status: str = "online"  # online | offline | busy | paused
    description: str = ""
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

        log.info("Agent Gateway initialized (key configured, length=%d)", len(self.master_key))

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
            except Exception as e:
                log.warning("Agent %s WebSocket send failed, disconnecting: %s", agent_id, e)
                dead.append(agent_id)
        for agent_id in dead:
            self._ws_clients.pop(agent_id, None)

    async def broadcast_brain_brief(self, system: str, brief: str, max_tokens: int = 1024):
        """Push a brain brief to all connected WS agents.

        Called by HermesBrain every interval so external agents can act as the brain.
        Agents respond with {"type": "decision", ...} in their native protocol.
        """
        if not self._ws_clients:
            return
        await self._broadcast_to_agents({
            "type": "brain_brief",
            "system": system,
            "brief": brief,
            "max_tokens": max_tokens,
            "ts": time.time(),
        })

    async def send_chat_to_agent(self, agent_id: str, message: str) -> bool:
        """Send a chat message to a specific connected agent.

        Lets the user talk to their agent through the dashboard.
        """
        ws = self._ws_clients.get(agent_id)
        if not ws:
            return False
        try:
            await ws.send_json({
                "type": "chat",
                "message": message,
                "ts": time.time(),
            })
            return True
        except Exception as e:
            log.warning("Chat to %s failed: %s", agent_id, e)
            return False

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
        return request.headers.get("X-API-Key") or None

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

    def _parse_ironclaw_signal(self, body: dict) -> dict:
        """Parse IronClaw action-chain format into normalized signal.

        IronClaw agents use an action-chain with typed actions:
        {
            "actions": [{
                "type": "trade_signal",
                "params": {
                    "ticker": "ETH",
                    "side": "long",           // long | short | close
                    "confidence": 0.85,       // 0-1
                    "urgency": "high",        // low | medium | high
                    "analysis": "..."
                }
            }],
            "agent_version": "...",
            "session_id": "..."
        }
        Also accepts flat format with "ticker"/"side" at top level.
        """
        actions = body.get("actions", [])
        if not actions:
            # Flat format fallback
            params = body
        else:
            action = actions[0]
            params = action.get("params", action.get("parameters", {}))

        side = str(params.get("side", params.get("direction", "close"))).lower()
        direction = {"long": "long", "short": "short", "close": "flat",
                     "buy": "long", "sell": "short", "flat": "flat"}.get(side, "flat")
        confidence = float(params.get("confidence", params.get("conviction", 0.5)))

        # Urgency scales conviction
        urgency_mult = {"low": 0.6, "medium": 0.8, "high": 1.0}.get(
            str(params.get("urgency", "medium")).lower(), 0.8)
        adj_confidence = confidence * urgency_mult

        return {
            "asset": str(params.get("ticker", params.get("asset", "ETH"))).upper().replace("USD", ""),
            "direction": direction,
            "strength": adj_confidence if direction == "long" else -adj_confidence if direction == "short" else 0.0,
            "confidence": confidence,
            "rationale": str(params.get("analysis", params.get("reasoning", ""))),
        }

    def _parse_raw_signal(self, body: dict) -> dict:
        """Parse a raw/generic signal format."""
        direction = str(body.get("direction", body.get("side", "flat"))).lower()
        direction = {"buy": "long", "sell": "short", "bullish": "long",
                     "bearish": "short"}.get(direction, direction)
        if direction not in ("long", "short", "flat"):
            direction = "flat"

        try:
            strength = float(body.get("strength", body.get("score", 0.0)))
            confidence = float(body.get("confidence", 0.5))
        except (ValueError, TypeError):
            strength, confidence = 0.0, 0.5

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
            "protocol": "hermes",          // openclaw | hermes | ironclaw | raw
            "agent_type": "signal-momentum",
            "capabilities": ["spot-trading", "real-time-streaming"],
            "description": "My momentum signal agent",
            "master_key": "..."            // required for first connect
        }

        Returns: { "agent_id": "...", "asn": "ASN-SWT-...", "api_key": "...", ... }
        """
        body = await request.json()
        master = body.get("master_key", "")
        if not hmac.compare_digest(master, self.master_key):
            return web.json_response({"error": "invalid master_key"}, status=401)

        import re
        raw_name = str(body.get("name", f"external_{len(self.agents)}")).strip()[:64]
        # Sanitize: alphanumeric, spaces, hyphens, underscores only
        name = re.sub(r'[^\w\s\-]', '', raw_name).strip()
        if not name:
            return web.json_response({"error": "name is required (alphanumeric only)"}, status=400)
        protocol = body.get("protocol", "raw")
        if protocol not in ("openclaw", "hermes", "ironclaw", "raw"):
            return web.json_response({"error": "protocol must be openclaw, hermes, ironclaw, or raw"}, status=400)

        agent_type = str(body.get("agent_type", "signal-custom"))[:64]
        if agent_type not in AGENT_TYPE_REGISTRY:
            agent_type = "signal-custom"

        # Sanitize agent_id — alphanumeric + underscores only
        import re
        safe_name = re.sub(r'[^a-z0-9_]', '_', name.lower())[:48]
        agent_id = f"ext_{safe_name}"

        # Hard limits on external agent registration
        MAX_EXTERNAL_AGENTS = 10
        MAX_AGENT_WEIGHT = 0.20   # No single external agent can dominate signals
        MAX_TOTAL_EXT_WEIGHT = 0.50  # All external agents combined capped at 50%

        ext_count = sum(1 for aid in self.agents if aid.startswith("ext_"))
        if ext_count >= MAX_EXTERNAL_AGENTS:
            return web.json_response(
                {"error": f"max external agents ({MAX_EXTERNAL_AGENTS}) reached"}, status=429)

        weight = min(MAX_AGENT_WEIGHT, float(body.get("weight", EXTERNAL_AGENT_DEFAULT_WEIGHT)))
        current_ext_weight = sum(a.weight for aid, a in self.agents.items() if aid.startswith("ext_"))
        if current_ext_weight + weight > MAX_TOTAL_EXT_WEIGHT:
            weight = max(0.0, MAX_TOTAL_EXT_WEIGHT - current_ext_weight)
            if weight < 0.01:
                return web.json_response(
                    {"error": f"total external agent weight cap ({MAX_TOTAL_EXT_WEIGHT}) reached"}, status=429)

        api_key = secrets.token_hex(32)
        asn = generate_asn()

        agent = ConnectedAgent(
            agent_id=agent_id,
            name=name,
            protocol=protocol,
            api_key=api_key,
            agent_type=agent_type,
            asn=asn,
            description=str(body.get("description", ""))[:200],
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

        log.info("AGENT REGISTERED: %s asn=%s type=%s protocol=%s weight=%.3f",
                 agent_id, asn, agent_type, protocol, weight)

        type_info = AGENT_TYPE_REGISTRY.get(agent_type, {})
        return web.json_response({
            "agent_id": agent_id,
            "asn": asn,
            "api_key": api_key,
            "agent_type": agent_type,
            "agent_type_label": type_info.get("label", agent_type),
            "category": type_info.get("category", ""),
            "weight": weight,
            "protocol": protocol,
            "status": "online",
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

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        # Support nested "data"/"payload" keys
        signal_body = body.get("data", body.get("payload", body))

        # Parse based on protocol
        try:
            if agent.protocol == "openclaw":
                parsed = self._parse_openclaw_signal(signal_body)
            elif agent.protocol == "hermes":
                parsed = self._parse_hermes_signal(signal_body)
            elif agent.protocol == "ironclaw":
                parsed = self._parse_ironclaw_signal(signal_body)
            else:
                parsed = self._parse_raw_signal(signal_body)
        except Exception as e:
            log.warning("Signal parse error from %s: %s", agent.agent_id, e)
            return web.json_response({"error": "signal parse error: invalid format"}, status=400)

        # Build internal Signal — clamp values to valid ranges
        raw_strength = parsed["strength"]
        raw_confidence = parsed["confidence"]
        clamped_strength = max(-1.0, min(1.0, float(raw_strength)))
        clamped_confidence = max(0.0, min(1.0, float(raw_confidence)))
        if clamped_strength != raw_strength or clamped_confidence != raw_confidence:
            log.warning("Gateway signal clamped: %s str=%.4f->%.4f conf=%.4f->%.4f",
                        agent.agent_id, raw_strength, clamped_strength,
                        raw_confidence, clamped_confidence)
        signal = Signal(
            agent_id=agent.agent_id,
            asset=parsed["asset"],
            direction=parsed["direction"],
            strength=clamped_strength,
            confidence=clamped_confidence,
            rationale=f"[{agent.name}] {parsed['rationale'][:150].replace(chr(10), ' ').replace(chr(13), ' ')}",
        )

        # Publish to bus — must match the topic strategist subscribes to at registration
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
        """GET /api/gateway/agents — list connected agents (requires auth)."""
        # Require either a valid agent API key or the master key
        api_key = self._extract_key(request)
        if api_key:
            if not self._verify_key(api_key) and api_key != self.master_key:
                return web.json_response({"error": "invalid api_key"}, status=401)
        elif self.master_key:
            return web.json_response({"error": "authentication required"}, status=401)

        agents = []
        for a in self.agents.values():
            type_info = AGENT_TYPE_REGISTRY.get(a.agent_type, {})
            agents.append({
                "agent_id": a.agent_id,
                "asn": a.asn,
                "name": a.name,
                "agent_type": a.agent_type,
                "agent_type_label": type_info.get("label", a.agent_type),
                "category": type_info.get("category", ""),
                "protocol": a.protocol,
                "status": a.status,
                "description": a.description,
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
        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=1024 * 1024)
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

        if msg_type == "brain_brief":
            # Brain agent pushing a market brief — relay to all brain subscribers
            # This is handled by the GatewayProvider on the brain side;
            # the agent just reads the brief and sends back a "decision" message.
            # Nothing to do here — the WS is bidirectional.
            return

        if msg_type == "decision":
            # Brain agent responding with a trading decision.
            # The GatewayProvider reads this directly from the WS stream.
            # If it arrives here, it means no brain is listening — treat as signal.
            log.info("Decision from %s (no brain listener, converting to signal)",
                     agent.agent_id)
            msg_type = "signal"  # fall through to signal handling

        if msg_type == "chat_response":
            # Agent responding to a user chat message — relay to dashboard via bus
            await self.bus.publish("agent.chat", {
                "agent_id": agent.agent_id,
                "agent_name": agent.name,
                "message": str(data.get("message", ""))[:2000],
                "ts": time.time(),
            })
            return

        if msg_type == "signal":
            if not self._check_rate_limit(agent):
                await ws.send_json({
                    "type": "error",
                    "message": "rate limit exceeded",
                    "retry_after": 60,
                })
                return

            # Extract signal payload — support nested "data"/"payload" keys
            signal_body = data.get("data", data.get("payload", data))

            try:
                if agent.protocol == "openclaw":
                    parsed = self._parse_openclaw_signal(signal_body)
                elif agent.protocol == "hermes":
                    parsed = self._parse_hermes_signal(signal_body)
                elif agent.protocol == "ironclaw":
                    parsed = self._parse_ironclaw_signal(signal_body)
                else:
                    parsed = self._parse_raw_signal(signal_body)
            except Exception as e:
                log.warning("Signal parse error from %s: %s", agent.agent_id, e)
                await ws.send_json({
                    "type": "error",
                    "message": f"signal parse error: {e}",
                })
                return

            signal = Signal(
                agent_id=agent.agent_id,
                asset=parsed["asset"],
                direction=parsed["direction"],
                strength=parsed["strength"],
                confidence=parsed["confidence"],
                rationale=f"[{agent.name}] {parsed['rationale'][:150].replace(chr(10), ' ').replace(chr(13), ' ')}",
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

    async def handle_registry(self, request: web.Request) -> web.Response:
        """GET /api/gateway/registry — agent type registry + capabilities (requires auth)."""
        api_key = self._extract_key(request)
        if api_key:
            if not self._verify_key(api_key) and api_key != self.master_key:
                return web.json_response({"error": "invalid api_key"}, status=401)
        elif self.master_key:
            return web.json_response({"error": "authentication required"}, status=401)

        return web.json_response({
            "types": AGENT_TYPE_REGISTRY,
            "categories": AGENT_CATEGORIES,
            "capabilities": AGENT_CAPABILITIES,
        })

    async def handle_chat(self, request: web.Request) -> web.Response:
        """POST /api/gateway/chat — send a message to a connected agent.

        Body: {"agent_id": "ext_my_agent", "message": "What do you think about ETH?"}
        The agent receives {"type": "chat", "message": "..."} on its WS.
        """
        body = await request.json()
        agent_id = body.get("agent_id", "")
        message = body.get("message", "")
        if not agent_id or not message:
            return web.json_response({"error": "agent_id and message required"}, status=400)

        sent = await self.send_chat_to_agent(agent_id, message)
        if not sent:
            return web.json_response({"error": f"agent {agent_id} not connected via WS"}, status=404)

        return web.json_response({"sent": True, "agent_id": agent_id})

    def register_routes(self, app: web.Application):
        """Add gateway routes to an aiohttp Application."""
        app.router.add_post("/api/gateway/connect", self.handle_connect)
        app.router.add_post("/api/gateway/signal", self.handle_signal)
        app.router.add_post("/api/gateway/chat", self.handle_chat)
        app.router.add_get("/api/gateway/market", self.handle_market)
        app.router.add_get("/api/gateway/portfolio", self.handle_portfolio)
        app.router.add_get("/api/gateway/agents", self.handle_agents_list)
        app.router.add_get("/api/gateway/registry", self.handle_registry)
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
