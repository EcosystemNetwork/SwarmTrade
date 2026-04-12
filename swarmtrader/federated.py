"""Federated Learning — agents share model improvements without leaking data.

Inspired by Colony (agents collaborate without sharing proprietary data),
HiveMind (P2P network sharing intents of sovereign AI agents based on
local memory), and the broader ML privacy pattern.

Agents improve their signal quality by sharing model gradients (not raw
data or positions) with the swarm. This allows collective learning
while preserving each agent's proprietary strategy.

Protocol:
  1. Each agent trains locally on its own signal history
  2. Agent computes gradient update (delta weights)
  3. Gradient is clipped and noise-added (differential privacy)
  4. Gradient shared with swarm via Bus
  5. Coordinator aggregates gradients (FedAvg)
  6. Aggregated update distributed back to all agents

Bus integration:
  Subscribes to: federated.gradient (individual agent updates)
  Publishes to:  federated.global_update (aggregated model)
"""
from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field

from .core import Bus

log = logging.getLogger("swarm.federated")


@dataclass
class GradientUpdate:
    """A gradient update from a single agent."""
    agent_id: str
    round_number: int
    # Gradient: weight name -> delta value
    gradients: dict[str, float]
    # Metadata
    samples_used: int = 0      # how many data points this was trained on
    local_loss: float = 0.0    # agent's local loss before update
    ts: float = field(default_factory=time.time)


@dataclass
class GlobalModel:
    """The federated global model state."""
    round_number: int = 0
    weights: dict[str, float] = field(default_factory=dict)
    contributors: int = 0
    avg_loss: float = 0.0
    ts: float = field(default_factory=time.time)


class FederatedCoordinator:
    """Coordinates federated learning across swarm agents.

    Uses FedAvg (Federated Averaging) for aggregation:
      global_weight = sum(n_k * w_k) / sum(n_k)
    where n_k is the number of samples agent k trained on.

    Privacy protection:
      - Gradient clipping: max L2 norm per update
      - Gaussian noise: added to gradients before sharing
      - Minimum contributors: won't publish update with < 3 agents
    """

    def __init__(self, bus: Bus, min_contributors: int = 3,
                 clip_norm: float = 1.0, noise_sigma: float = 0.01,
                 round_interval_s: float = 300.0):
        self.bus = bus
        self.min_contributors = min_contributors
        self.clip_norm = clip_norm
        self.noise_sigma = noise_sigma
        self.round_interval_s = round_interval_s
        self._global_model = GlobalModel()
        self._pending_gradients: list[GradientUpdate] = []
        self._round = 0
        self._history: list[GlobalModel] = []
        self._stats = {
            "rounds": 0, "gradients_received": 0,
            "updates_published": 0,
        }

        bus.subscribe("federated.gradient", self._on_gradient)

    async def _on_gradient(self, update: GradientUpdate | dict):
        """Receive a gradient update from an agent."""
        if isinstance(update, dict):
            update = GradientUpdate(
                agent_id=update.get("agent_id", ""),
                round_number=update.get("round_number", self._round),
                gradients=update.get("gradients", {}),
                samples_used=update.get("samples_used", 1),
                local_loss=update.get("local_loss", 0),
            )

        # Apply differential privacy: clip + noise
        clipped = self._clip_gradient(update.gradients)
        noised = self._add_noise(clipped)
        update.gradients = noised

        self._pending_gradients.append(update)
        self._stats["gradients_received"] += 1

        # Check if we have enough for an aggregation round
        if len(self._pending_gradients) >= self.min_contributors:
            await self._aggregate()

    def _clip_gradient(self, gradients: dict[str, float]) -> dict[str, float]:
        """Clip gradient to max L2 norm (privacy protection)."""
        if not gradients:
            return gradients

        l2_norm = math.sqrt(sum(v ** 2 for v in gradients.values()))
        if l2_norm > self.clip_norm:
            scale = self.clip_norm / l2_norm
            return {k: v * scale for k, v in gradients.items()}
        return dict(gradients)

    def _add_noise(self, gradients: dict[str, float]) -> dict[str, float]:
        """Add Gaussian noise for differential privacy."""
        return {
            k: v + random.gauss(0, self.noise_sigma)
            for k, v in gradients.items()
        }

    async def _aggregate(self):
        """Aggregate pending gradients using FedAvg."""
        if len(self._pending_gradients) < self.min_contributors:
            return

        self._round += 1
        gradients = self._pending_gradients
        self._pending_gradients = []

        # Weighted average by samples_used
        total_samples = sum(g.samples_used for g in gradients)
        if total_samples == 0:
            total_samples = len(gradients)

        # Collect all weight keys
        all_keys = set()
        for g in gradients:
            all_keys.update(g.gradients.keys())

        # FedAvg aggregation
        aggregated: dict[str, float] = {}
        for key in all_keys:
            weighted_sum = sum(
                g.gradients.get(key, 0) * g.samples_used
                for g in gradients
            )
            aggregated[key] = weighted_sum / total_samples

        # Update global model
        for key, delta in aggregated.items():
            current = self._global_model.weights.get(key, 0.0)
            self._global_model.weights[key] = current + delta

        avg_loss = sum(g.local_loss for g in gradients) / len(gradients)

        self._global_model.round_number = self._round
        self._global_model.contributors = len(gradients)
        self._global_model.avg_loss = round(avg_loss, 6)
        self._global_model.ts = time.time()

        self._history.append(GlobalModel(
            round_number=self._round,
            weights=dict(self._global_model.weights),
            contributors=len(gradients),
            avg_loss=avg_loss,
        ))
        if len(self._history) > 100:
            self._history = self._history[-50:]

        self._stats["rounds"] += 1
        self._stats["updates_published"] += 1

        log.info(
            "FEDERATED: round %d — %d contributors, %d weights updated, avg_loss=%.6f",
            self._round, len(gradients), len(aggregated), avg_loss,
        )

        await self.bus.publish("federated.global_update", {
            "round": self._round,
            "weights": aggregated,
            "contributors": len(gradients),
            "avg_loss": avg_loss,
        })

    def get_global_weights(self) -> dict[str, float]:
        """Get current global model weights for agent consumption."""
        return dict(self._global_model.weights)

    def summary(self) -> dict:
        return {
            **self._stats,
            "current_round": self._round,
            "global_weights": len(self._global_model.weights),
            "pending_gradients": len(self._pending_gradients),
            "convergence": [
                {"round": h.round_number, "loss": round(h.avg_loss, 6), "n": h.contributors}
                for h in self._history[-10:]
            ],
        }
