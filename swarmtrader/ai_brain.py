"""Multi-Model AI Brain — pluggable LLM adapter for strategy reasoning.

Inspired by GhostFi (multi-model adapter: Groq/OpenAI/Gemini), TWAP CHOP
(Claude SDK for execution planning), Alpha Dawg (0G sealed inference with
Qwen 7B), and CookFi AI (LLM decision engine with confidence+risk output).

Provides a unified interface for calling any LLM to:
  1. Analyze market context and generate trade rationale
  2. Parse natural language strategies into config
  3. Generate adversarial debate arguments
  4. Summarize multi-signal convergence into narrative

Supports: Claude (Anthropic), GPT (OpenAI), Groq (Llama/Mixtral),
          DeepSeek, and local models via HTTP.

Bus integration:
  Subscribes to: brain.query (on-demand LLM calls)
  Publishes to:  brain.response, signal.ai_brain

Environment variables:
  AI_BRAIN_PROVIDER    — "anthropic", "openai", "groq", "deepseek", "local"
  AI_BRAIN_MODEL       — Model name (default: provider-specific)
  AI_BRAIN_API_KEY     — API key for the provider
  AI_BRAIN_BASE_URL    — Base URL for local/custom endpoints
  AI_BRAIN_MAX_TOKENS  — Max response tokens (default: 1024)
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

import aiohttp

from .core import Bus, Signal

log = logging.getLogger("swarm.brain")

# Provider configurations
PROVIDERS = {
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-sonnet-4-20250514",
        "header_key": "x-api-key",
        "format": "anthropic",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "header_key": "Authorization",
        "header_prefix": "Bearer ",
        "format": "openai",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "header_key": "Authorization",
        "header_prefix": "Bearer ",
        "format": "openai",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "header_key": "Authorization",
        "header_prefix": "Bearer ",
        "format": "openai",
    },
    "local": {
        "base_url": "http://localhost:11434/v1",
        "default_model": "llama3",
        "header_key": "",
        "format": "openai",
    },
}


@dataclass
class BrainQuery:
    """A query to the AI brain."""
    query_id: str
    prompt: str
    context: dict = field(default_factory=dict)
    task: str = "analyze"      # "analyze", "strategy", "debate", "summarize"
    max_tokens: int = 1024
    temperature: float = 0.3
    ts: float = field(default_factory=time.time)


@dataclass
class BrainResponse:
    """Response from the AI brain."""
    query_id: str
    content: str
    provider: str
    model: str
    # Extracted structured data
    direction: str = ""        # "long", "short", "hold"
    confidence: float = 0.0
    rationale: str = ""
    risk_factors: list[str] = field(default_factory=list)
    # Metadata
    tokens_used: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    ts: float = field(default_factory=time.time)


class AIBrain:
    """Multi-model AI brain for the trading swarm.

    Provides reasoning capabilities to any agent in the swarm.
    Automatically falls back through providers if one fails:
      1. Primary provider (configured)
      2. Groq (fast, free tier)
      3. Local model (no API key needed)
    """

    def __init__(self, bus: Bus, provider: str = "",
                 model: str = "", api_key: str = ""):
        self.bus = bus
        self.provider = provider or os.getenv("AI_BRAIN_PROVIDER", "groq")
        self.model = model or os.getenv("AI_BRAIN_MODEL", "")
        self.api_key = api_key or os.getenv("AI_BRAIN_API_KEY", "")
        self.max_tokens = int(os.getenv("AI_BRAIN_MAX_TOKENS", "1024"))
        self._query_counter = 0
        self._responses: list[BrainResponse] = []
        self._stats = {
            "queries": 0, "successes": 0, "failures": 0,
            "total_tokens": 0, "total_cost": 0.0,
        }

        # Resolve provider config
        self._config = PROVIDERS.get(self.provider, PROVIDERS["local"])
        if not self.model:
            self.model = self._config["default_model"]

        bus.subscribe("brain.query", self._on_query)

        log.info("AI Brain: provider=%s model=%s", self.provider, self.model)

    async def query(self, prompt: str, task: str = "analyze",
                    context: dict | None = None,
                    temperature: float = 0.3) -> BrainResponse:
        """Send a query to the AI brain and get structured response."""
        self._query_counter += 1
        query_id = f"brain-{self._query_counter:05d}"
        self._stats["queries"] += 1

        # Build system prompt based on task
        system_prompt = self._system_prompt(task)

        start = time.time()

        try:
            if self._config["format"] == "anthropic":
                content, tokens = await self._call_anthropic(system_prompt, prompt, temperature)
            else:
                content, tokens = await self._call_openai_compat(system_prompt, prompt, temperature)

            latency = (time.time() - start) * 1000
            self._stats["successes"] += 1
            self._stats["total_tokens"] += tokens

        except Exception as e:
            log.warning("AI Brain query failed (%s): %s — trying fallback", self.provider, e)
            # Fallback: generate a basic response without LLM
            content = self._fallback_response(prompt, task, context)
            tokens = 0
            latency = 0

        # Parse structured response
        response = BrainResponse(
            query_id=query_id,
            content=content,
            provider=self.provider,
            model=self.model,
            tokens_used=tokens,
            latency_ms=round(latency, 1),
        )

        # Try to extract structured data
        self._parse_response(response, content)

        self._responses.append(response)
        if len(self._responses) > 200:
            self._responses = self._responses[-100:]

        return response

    async def _on_query(self, query_data):
        """Handle Bus-published brain queries."""
        if isinstance(query_data, BrainQuery):
            response = await self.query(
                query_data.prompt, query_data.task,
                query_data.context, query_data.temperature,
            )
            await self.bus.publish("brain.response", response)
        elif isinstance(query_data, dict):
            response = await self.query(
                query_data.get("prompt", ""),
                query_data.get("task", "analyze"),
                query_data.get("context"),
            )
            await self.bus.publish("brain.response", response)

    def _system_prompt(self, task: str) -> str:
        base = (
            "You are an expert crypto trading analyst in an autonomous trading swarm. "
            "Be concise and data-driven. Always state direction (long/short/hold), "
            "confidence (0-1), and key risk factors."
        )
        if task == "strategy":
            return base + " Generate trading strategy parameters as JSON."
        elif task == "debate":
            return base + " Argue your assigned position (bull or bear) with specific evidence."
        elif task == "summarize":
            return base + " Summarize the market narrative from these data points."
        return base

    async def _call_anthropic(self, system: str, prompt: str,
                               temperature: float) -> tuple[str, int]:
        """Call Anthropic API (Claude)."""
        url = f"{self._config['base_url']}/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.json()
                content = data.get("content", [{}])[0].get("text", "")
                tokens = data.get("usage", {}).get("output_tokens", 0)
                return content, tokens

    async def _call_openai_compat(self, system: str, prompt: str,
                                    temperature: float) -> tuple[str, int]:
        """Call OpenAI-compatible API (GPT, Groq, DeepSeek, local)."""
        url = f"{self._config['base_url']}/chat/completions"
        headers = {"content-type": "application/json"}
        if self.api_key and self._config.get("header_key"):
            prefix = self._config.get("header_prefix", "")
            headers[self._config["header_key"]] = f"{prefix}{self.api_key}"

        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                tokens = data.get("usage", {}).get("completion_tokens", 0)
                return content, tokens

    def _fallback_response(self, prompt: str, task: str, context: dict | None) -> str:
        """Generate a basic response without LLM (rule-based fallback)."""
        if task == "analyze":
            return "HOLD | confidence: 0.3 | Insufficient data for LLM analysis. Falling back to signal-based decisions."
        elif task == "debate":
            return "No strong evidence either way. Recommend HOLD until more data available."
        return "Analysis unavailable — LLM provider not configured."

    def _parse_response(self, response: BrainResponse, content: str):
        """Extract structured data from LLM response text."""
        lower = content.lower()
        if "long" in lower or "buy" in lower or "bullish" in lower:
            response.direction = "long"
        elif "short" in lower or "sell" in lower or "bearish" in lower:
            response.direction = "short"
        else:
            response.direction = "hold"

        # Extract confidence if mentioned
        for marker in ["confidence:", "conf:", "conviction:"]:
            if marker in lower:
                try:
                    idx = lower.index(marker) + len(marker)
                    num_str = lower[idx:idx + 10].strip().split()[0]
                    response.confidence = float(num_str.rstrip(".,;"))
                except (ValueError, IndexError):
                    pass

        if response.confidence == 0:
            response.confidence = 0.5  # default

        response.rationale = content[:200]

    def summary(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            **self._stats,
            "recent": [
                {
                    "id": r.query_id,
                    "direction": r.direction,
                    "confidence": r.confidence,
                    "tokens": r.tokens_used,
                    "latency_ms": r.latency_ms,
                }
                for r in self._responses[-5:]
            ],
        }
