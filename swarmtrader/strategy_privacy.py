"""Strategy privacy layer — commit-reveal scheme for signal weights.

Prevents front-running and strategy copying by:
1. Hashing strategy weights + salt before publishing
2. Only revealing the actual weights after execution
3. On-chain commit hash can prove strategy existed at a given time

This is simpler than FHE (CipherTradeArena's approach) but still
provides meaningful privacy for competitive trading.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field

log = logging.getLogger("swarm.privacy")


@dataclass
class StrategyCommit:
    """A committed (hidden) strategy configuration."""
    commit_hash: str       # SHA-256(weights_json + salt)
    salt: str              # random hex salt (kept secret until reveal)
    timestamp: float       # when committed
    revealed: bool = False
    weights_json: str = "" # populated on reveal


@dataclass
class StrategyReveal:
    """A revealed strategy with proof of prior commitment."""
    commit_hash: str
    salt: str
    weights: dict[str, float]
    timestamp_committed: float
    timestamp_revealed: float
    valid: bool  # does the reveal match the commit?


class StrategyPrivacyManager:
    """Manages commit-reveal lifecycle for strategy configurations.

    Usage:
        1. Before trading: commit(weights) → commit_hash
        2. During trading: strategies are hidden (only hash is public)
        3. After trading: reveal(commit_hash) → proves strategy existed
        4. On-chain: commit_hash can be posted to ERC-8004 validation registry
    """

    def __init__(self):
        self._commits: dict[str, StrategyCommit] = {}  # hash -> commit
        self._active_commit: str | None = None

    def commit(self, weights: dict[str, float],
               metadata: dict | None = None) -> StrategyCommit:
        """Commit a strategy configuration (hide it).

        Args:
            weights: Agent weight dictionary
            metadata: Optional metadata (regime, threshold, etc.)

        Returns:
            StrategyCommit with hash and salt
        """
        salt = secrets.token_hex(32)

        payload = {
            "weights": {k: round(v, 6) for k, v in sorted(weights.items())},
            "metadata": metadata or {},
        }
        weights_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))

        # SHA-256(weights_json + salt)
        commit_hash = hashlib.sha256(
            (weights_json + salt).encode("utf-8")
        ).hexdigest()

        commit = StrategyCommit(
            commit_hash=commit_hash,
            salt=salt,
            timestamp=time.time(),
            weights_json=weights_json,
        )
        self._commits[commit_hash] = commit
        self._active_commit = commit_hash

        log.info("Strategy committed: hash=%s... (weights hidden)", commit_hash[:16])
        return commit

    def reveal(self, commit_hash: str) -> StrategyReveal:
        """Reveal a previously committed strategy.

        Args:
            commit_hash: The commit hash to reveal

        Returns:
            StrategyReveal with proof of validity
        """
        commit = self._commits.get(commit_hash)
        if not commit:
            raise ValueError(f"Unknown commit hash: {commit_hash}")

        # Verify: re-hash and compare
        verify_hash = hashlib.sha256(
            (commit.weights_json + commit.salt).encode("utf-8")
        ).hexdigest()

        valid = hmac.compare_digest(verify_hash, commit_hash)
        payload = json.loads(commit.weights_json)

        commit.revealed = True

        reveal = StrategyReveal(
            commit_hash=commit_hash,
            salt=commit.salt,
            weights=payload.get("weights", {}),
            timestamp_committed=commit.timestamp,
            timestamp_revealed=time.time(),
            valid=valid,
        )

        log.info("Strategy revealed: hash=%s... valid=%s (committed %.0fs ago)",
                 commit_hash[:16], valid, reveal.timestamp_revealed - reveal.timestamp_committed)

        return reveal

    def verify_external(self, commit_hash: str, weights_json: str, salt: str) -> bool:
        """Verify someone else's commit-reveal (for on-chain validation).

        Args:
            commit_hash: The published commit hash
            weights_json: The revealed weights JSON
            salt: The revealed salt

        Returns:
            True if the reveal matches the commit
        """
        verify = hashlib.sha256(
            (weights_json + salt).encode("utf-8")
        ).hexdigest()
        return hmac.compare_digest(verify, commit_hash)

    @property
    def active_commit(self) -> StrategyCommit | None:
        if self._active_commit:
            return self._commits.get(self._active_commit)
        return None

    @property
    def commit_history(self) -> list[dict]:
        return [
            {
                "hash": c.commit_hash[:16] + "...",
                "timestamp": c.timestamp,
                "revealed": c.revealed,
            }
            for c in sorted(self._commits.values(), key=lambda x: x.timestamp)
        ]
