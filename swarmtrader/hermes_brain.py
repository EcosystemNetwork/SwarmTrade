"""HermesBrain — LLM orchestrator that controls all swarm agents.

Subscribes to every signal and market topic on the bus, compiles a rolling
market brief, sends it to any LLM backend, and emits TradeIntent objects
based on the LLM's reasoning.

The 80+ swarm agents become the eyes and ears; your LLM is the brain.

Supported backends (auto-detected from env vars):
  1. Ollama        — OLLAMA_URL  (local: gemma3, hermes, llama3, etc.)
  2. OpenAI-compat — LLM_API_URL + LLM_API_KEY  (vLLM, LM Studio, Together, Kimi, etc.)
  3. OpenAI        — OPENAI_API_KEY  (gpt-4o, gpt-4-turbo, etc.)
  4. Anthropic     — ANTHROPIC_API_KEY  (claude-opus-4-6, claude-sonnet-4-6, etc.)

Env vars:
  LLM_PROVIDER      — Force provider: ollama | openai | anthropic | openai-compat
  LLM_API_URL       — Base URL for OpenAI-compatible APIs
  LLM_API_KEY       — API key for OpenAI-compat / Kimi / Together / etc.
  LLM_MODEL         — Model name (auto-detected if empty)
  OLLAMA_URL        — Ollama API base (default: http://localhost:11434)
  OLLAMA_MODEL      — Alias for LLM_MODEL when using Ollama
  OPENAI_API_KEY    — OpenAI API key
  ANTHROPIC_API_KEY — Anthropic API key
  HERMES_INTERVAL   — Seconds between LLM calls (default: 15)
  HERMES_MAX_TOKENS — Max response tokens (default: 1024)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field

import aiohttp

from .core import Bus, Signal, TradeIntent, MarketSnapshot, OrderSpec, PortfolioTracker

log = logging.getLogger("swarm.hermes")


# ── Signal buffer ─────────────────────────────────────────────────────────

@dataclass
class SignalSnapshot:
    agent_id: str
    asset: str
    direction: str
    strength: float
    confidence: float
    rationale: str
    ts: float


@dataclass
class MarketState:
    """Rolling view of everything the swarm knows right now."""
    prices: dict[str, float] = field(default_factory=dict)
    signals: dict[str, SignalSnapshot] = field(default_factory=dict)
    regime: str = "unknown"
    fear_greed: str = ""
    last_intent_ts: float = 0.0
    recent_executions: list[str] = field(default_factory=list)


SIGNAL_TOPICS = [
    "signal.momentum", "signal.mean_rev", "signal.vol", "signal.regime",
    "signal.spread", "signal.rsi", "signal.macd", "signal.bollinger",
    "signal.vwap", "signal.ichimoku", "signal.mtf", "signal.correlation",
    "signal.news", "signal.rss_news", "signal.whale", "signal.confluence",
    "signal.liquidation", "signal.atr_stop", "signal.position",
    "signal.prism", "signal.prism_volume", "signal.prism_breakout",
    "signal.orderbook", "signal.funding", "signal.onchain",
    "signal.birdeye", "signal.hyperliquid", "signal.arbitrage",
    "signal.arb_live", "signal.exchange_flow", "signal.stablecoin",
    "signal.macro", "signal.options", "signal.unlock", "signal.github",
    "signal.polymarket", "signal.smart_money", "signal.social",
    "signal.social_alpha", "signal.social_consensus",
    "signal.ml", "signal.hedge", "signal.rebalance",
    "signal.yield", "signal.alpha_swarm", "signal.fear_greed",
    "signal.open_interest", "signal.liquidation_levels",
]


# ═══════════════════════════════════════════════════════════════════════════
# LLM Provider Backends
# ═══════════════════════════════════════════════════════════════════════════

class LLMProvider:
    """Base class for LLM backends."""
    name: str = "base"

    async def setup(self, session: aiohttp.ClientSession) -> bool:
        """Verify connection and resolve model. Return True if ready."""
        raise NotImplementedError

    async def query(self, session: aiohttp.ClientSession,
                    system: str, prompt: str, max_tokens: int) -> str:
        """Send prompt and return raw text response."""
        raise NotImplementedError

    def describe(self) -> str:
        """Human-readable description for logs."""
        return self.name


class OllamaProvider(LLMProvider):
    """Local Ollama server — supports any model the user has pulled."""
    name = "ollama"

    PREFERRED = [
        "gemma3", "nous-hermes2", "hermes3",
        "llama3", "llama3.1", "mistral", "mixtral",
        "qwen2", "qwen2.5", "phi3", "deepseek-r1",
    ]

    def __init__(self, url: str, model: str = ""):
        self.url = url.rstrip("/")
        self.model = model

    async def setup(self, session: aiohttp.ClientSession) -> bool:
        try:
            async with session.get(
                f"{self.url}/api/tags",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.error("Ollama HTTP %d", resp.status)
                    return False
                data = await resp.json()
        except aiohttp.ClientConnectorError:
            log.error("Cannot connect to Ollama at %s — is it running?", self.url)
            return False
        except Exception as e:
            log.error("Ollama handshake failed: %s", e)
            return False

        models = [m.get("name", "") for m in data.get("models", [])]
        if not models:
            log.error("Ollama running but no models pulled. Run: ollama pull gemma3")
            return False

        log.info("Ollama models: %s", ", ".join(models))

        # Use configured model if it exists
        if self.model:
            matched = [m for m in models if m == self.model or m.startswith(self.model + ":")]
            if matched:
                self.model = matched[0]
                return True
            log.warning("Model '%s' not found, auto-detecting...", self.model)

        # Auto-detect best model
        lower_map = {m.lower(): m for m in models}
        for pref in self.PREFERRED:
            if pref.lower() in lower_map:
                self.model = lower_map[pref.lower()]
                return True
            for k, v in lower_map.items():
                if k.startswith(pref.lower()):
                    self.model = v
                    return True

        self.model = models[0]
        return True

    async def query(self, session: aiohttp.ClientSession,
                    system: str, prompt: str, max_tokens: int) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {"num_predict": max_tokens, "temperature": 0.3, "top_p": 0.9},
        }
        async with session.post(
            f"{self.url}/api/generate", json=payload,
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Ollama HTTP {resp.status}: {(await resp.text())[:200]}")
            data = await resp.json()
            tokens = data.get("eval_count", 0)
            dur = data.get("total_duration", 0) / 1e9
            log.info("Ollama: %d tokens in %.1fs", tokens, dur)
            return data.get("response", "")

    def describe(self) -> str:
        return f"Ollama ({self.url}) → {self.model}"


class OpenAICompatProvider(LLMProvider):
    """Any OpenAI-compatible API: OpenAI, vLLM, LM Studio, Together, Kimi, etc."""
    name = "openai-compat"

    def __init__(self, url: str, api_key: str, model: str = ""):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def setup(self, session: aiohttp.ClientSession) -> bool:
        # Try to list models to find a valid one
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.model:
            log.info("OpenAI-compat: using configured model '%s'", self.model)
            return True

        try:
            async with session.get(
                f"{self.url}/v1/models", headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    models = [m.get("id", "") for m in data.get("data", [])]
                    if models:
                        self.model = models[0]
                        log.info("OpenAI-compat models: %s (using %s)",
                                 ", ".join(models[:5]), self.model)
                        return True
        except Exception:
            pass

        log.error("OpenAI-compat: set LLM_MODEL — could not auto-detect models at %s", self.url)
        return False

    async def query(self, session: aiohttp.ClientSession,
                    system: str, prompt: str, max_tokens: int) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        async with session.post(
            f"{self.url}/v1/chat/completions", json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"OpenAI-compat HTTP {resp.status}: {(await resp.text())[:200]}")
            data = await resp.json()
            return data["choices"][0]["message"]["content"]

    def describe(self) -> str:
        # Mask the URL for Kimi/Together/etc — just show host
        from urllib.parse import urlparse
        host = urlparse(self.url).hostname or self.url
        return f"OpenAI-compat ({host}) → {self.model}"


class OpenAIProvider(OpenAICompatProvider):
    """Native OpenAI API."""
    name = "openai"

    def __init__(self, api_key: str, model: str = ""):
        super().__init__("https://api.openai.com", api_key, model or "gpt-4o")

    def describe(self) -> str:
        return f"OpenAI → {self.model}"


class AnthropicProvider(LLMProvider):
    """Anthropic Claude API (Messages API)."""
    name = "anthropic"

    def __init__(self, api_key: str, model: str = ""):
        self.api_key = api_key
        self.model = model or "claude-sonnet-4-6-20250514"

    async def setup(self, session: aiohttp.ClientSession) -> bool:
        log.info("Anthropic: using model '%s'", self.model)
        return True

    async def query(self, session: aiohttp.ClientSession,
                    system: str, prompt: str, max_tokens: int) -> str:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        async with session.post(
            "https://api.anthropic.com/v1/messages", json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Anthropic HTTP {resp.status}: {(await resp.text())[:200]}")
            data = await resp.json()
            return data["content"][0]["text"]

    def describe(self) -> str:
        return f"Anthropic → {self.model}"


class GatewayProvider(LLMProvider):
    """External agent connected via the Agent Gateway WebSocket.

    Works like a Discord/Telegram daemon — persistent WS connection.
    The brain pushes market briefs to the agent, the agent pushes back
    decisions in its native protocol (OpenClaw/Hermes/IronClaw/raw).

    The agent connects to ws://host:port/ws/agent?api_key=...
    and receives {"type": "brain_brief", "brief": "...", "system": "..."}.
    It responds with {"type": "decision", "data": {...}} in any supported format.
    """
    name = "gateway"

    def __init__(self, gateway_url: str, api_key: str, protocol: str = "raw"):
        self.gateway_url = gateway_url.rstrip("/")
        self.api_key = api_key
        self.protocol = protocol  # openclaw | hermes | ironclaw | raw
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._agent_name: str = ""

    async def setup(self, session: aiohttp.ClientSession) -> bool:
        """Connect to the gateway WS as a brain agent."""
        ws_url = self.gateway_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws/agent?api_key={self.api_key}"

        try:
            self._ws = await session.ws_connect(ws_url, heartbeat=30.0, timeout=10.0)
            welcome = await asyncio.wait_for(self._ws.receive_json(), timeout=10.0)
            if welcome.get("type") == "error":
                log.error("Gateway rejected connection: %s", welcome.get("message"))
                return False
            self._agent_name = welcome.get("agent_id", "external")
            log.info("Gateway WS connected as '%s'", self._agent_name)
            return True
        except Exception as e:
            log.error("Gateway WS connection failed: %s", e)
            return False

    async def query(self, session: aiohttp.ClientSession,
                    system: str, prompt: str, max_tokens: int) -> str:
        """Push brief to agent via WS, wait for decision response."""
        if not self._ws or self._ws.closed:
            raise RuntimeError("Gateway WS not connected")

        # Send the brief to the agent
        await self._ws.send_json({
            "type": "brain_brief",
            "system": system,
            "brief": prompt,
            "max_tokens": max_tokens,
            "ts": time.time(),
        })

        # Wait for decision (with timeout)
        try:
            msg = await asyncio.wait_for(self._ws.receive(), timeout=90.0)
        except asyncio.TimeoutError:
            raise RuntimeError("Agent did not respond within 90s")

        if msg.type == aiohttp.WSMsgType.TEXT:
            return msg.data
        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            raise RuntimeError("Gateway WS closed unexpectedly")
        return ""

    def describe(self) -> str:
        return f"Gateway agent ({self._agent_name}, {self.protocol})"


def _detect_provider() -> LLMProvider:
    """Auto-detect which LLM backend to use from env vars."""
    forced = os.getenv("LLM_PROVIDER", "").lower().strip()
    model = os.getenv("LLM_MODEL", "") or os.getenv("OLLAMA_MODEL", "")

    # Gateway: external agent connected via WS
    if forced == "gateway" or (not forced and os.getenv("BRAIN_GATEWAY_URL")):
        url = os.getenv("BRAIN_GATEWAY_URL", "")
        key = os.getenv("BRAIN_GATEWAY_KEY", "")
        protocol = os.getenv("BRAIN_PROTOCOL", "raw")
        if url and key:
            return GatewayProvider(gateway_url=url, api_key=key, protocol=protocol)

    # Explicit provider override
    if forced == "anthropic" or (not forced and os.getenv("ANTHROPIC_API_KEY")):
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if key:
            return AnthropicProvider(api_key=key, model=model)

    if forced == "openai" or (not forced and os.getenv("OPENAI_API_KEY") and not os.getenv("LLM_API_URL")):
        key = os.getenv("OPENAI_API_KEY", "")
        if key:
            return OpenAIProvider(api_key=key, model=model)

    if forced == "openai-compat" or (not forced and os.getenv("LLM_API_URL")):
        url = os.getenv("LLM_API_URL", "")
        key = os.getenv("LLM_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
        if url:
            return OpenAICompatProvider(url=url, api_key=key, model=model)

    # Default: Ollama (local)
    url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    return OllamaProvider(url=url, model=model)


# ═══════════════════════════════════════════════════════════════════════════
# HermesBrain — The brain that controls the swarm
# ═══════════════════════════════════════════════════════════════════════════

class HermesBrain:
    """LLM-powered strategist that replaces the rule-based Strategist.

    Flow:
      1. All 80+ agents publish signals to the bus
      2. HermesBrain collects them into a MarketState
      3. Every HERMES_INTERVAL seconds, it compiles a market brief
      4. Sends brief to any LLM backend (Ollama/OpenAI/Claude/Kimi/etc.)
      5. Parses the JSON response into TradeIntent(s)
      6. Publishes intent.new → risk agents still gate execution
    """

    SYSTEM_PROMPT = """You are an elite AI trading strategist controlling a swarm of {n_agents} specialized trading agents. Your agents monitor technical indicators, on-chain data, social sentiment, whale movements, order books, funding rates, and more.

