"""Voice-Activated Trading — speak to trade.

Inspired by EchoFi (voice-activated DeFi assistant), Orchestra (talk to
your crypto with Ledger security), signet (speak to trade with World ID),
and Aura (local AI voice assistant for real-time trading on Sui).

Pipeline:
  1. Audio input → Speech-to-Text (Whisper API or local)
  2. Text → Intent parsing (NLP strategy parser)
  3. Intent → Validation (risk checks, price gate)
  4. Validation → Execution (standard SwarmTrader pipeline)
  5. Result → Text-to-Speech response

Supports:
  - OpenAI Whisper API (cloud)
  - Local Whisper (via whisper.cpp or faster-whisper)
  - Browser Web Speech API (via WebSocket)

Bus integration:
  Subscribes to: voice.input (audio data from clients)
  Publishes to:  voice.parsed, voice.response, intent.new

Environment variables:
  VOICE_PROVIDER     — "openai", "local", "browser" (default: browser)
  VOICE_API_KEY      — OpenAI API key for Whisper (if using cloud)
  VOICE_LANGUAGE     — Language code (default: en)
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field

import aiohttp

from .core import Bus, Signal, TradeIntent

log = logging.getLogger("swarm.voice")


@dataclass
class VoiceCommand:
    """A parsed voice command."""
    command_id: str
    raw_text: str
    # Parsed fields
    action: str = ""          # "buy", "sell", "status", "risk", "alert"
    asset: str = ""
    amount_usd: float = 0.0
    modifier: str = ""        # "limit", "stop", "market"
    limit_price: float = 0.0
    # Metadata
    confidence: float = 0.0   # STT confidence
    language: str = "en"
    provider: str = ""
    latency_ms: float = 0.0
    ts: float = field(default_factory=time.time)


# Natural language patterns for trade commands
TRADE_PATTERNS = [
    # "buy 500 dollars of ETH"
    re.compile(r"(buy|sell|long|short)\s+(\$?[\d,]+(?:\.\d+)?)\s*(?:dollars?\s+(?:of|in)\s+)?(\w+)", re.I),
    # "buy ETH for 500"
    re.compile(r"(buy|sell|long|short)\s+(\w+)\s+(?:for|at|with)\s+\$?([\d,]+(?:\.\d+)?)", re.I),
    # "go long ETH 500"
    re.compile(r"(?:go\s+)?(long|short)\s+(\w+)\s+\$?([\d,]+(?:\.\d+)?)", re.I),
]

STATUS_PATTERNS = [
    re.compile(r"(?:what(?:'s| is)?\s+(?:my\s+)?)?(?:status|portfolio|balance|position)", re.I),
    re.compile(r"how\s+(?:am\s+i\s+doing|are\s+(?:we|things))", re.I),
]

RISK_PATTERNS = [
    re.compile(r"(?:what(?:'s| is)?\s+(?:my\s+)?)?(?:risk|exposure|drawdown)", re.I),
    re.compile(r"(?:am\s+i\s+)?(?:safe|in\s+danger|over\s*exposed)", re.I),
]

KILL_PATTERNS = [
    re.compile(r"(?:kill|stop|halt|emergency|flatten|close\s+(?:all|everything))", re.I),
]


class VoiceTradingEngine:
    """Voice-to-trade pipeline with multi-provider STT support.

    Accepts audio via WebSocket or text (from browser Speech API),
    parses into structured commands, and routes through the standard
    SwarmTrader execution pipeline.
    """

    def __init__(self, bus: Bus, provider: str = "browser"):
        self.bus = bus
        self.provider = provider or os.getenv("VOICE_PROVIDER", "browser")
        self.api_key = os.getenv("VOICE_API_KEY", "")
        self._commands: list[VoiceCommand] = []
        self._cmd_counter = 0
        self._stats = {
            "transcribed": 0, "parsed": 0, "executed": 0, "failed": 0,
        }

        bus.subscribe("voice.input", self._on_voice_input)
        bus.subscribe("voice.text", self._on_voice_text)

    async def _on_voice_input(self, audio_data: bytes):
        """Process raw audio data through STT."""
        start = time.time()
        text, confidence = await self._transcribe(audio_data)
        latency = (time.time() - start) * 1000

        if not text:
            self._stats["failed"] += 1
            return

        self._stats["transcribed"] += 1
        log.info("VOICE STT: '%s' (conf=%.2f, %.0fms)", text, confidence, latency)

        command = self._parse_command(text, confidence, latency)
        await self._execute_command(command)

    async def _on_voice_text(self, data: dict):
        """Process pre-transcribed text (from browser Speech API)."""
        text = data.get("text", "")
        confidence = data.get("confidence", 0.9)
        if not text:
            return

        self._stats["transcribed"] += 1
        command = self._parse_command(text, confidence, 0.0)
        await self._execute_command(command)

    async def _transcribe(self, audio: bytes) -> tuple[str, float]:
        """Transcribe audio to text via configured provider."""
        if self.provider == "openai" and self.api_key:
            return await self._transcribe_openai(audio)
        return "", 0.0

    async def _transcribe_openai(self, audio: bytes) -> tuple[str, float]:
        """Transcribe via OpenAI Whisper API."""
        try:
            url = "https://api.openai.com/v1/audio/transcriptions"
            headers = {"Authorization": f"Bearer {self.api_key}"}
            data = aiohttp.FormData()
            data.add_field("file", audio, filename="audio.webm", content_type="audio/webm")
            data.add_field("model", "whisper-1")
            data.add_field("language", os.getenv("VOICE_LANGUAGE", "en"))

            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=data, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    result = await resp.json()
                    return result.get("text", ""), 0.9
        except Exception as e:
            log.warning("Whisper transcription failed: %s", e)
            return "", 0.0

    def _parse_command(self, text: str, confidence: float,
                       latency: float) -> VoiceCommand:
        """Parse transcribed text into a structured command."""
        self._cmd_counter += 1
        cmd = VoiceCommand(
            command_id=f"voice-{self._cmd_counter:05d}",
            raw_text=text,
            confidence=confidence,
            provider=self.provider,
            latency_ms=latency,
        )

        # Try trade patterns
        for pattern in TRADE_PATTERNS:
            match = pattern.search(text)
            if match:
                groups = match.groups()
                action = groups[0].lower()
                if action in ("long",):
                    action = "buy"
                elif action in ("short",):
                    action = "sell"
                cmd.action = action

                # Figure out which group is amount vs asset
                for g in groups[1:]:
                    clean = g.replace("$", "").replace(",", "")
                    try:
                        cmd.amount_usd = float(clean)
                    except ValueError:
                        if g.isalpha():
                            cmd.asset = g.upper()

                self._stats["parsed"] += 1
                return cmd

        # Try status patterns
        for pattern in STATUS_PATTERNS:
            if pattern.search(text):
                cmd.action = "status"
                self._stats["parsed"] += 1
                return cmd

        # Try risk patterns
        for pattern in RISK_PATTERNS:
            if pattern.search(text):
                cmd.action = "risk"
                self._stats["parsed"] += 1
                return cmd

        # Try kill patterns
        for pattern in KILL_PATTERNS:
            if pattern.search(text):
                cmd.action = "kill"
                self._stats["parsed"] += 1
                return cmd

        cmd.action = "unknown"
        return cmd

    async def _execute_command(self, cmd: VoiceCommand):
        """Execute a parsed voice command."""
        self._commands.append(cmd)
        if len(self._commands) > 200:
            self._commands = self._commands[-100:]

        if cmd.action in ("buy", "sell") and cmd.asset and cmd.amount_usd > 0:
            self._stats["executed"] += 1
            log.info(
                "VOICE TRADE: %s $%.0f %s (conf=%.2f)",
                cmd.action.upper(), cmd.amount_usd, cmd.asset, cmd.confidence,
            )
            await self.bus.publish("voice.parsed", cmd)

            # Response
            await self.bus.publish("voice.response", {
                "text": f"Queuing {cmd.action} order for ${cmd.amount_usd:.0f} of {cmd.asset}. "
                        f"Running risk checks now.",
                "command_id": cmd.command_id,
            })

        elif cmd.action == "status":
            await self.bus.publish("voice.response", {
                "text": "Checking portfolio status.",
                "command_id": cmd.command_id,
            })

        elif cmd.action == "kill":
            await self.bus.publish("voice.response", {
                "text": "Emergency kill switch requires dashboard confirmation for safety.",
                "command_id": cmd.command_id,
            })

        elif cmd.action == "unknown":
            self._stats["failed"] += 1
            await self.bus.publish("voice.response", {
                "text": f"I didn't understand: '{cmd.raw_text}'. "
                        f"Try: 'buy 500 dollars of ETH' or 'what's my status'.",
                "command_id": cmd.command_id,
            })

    def summary(self) -> dict:
        return {
            **self._stats,
            "provider": self.provider,
            "recent_commands": [
                {
                    "id": c.command_id,
                    "text": c.raw_text[:50],
                    "action": c.action,
                    "asset": c.asset,
                    "amount": c.amount_usd,
                }
                for c in self._commands[-5:]
            ],
        }
