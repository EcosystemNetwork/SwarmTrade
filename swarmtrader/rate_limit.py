"""Shared rate limiter for external API calls.

Coordinates rate limits across all agents hitting the same external services
(CoinGecko, CryptoPanic, Blockchair, etc.) to avoid 429 errors and bans.
"""
from __future__ import annotations
import asyncio, logging, time
from collections import defaultdict

log = logging.getLogger("swarm.rate_limit")


class APIRateLimiter:
    """Token-bucket rate limiter shared across agents.

    Usage:
        limiter = APIRateLimiter()
        limiter.configure("coingecko", max_calls=10, window_s=60)

        async with limiter.acquire("coingecko"):
            resp = await session.get(url)
    """

    def __init__(self):
        self._configs: dict[str, dict] = {}
        self._calls: dict[str, list[float]] = defaultdict(list)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def configure(self, service: str, max_calls: int, window_s: float):
        """Set rate limit for a service."""
        self._configs[service] = {
            "max_calls": max_calls,
            "window_s": window_s,
        }

    def acquire(self, service: str) -> "_RateLimitContext":
        """Return an async context manager that waits for a slot."""
        return _RateLimitContext(self, service)

    async def _wait_for_slot(self, service: str):
        """Wait until a request slot is available."""
        cfg = self._configs.get(service)
        if not cfg:
            return  # No limit configured, allow through

        async with self._locks[service]:
            now = time.time()
            window = cfg["window_s"]
            max_calls = cfg["max_calls"]

            # Prune old entries
            calls = self._calls[service]
            cutoff = now - window
            self._calls[service] = [t for t in calls if t > cutoff]
            calls = self._calls[service]

            if len(calls) >= max_calls:
                # Wait until the oldest call expires
                wait_time = calls[0] - cutoff
                if wait_time > 0:
                    log.debug("Rate limit %s: waiting %.1fs (%d/%d used)",
                              service, wait_time, len(calls), max_calls)
                    await asyncio.sleep(wait_time)
                    # Re-prune after waiting
                    now = time.time()
                    self._calls[service] = [t for t in self._calls[service]
                                            if t > now - window]

            self._calls[service].append(time.time())

    def usage(self, service: str) -> dict:
        """Return current usage stats for a service."""
        cfg = self._configs.get(service, {})
        window = cfg.get("window_s", 60)
        max_calls = cfg.get("max_calls", 0)
        now = time.time()
        recent = [t for t in self._calls.get(service, []) if t > now - window]
        return {
            "service": service,
            "used": len(recent),
            "limit": max_calls,
            "window_s": window,
            "available": max(0, max_calls - len(recent)),
        }

    def summary(self) -> dict:
        """Return usage for all configured services."""
        return {svc: self.usage(svc) for svc in self._configs}


class _RateLimitContext:
    def __init__(self, limiter: APIRateLimiter, service: str):
        self._limiter = limiter
        self._service = service

    async def __aenter__(self):
        await self._limiter._wait_for_slot(self._service)
        return self

    async def __aexit__(self, *exc):
        pass


# ── Global instance (shared across agents) ────────────────────

_global_limiter: APIRateLimiter | None = None


def get_api_limiter() -> APIRateLimiter:
    """Get or create the global API rate limiter with default configs."""
    global _global_limiter
    if _global_limiter is None:
        _global_limiter = APIRateLimiter()
        # Default rate limits for known services
        _global_limiter.configure("coingecko", max_calls=10, window_s=60)
        _global_limiter.configure("cryptopanic", max_calls=5, window_s=60)
        _global_limiter.configure("blockchair", max_calls=5, window_s=60)
        _global_limiter.configure("etherscan", max_calls=5, window_s=60)
        _global_limiter.configure("reddit", max_calls=10, window_s=60)
        _global_limiter.configure("feargreed", max_calls=5, window_s=300)
        _global_limiter.configure("kraken_rest", max_calls=15, window_s=60)
    return _global_limiter
