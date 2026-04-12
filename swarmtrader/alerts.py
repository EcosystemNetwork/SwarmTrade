"""Unified alert routing — PagerDuty, Opsgenie, Slack, and Telegram.

Implements the AlertHook signature (async def(level, message)) expected by
CircuitBreaker and can also be called directly from ObservabilityDashboard.

Each channel is opt-in via env vars.  If no credentials are configured for a
channel it is silently skipped — the system always falls back to logging.

Severity mapping:
  CRITICAL → PagerDuty/Opsgenie incident + Slack + Telegram
  WARNING  → Slack + Telegram
  INFO     → Telegram only (if configured)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import aiohttp

log = logging.getLogger("swarm.alerts")

# Rate-limit: at most one alert per (channel, level) per this many seconds
_RATE_LIMIT_S = 60.0


class AlertRouter:
    """Fan-out alerts to external incident and chat services.

    Usage::

        router = AlertRouter()          # reads env vars
        await router("CRITICAL", "Circuit breaker tripped: 5 consecutive losses")

    Or pass as the ``alert_hook`` to CircuitBreaker::

        cb = CircuitBreaker(bus, kill_switch, alert_hook=router)
    """

    def __init__(
        self,
        pagerduty_routing_key: str = "",
        opsgenie_api_key: str = "",
        slack_webhook_url: str = "",
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
    ):
        self._pd_key = pagerduty_routing_key or os.getenv("PAGERDUTY_ROUTING_KEY", "")
        self._og_key = opsgenie_api_key or os.getenv("OPSGENIE_API_KEY", "")
        self._slack_url = slack_webhook_url or os.getenv("SLACK_ALERT_WEBHOOK", "")
        self._tg_token = telegram_bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._tg_chat = telegram_chat_id or os.getenv("TELEGRAM_ALERT_CHAT", "")

        self._session: aiohttp.ClientSession | None = None
        self._last_sent: dict[str, float] = {}  # "channel:level" -> timestamp

        channels = []
        if self._pd_key:
            channels.append("PagerDuty")
        if self._og_key:
            channels.append("Opsgenie")
        if self._slack_url:
            channels.append("Slack")
        if self._tg_token and self._tg_chat:
            channels.append("Telegram")

        if channels:
            log.info("Alert router: channels=%s", ", ".join(channels))
        else:
            log.warning("Alert router: no external channels configured — alerts will only be logged")

    async def __call__(self, level: str, message: str) -> None:
        """AlertHook-compatible entry point."""
        level = level.upper()
        log.log(
            logging.CRITICAL if level == "CRITICAL" else logging.WARNING,
            "ALERT [%s]: %s", level, message,
        )

        if level == "CRITICAL":
            await self._send_pagerduty(message)
            await self._send_opsgenie(message, priority="P1")
            await self._send_slack(message, level)
            await self._send_telegram(message, level)
        elif level == "WARNING":
            await self._send_slack(message, level)
            await self._send_telegram(message, level)
        else:  # INFO and anything else
            await self._send_telegram(message, level)

    # ── Internal helpers ─────────────────────────────────────────

    def _rate_ok(self, channel: str, level: str) -> bool:
        key = f"{channel}:{level}"
        now = time.time()
        if now - self._last_sent.get(key, 0) < _RATE_LIMIT_S:
            return False
        self._last_sent[key] = now
        return True

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    # ── PagerDuty Events API v2 ──────────────────────────────────

    async def _send_pagerduty(self, message: str) -> None:
        if not self._pd_key or not self._rate_ok("pagerduty", "trigger"):
            return
        session = await self._get_session()
        payload: dict[str, Any] = {
            "routing_key": self._pd_key,
            "event_action": "trigger",
            "payload": {
                "summary": f"[SwarmTrader] {message[:1024]}",
                "severity": "critical",
                "source": "swarmtrader",
                "component": "circuit_breaker",
                "group": "trading",
            },
        }
        try:
            async with session.post(
                "https://events.pagerduty.com/v2/enqueue",
                json=payload,
            ) as resp:
                if resp.status >= 300:
                    body = await resp.text()
                    log.error("PagerDuty alert failed (%d): %s", resp.status, body[:200])
                else:
                    log.info("PagerDuty alert sent")
        except Exception as e:
            log.error("PagerDuty alert error: %s", e)

    # ── Opsgenie Alerts API ──────────────────────────────────────

    async def _send_opsgenie(self, message: str, priority: str = "P1") -> None:
        if not self._og_key or not self._rate_ok("opsgenie", priority):
            return
        session = await self._get_session()
        payload: dict[str, Any] = {
            "message": f"[SwarmTrader] {message[:130]}",
            "description": message[:15000],
            "priority": priority,
            "source": "swarmtrader",
            "tags": ["trading", "circuit_breaker"],
        }
        try:
            async with session.post(
                "https://api.opsgenie.com/v2/alerts",
                json=payload,
                headers={"Authorization": f"GenieKey {self._og_key}"},
            ) as resp:
                if resp.status >= 300:
                    body = await resp.text()
                    log.error("Opsgenie alert failed (%d): %s", resp.status, body[:200])
                else:
                    log.info("Opsgenie alert sent")
        except Exception as e:
            log.error("Opsgenie alert error: %s", e)

    # ── Slack Incoming Webhook ───────────────────────────────────

    async def _send_slack(self, message: str, level: str) -> None:
        if not self._slack_url or not self._rate_ok("slack", level):
            return
        session = await self._get_session()
        icon = ":rotating_light:" if level == "CRITICAL" else ":warning:"
        payload = {
            "text": f"{icon} *SwarmTrader {level}*\n{message}",
        }
        try:
            async with session.post(self._slack_url, json=payload) as resp:
                if resp.status >= 300:
                    body = await resp.text()
                    log.error("Slack alert failed (%d): %s", resp.status, body[:200])
                else:
                    log.info("Slack alert sent")
        except Exception as e:
            log.error("Slack alert error: %s", e)

    # ── Telegram Bot API ─────────────────────────────────────────

    async def _send_telegram(self, message: str, level: str) -> None:
        if not self._tg_token or not self._tg_chat:
            return
        if not self._rate_ok("telegram", level):
            return
        session = await self._get_session()
        icon = "\U0001f6a8" if level == "CRITICAL" else "\u26a0\ufe0f" if level == "WARNING" else "\u2139\ufe0f"
        text = f"{icon} <b>SwarmTrader {level}</b>\n<pre>{message[:4000]}</pre>"
        try:
            async with session.post(
                f"https://api.telegram.org/bot{self._tg_token}/sendMessage",
                json={
                    "chat_id": self._tg_chat,
                    "text": text,
                    "parse_mode": "HTML",
                },
            ) as resp:
                if resp.status >= 300:
                    body = await resp.text()
                    log.error("Telegram alert failed (%d): %s", resp.status, body[:200])
                else:
                    log.info("Telegram alert sent")
        except Exception as e:
            log.error("Telegram alert error: %s", e)

    # ── Cleanup ──────────────────────────────────────────────────

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
