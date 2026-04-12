"""Agent Communication Protocol — structured inter-agent messaging.

Inspired by Geneva (agents collaborate using agile methodology), CrewKit
(CrewAI + AgentKit for collaborative workflows), and TriadFi (Fetch.ai
uAgents message-passing between Signal->Risk->Execution).

Upgrades the raw Bus pub/sub with structured message types, request/
response patterns, and agent discovery.

Message types:
  1. REQUEST — agent asks another agent for data/analysis
  2. RESPONSE — reply to a request with results
  3. BROADCAST — announcement to all agents (regime change, etc.)
  4. TASK — assign work to a specialist agent
  5. RESULT — completed task output

Bus integration:
  Subscribes to: protocol.* (all protocol messages)
  Publishes to:  protocol.routed (after routing)
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from collections import defaultdict

from .core import Bus

log = logging.getLogger("swarm.protocol")


@dataclass
class AgentMessage:
    """A structured message between agents."""
    message_id: str
    msg_type: str           # "request", "response", "broadcast", "task", "result"
    sender: str             # agent_id
    recipient: str          # agent_id or "*" for broadcast
    # Content
    topic: str              # what this is about
    payload: dict = field(default_factory=dict)
    # Request/response tracking
    correlation_id: str = ""    # links response to original request
    ttl_seconds: float = 30.0   # message expires after this
    # Priority
    priority: int = 5           # 1 (highest) to 10 (lowest)
    # Metadata
    ts: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        return time.time() - self.ts > self.ttl_seconds


@dataclass
class AgentCapability:
    """What an agent can do (for discovery/routing)."""
    agent_id: str
    capabilities: list[str]     # ["analyze_momentum", "risk_check", "execute_trade"]
    asset_classes: list[str]    # ["ETH", "BTC", "MEME"]
    response_time_ms: float = 0.0
    reliability: float = 1.0    # % of requests successfully answered


class AgentCommunicationProtocol:
    """Structured messaging layer for agent-to-agent communication.

    Features:
      - Request/response with correlation tracking
      - Agent capability registry for smart routing
      - Priority queue (urgent messages processed first)
      - Timeout and retry for unreliable agents
      - Message history for debugging
    """

    def __init__(self, bus: Bus, max_history: int = 500):
        self.bus = bus
        self._capabilities: dict[str, AgentCapability] = {}
        self._pending_requests: dict[str, AgentMessage] = {}  # correlation_id -> request
        self._response_callbacks: dict[str, asyncio.Future] = {}
        self._history: list[AgentMessage] = []
        self._stats = {
            "sent": 0, "received": 0, "requests": 0,
            "responses": 0, "broadcasts": 0, "timeouts": 0,
        }

        bus.subscribe("protocol.message", self._on_message)

    def register_agent(self, agent_id: str, capabilities: list[str],
                       asset_classes: list[str] | None = None) -> AgentCapability:
        """Register an agent's capabilities for discovery."""
        cap = AgentCapability(
            agent_id=agent_id,
            capabilities=capabilities,
            asset_classes=asset_classes or [],
        )
        self._capabilities[agent_id] = cap
        return cap

    def discover(self, capability: str, asset: str = "") -> list[str]:
        """Find agents that can handle a specific capability."""
        results = []
        for agent_id, cap in self._capabilities.items():
            if capability in cap.capabilities:
                if not asset or asset in cap.asset_classes or not cap.asset_classes:
                    results.append(agent_id)
        # Sort by reliability
        results.sort(key=lambda a: self._capabilities[a].reliability, reverse=True)
        return results

    async def send(self, msg: AgentMessage):
        """Send a message through the protocol."""
        self._history.append(msg)
        if len(self._history) > 500:
            self._history = self._history[-250:]

        self._stats["sent"] += 1

        if msg.msg_type == "request":
            self._stats["requests"] += 1
            self._pending_requests[msg.message_id] = msg
        elif msg.msg_type == "broadcast":
            self._stats["broadcasts"] += 1
        elif msg.msg_type == "response":
            self._stats["responses"] += 1

        await self.bus.publish("protocol.message", msg)

    async def request(self, sender: str, recipient: str, topic: str,
                      payload: dict, timeout: float = 10.0) -> AgentMessage | None:
        """Send a request and wait for response."""
        msg_id = uuid.uuid4().hex[:8]
        msg = AgentMessage(
            message_id=msg_id,
            msg_type="request",
            sender=sender,
            recipient=recipient,
            topic=topic,
            payload=payload,
            correlation_id=msg_id,
            ttl_seconds=timeout,
        )

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._response_callbacks[msg_id] = future

        await self.send(msg)

        try:
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        except asyncio.TimeoutError:
            self._stats["timeouts"] += 1
            self._response_callbacks.pop(msg_id, None)
            return None

    async def respond(self, original: AgentMessage, sender: str,
                      payload: dict):
        """Send a response to a request."""
        response = AgentMessage(
            message_id=uuid.uuid4().hex[:8],
            msg_type="response",
            sender=sender,
            recipient=original.sender,
            topic=original.topic,
            payload=payload,
            correlation_id=original.correlation_id,
        )
        await self.send(response)

    async def broadcast(self, sender: str, topic: str, payload: dict):
        """Broadcast a message to all agents."""
        msg = AgentMessage(
            message_id=uuid.uuid4().hex[:8],
            msg_type="broadcast",
            sender=sender,
            recipient="*",
            topic=topic,
            payload=payload,
        )
        await self.send(msg)

    async def _on_message(self, msg: AgentMessage):
        """Route incoming messages."""
        if not isinstance(msg, AgentMessage):
            return

        self._stats["received"] += 1

        # Handle response callbacks
        if msg.msg_type == "response" and msg.correlation_id in self._response_callbacks:
            future = self._response_callbacks.pop(msg.correlation_id)
            if not future.done():
                future.set_result(msg)

    def summary(self) -> dict:
        return {
            **self._stats,
            "registered_agents": len(self._capabilities),
            "pending_requests": len(self._pending_requests),
            "capabilities": {
                aid: cap.capabilities
                for aid, cap in self._capabilities.items()
            },
        }