You receive a market brief every {interval}s with all active signals from your agent swarm. Based on this intelligence, you decide whether to:
1. GO LONG on an asset (buy)
2. GO SHORT / EXIT a position (sell)
3. HOLD — do nothing and wait for better setups

RULES:
- You manage a portfolio of ${capital:.0f} USD
- Max single trade: ${max_size:.0f}
- You MUST respond with valid JSON only — no markdown, no explanation outside JSON
- Only trade when you have HIGH CONVICTION from multiple agents agreeing
- Consider the market regime (trending/reverting/volatile) when sizing
- Be conservative — missing a trade costs nothing, a bad trade costs money

RESPONSE FORMAT (JSON array — empty array [] means HOLD):
[
  {{
    "action": "buy" | "sell",
    "asset": "ETH" | "BTC" | "SOL" | etc,
    "size_usd": 100.0,
    "confidence": 0.0-1.0,
    "reasoning": "brief explanation",
    "order_type": "market" | "limit",
    "urgency": "high" | "medium" | "low"
  }}
]"""

    def __init__(
        self,
        bus: Bus,
        portfolio: PortfolioTracker,
        assets: list[str],
        base_size: float = 500.0,
        max_size: float = 2000.0,
        capital: float = 10000.0,
        provider: LLMProvider | None = None,
        gateway=None,
        interval: float | None = None,
        max_tokens: int | None = None,
        cooldown_s: float = 5.0,
    ):
        self.bus = bus
        self.portfolio = portfolio
        self.assets = assets
        self.base_size = base_size
        self.max_size = max_size
        self.gateway = gateway  # AgentGateway — push briefs to connected agents
        self.capital = capital
        self.cooldown_s = cooldown_s

        self.provider = provider or _detect_provider()
        self.interval = interval or float(os.getenv("HERMES_INTERVAL", "15"))
        self.max_tokens = max_tokens or int(os.getenv("HERMES_MAX_TOKENS", "1024"))

        self.state = MarketState()
        self._running = False
        self._ready = False
        self._session: aiohttp.ClientSession | None = None
        self._last_call_ts: float = 0.0
        self._call_count: int = 0
        self._error_count: int = 0

        # Subscribe to everything
        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("exec.report", self._on_exec_report)
        for topic in SIGNAL_TOPICS:
            bus.subscribe(topic, self._on_signal)

        log.info("HermesBrain initialized: provider=%s interval=%.0fs",
                 self.provider.name, self.interval)

    # ── Bus handlers ──────────────────────────────────────────────────────

    async def _on_snapshot(self, snap: MarketSnapshot):
        self.state.prices.update(snap.prices)

    async def _on_signal(self, sig: Signal):
        key = f"{sig.agent_id}:{sig.asset}"
        self.state.signals[key] = SignalSnapshot(
            agent_id=sig.agent_id, asset=sig.asset,
            direction=sig.direction, strength=sig.strength,
            confidence=sig.confidence, rationale=sig.rationale[:120],
            ts=sig.ts,
        )
        if sig.agent_id == "regime":
            try:
                self.state.regime = sig.rationale.split("regime=")[1].split()[0]
            except (IndexError, ValueError):
                pass
        elif sig.agent_id == "fear_greed":
            self.state.fear_greed = sig.rationale[:80]

    async def _on_exec_report(self, report):
        entry = f"{report.status}:{report.asset}@{report.fill_price or '?'}"
        self.state.recent_executions.append(entry)
        if len(self.state.recent_executions) > 10:
            self.state.recent_executions = self.state.recent_executions[-10:]

    # ── Market brief compiler ─────────────────────────────────────────────

    def _compile_brief(self) -> str:
        """Build a concise market brief from all collected signals."""
        now = time.time()
        lines = []

        # Prices
        if self.state.prices:
            price_str = ", ".join(f"{a}: ${p:,.2f}" for a, p in self.state.prices.items())
            lines.append(f"PRICES: {price_str}")

        # Regime
        lines.append(f"REGIME: {self.state.regime}")
        if self.state.fear_greed:
            lines.append(f"FEAR/GREED: {self.state.fear_greed}")

        # Portfolio
        if self.portfolio:
            summary = self.portfolio.summary()
            positions = summary.get("positions", {})
            if positions:
                pos_lines = []
                for asset, info in positions.items():
                    pos_lines.append(
                        f"  {asset}: qty={info['qty']} entry=${info['avg_entry']:.2f} "
                        f"unrealized={info['unrealized_pnl']:+.2f}"
                    )
                lines.append("OPEN POSITIONS:\n" + "\n".join(pos_lines))
            else:
                lines.append("OPEN POSITIONS: none")
            lines.append(f"TOTAL PnL: realized={summary['total_realized_pnl']:+.4f} "
                         f"fees={summary['total_fees']:.4f}")

        # Agent signals grouped by asset
        stale_cutoff = now - 120
        active_signals = [s for s in self.state.signals.values() if s.ts > stale_cutoff]

        if active_signals:
            by_asset: dict[str, list[SignalSnapshot]] = {}
            for s in active_signals:
                by_asset.setdefault(s.asset, []).append(s)

            for asset in sorted(by_asset.keys()):
                signals = sorted(by_asset[asset], key=lambda s: -s.confidence)
                lines.append(f"\n--- {asset} SIGNALS ({len(signals)} agents) ---")

                longs = [s for s in signals if s.direction == "long"]
                shorts = [s for s in signals if s.direction == "short"]
                flats = [s for s in signals if s.direction == "flat"]
                lines.append(
                    f"CONSENSUS: {len(longs)} LONG, {len(shorts)} SHORT, {len(flats)} FLAT"
                )

                for s in signals[:12]:
                    age = int(now - s.ts)
                    lines.append(
                        f"  [{s.agent_id}] {s.direction} "
                        f"str={s.strength:+.2f} conf={s.confidence:.2f} "
                        f"({age}s ago) — {s.rationale}"
                    )
        else:
            lines.append("\nNO ACTIVE SIGNALS (waiting for agents to report)")

        if self.state.recent_executions:
            lines.append(f"\nRECENT TRADES: {', '.join(self.state.recent_executions[-5:])}")

        return "\n".join(lines)

    # ── LLM interaction ───────────────────────────────────────────────────

    async def _query_llm(self, brief: str) -> list[dict]:
        """Send market brief to LLM and parse JSON response."""
        if not self._session:
            self._session = aiohttp.ClientSession()

        n_signals = len([s for s in self.state.signals.values()
                         if time.time() - s.ts < 120])

        system = self.SYSTEM_PROMPT.format(
            n_agents=n_signals,
            interval=self.interval,
            capital=self.capital,
            max_size=self.max_size,
        )

        prompt = (
            f"MARKET BRIEF (timestamp: {time.strftime('%H:%M:%S')}):\n\n"
            f"{brief}\n\n"
            f"Analyze all signals and decide: trade or hold? Respond with JSON array only."
        )

        try:
            raw = await self.provider.query(self._session, system, prompt, self.max_tokens)
            self._call_count += 1
            log.info("LLM response: %s", raw[:200])
            return self._parse_response(raw)

        except aiohttp.ClientConnectorError:
            log.error("Cannot connect to LLM provider — is it running?")
            self._error_count += 1
            return []
        except asyncio.TimeoutError:
            log.error("LLM request timed out (90s)")
            self._error_count += 1
            return []
        except Exception as e:
            log.error("LLM error: %s", e)
            self._error_count += 1
            return []

    def _parse_response(self, raw: str) -> list[dict]:
        """Parse LLM/agent response in ANY supported format.

        Understands:
          1. JSON array  — [{"action":"buy", ...}]  (direct LLM)
          2. OpenClaw     — {"tool_calls": [{"name":"submit_signal", "arguments":{...}}]}
          3. Hermes       — {"structured_output": {"signal":"bullish", ...}}
          4. IronClaw     — {"actions": [{"type":"trade_signal", "params":{...}}]}
          5. Raw/flat     — {"action":"buy", "asset":"ETH", ...}
        """
        text = raw.strip()

        # Strip markdown fences
        if text.startswith("```"):
            first_nl = text.index("\n") if "\n" in text else 3
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        if not text or text == "[]":
            return []

        # Try parsing as JSON object first (agent protocol formats)
        parsed = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            pass

        if parsed is not None:
            # Already a list → our native format
            if isinstance(parsed, list):
                return parsed

            if isinstance(parsed, dict):
                # Unwrap nested "data"/"payload" wrappers
                inner = parsed.get("data", parsed.get("payload", parsed))
                if isinstance(inner, dict):
                    parsed = inner

                # ── OpenClaw: tool_calls ──
                if "tool_calls" in parsed:
                    return [self._openclaw_to_decision(tc)
                            for tc in parsed["tool_calls"]]

                # ── Hermes: structured_output / output ──
                if any(k in parsed for k in ("structured_output", "tool_output", "output", "signal_data")):
                    return [self._hermes_to_decision(parsed)]

                # ── IronClaw: actions array ──
                if "actions" in parsed and isinstance(parsed["actions"], list):
                    return [self._ironclaw_to_decision(a)
                            for a in parsed["actions"]]

                # ── Raw flat: single decision object ──
                if "action" in parsed or "direction" in parsed or "side" in parsed:
                    return [self._raw_to_decision(parsed)]

        # Fallback: find JSON array in text
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end > start:
            try:
                decisions = json.loads(text[start:end + 1])
                if isinstance(decisions, list):
                    return decisions
            except json.JSONDecodeError:
                pass

        log.warning("BRAIN: could not parse response: %s", text[:150])
        return []

    # ── Protocol-specific parsers (mirrors gateway.py) ────────────────

    @staticmethod
    def _openclaw_to_decision(tool_call: dict) -> dict:
        """OpenClaw tool_call → normalized decision."""
        args = tool_call.get("arguments", tool_call.get("args", {}))
        action_raw = args.get("action", args.get("side", "hold")).lower()
        action = {"buy": "buy", "long": "buy", "sell": "sell",
                  "short": "sell", "hold": "hold"}.get(action_raw, "hold")
        return {
            "action": action,
            "asset": args.get("asset", args.get("symbol", "ETH")).upper().replace("USD", ""),
            "size_usd": float(args.get("size_usd", args.get("amount", 0))),
            "confidence": float(args.get("conviction", args.get("confidence", 0.5))),
            "reasoning": args.get("reasoning", args.get("rationale", "")),
            "order_type": args.get("order_type", "market"),
            "urgency": args.get("urgency", "medium"),
        }

    @staticmethod
    def _hermes_to_decision(body: dict) -> dict:
        """Hermes structured_output → normalized decision."""
        structured = (body.get("structured_output")
                      or body.get("tool_output")
                      or body.get("output")
                      or body.get("signal_data")
                      or {})
        signal_str = str(structured.get("signal", "neutral")).lower()
        action = {"bullish": "buy", "long": "buy", "buy": "buy",
                  "bearish": "sell", "short": "sell", "sell": "sell",
                  }.get(signal_str, "hold")
        return {
            "action": action,
            "asset": str(structured.get("asset", "ETH")).upper().replace("USD", ""),
            "size_usd": float(structured.get("size_usd", structured.get("amount", 0))),
            "confidence": float(structured.get("confidence", 0.5)),
            "reasoning": str(body.get("content", structured.get("reasoning", "")))[:200],
            "order_type": structured.get("order_type", "market"),
            "urgency": structured.get("urgency", "medium"),
        }

    @staticmethod
    def _ironclaw_to_decision(action_obj: dict) -> dict:
        """IronClaw action → normalized decision."""
        params = action_obj.get("params", action_obj.get("parameters", action_obj))
        side = str(params.get("side", params.get("direction", "close"))).lower()
        action = {"long": "buy", "buy": "buy",
                  "short": "sell", "sell": "sell",
                  }.get(side, "hold")
        confidence = float(params.get("confidence", params.get("conviction", 0.5)))
        urgency = str(params.get("urgency", "medium")).lower()
        urgency_mult = {"low": 0.6, "medium": 0.8, "high": 1.0}.get(urgency, 0.8)
        return {
            "action": action,
            "asset": str(params.get("ticker", params.get("asset", "ETH"))).upper().replace("USD", ""),
            "size_usd": float(params.get("size_usd", params.get("amount", 0))),
            "confidence": confidence * urgency_mult,
            "reasoning": str(params.get("analysis", params.get("reasoning", ""))),
            "order_type": params.get("order_type", "market"),
            "urgency": urgency,
        }

    @staticmethod
    def _raw_to_decision(body: dict) -> dict:
        """Raw/flat format → normalized decision."""
        action_raw = body.get("action", body.get("side", body.get("direction", "hold"))).lower()
        action = {"buy": "buy", "long": "buy", "bullish": "buy",
                  "sell": "sell", "short": "sell", "bearish": "sell",
                  }.get(action_raw, "hold")
        return {
            "action": action,
            "asset": str(body.get("asset", body.get("symbol", body.get("ticker", "ETH")))).upper().replace("USD", ""),
            "size_usd": float(body.get("size_usd", body.get("amount", 0))),
            "confidence": float(body.get("confidence", body.get("conviction", 0.5))),
            "reasoning": str(body.get("reasoning", body.get("rationale", body.get("content", "")))),
            "order_type": body.get("order_type", "market"),
            "urgency": body.get("urgency", "medium"),
        }

    # ── Decision → TradeIntent ────────────────────────────────────────────

    def _decision_to_intent(self, decision: dict) -> TradeIntent | None:
        """Convert an LLM decision dict into a TradeIntent."""
        action = decision.get("action", "").lower()
        asset = decision.get("asset", "").upper()
        size_usd = float(decision.get("size_usd", 0))
        confidence = float(decision.get("confidence", 0))
        reasoning = decision.get("reasoning", "LLM decision")
        order_type = decision.get("order_type", "market").lower()

        if action not in ("buy", "sell"):
            log.warning("LLM: invalid action '%s'", action)
            return None
        if not asset or asset not in self.assets:
            log.warning("LLM: unknown asset '%s' (valid: %s)", asset, self.assets)
            return None
        if size_usd <= 0 or size_usd > self.max_size:
            log.warning("LLM: size $%.0f out of range (0, %d]", size_usd, self.max_size)
            size_usd = min(max(10.0, size_usd), self.max_size)
        if confidence < 0.3:
            log.info("LLM: skipping low-confidence decision (%.2f)", confidence)
            return None

        # Spot constraint: can't sell what we don't hold
        if action == "sell" and self.portfolio:
            held = self.portfolio.get(asset).quantity
            price = self.portfolio.last_prices.get(asset, 0)
            if held < 1e-9:
                log.info("LLM: can't sell %s — no position", asset)
                return None
            if price > 0:
                size_usd = min(size_usd, held * price * 0.95)

        going_long = action == "buy"

        spec = OrderSpec()
        if order_type == "limit":
            spec.order_type = "limit"
            spec.post_only = True
            spec.time_in_force = "gtc"
        else:
            spec.order_type = "market"

        hermes_signal = Signal(
            agent_id="hermes",
            asset=asset,
            direction="long" if going_long else "short",
            strength=confidence if going_long else -confidence,
            confidence=confidence,
            rationale=f"[{self.provider.name}] {reasoning[:200]}",
        )

        intent = TradeIntent.new(
            asset_in="USDC" if going_long else asset,
            asset_out=asset if going_long else "USDC",
            amount_in=size_usd,
            min_out=0.0,
            ttl=time.time() + 15.0,
            supporting=[hermes_signal],
            order_spec=spec,
        )

        log.info("BRAIN INTENT %s: %s %s $%.0f conf=%.2f — %s",
                 intent.id, action.upper(), asset, size_usd, confidence, reasoning[:80])

        return intent

    # ── Main loop ─────────────────────────────────────────────────────────

    async def run(self):
        """Main loop: compile brief → query LLM → emit intents."""
        self._running = True

        if not self._session:
            self._session = aiohttp.ClientSession()

        # Handshake with provider — verify connection and resolve model
        if not await self.provider.setup(self._session):
            log.error("HermesBrain DISABLED — LLM provider not available. "
                      "Rule-based Strategist handles decisions.")
            return

        self._ready = True
        log.info("HermesBrain ONLINE: %s | interval=%.0fs",
                 self.provider.describe(), self.interval)

        # Wait for initial signals
        await asyncio.sleep(min(self.interval, 10.0))

        while self._running:
            try:
                now = time.time()

                if now - self._last_call_ts < self.cooldown_s:
                    await asyncio.sleep(1.0)
                    continue

                brief = self._compile_brief()
                n_signals = len([s for s in self.state.signals.values()
                                 if now - s.ts < 120])

                if n_signals < 2:
                    log.debug("BRAIN: only %d active signals, waiting...", n_signals)
                    await asyncio.sleep(5.0)
                    continue

                # Also push brief to any agents connected via gateway
                if self.gateway:
                    system = self.SYSTEM_PROMPT.format(
                        n_agents=n_signals, interval=self.interval,
                        capital=self.capital, max_size=self.max_size,
                    )
                    await self.gateway.broadcast_brain_brief(system, brief, self.max_tokens)

                log.info("BRAIN: querying %s with %d signals, regime=%s",
                         self.provider.name, n_signals, self.state.regime)
                decisions = await self._query_llm(brief)
                self._last_call_ts = time.time()

                if not decisions:
                    log.info("BRAIN: HOLD (no trades)")
                else:
                    for dec in decisions:
                        intent = self._decision_to_intent(dec)
                        if intent:
                            await self.bus.publish("intent.new", intent)

                # Publish status heartbeat
                await self.bus.publish("signal.hermes", Signal(
                    agent_id="hermes",
                    asset=self.assets[0] if self.assets else "ETH",
                    direction="flat",
                    strength=0.0,
                    confidence=1.0,
                    rationale=(
                        f"provider={self.provider.name} "
                        f"calls={self._call_count} errors={self._error_count} "
                        f"signals={n_signals} decisions={len(decisions)}"
                    ),
                ))

            except Exception as e:
                log.exception("HermesBrain loop error: %s", e)

            await asyncio.sleep(self.interval)

    async def stop(self):
        self._running = False
        if self._session:
            await self._session.close()
            self._session = None

    def status(self) -> dict:
        now = time.time()
        n_active = len([s for s in self.state.signals.values() if now - s.ts < 120])
        return {
            "provider": self.provider.name,
            "provider_detail": self.provider.describe(),
            "interval_s": self.interval,
            "running": self._running,
            "ready": self._ready,
            "call_count": self._call_count,
            "error_count": self._error_count,
            "active_signals": n_active,
            "total_tracked": len(self.state.signals),
            "regime": self.state.regime,
            "prices": dict(self.state.prices),
        }
