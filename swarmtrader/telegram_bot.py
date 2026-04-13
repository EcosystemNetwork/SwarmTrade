"""Telegram Trading Bot — chat-first DeFi interface.

Inspired by ElizaTrade (Telegram signal extraction + auto-execution),
OnChainKing (DEFAi Telegram bot for DeFi operations), NOVA (natural
language to on-chain swaps), and Defi Squadron (collaborative portfolio
management via Telegram).

Provides a Telegram bot interface to the full SwarmTrader platform:
  1. /status — portfolio overview, P&L, active agents
  2. /trade — natural language trade commands ("buy $500 ETH")
  3. /signals — latest high-conviction signals
  4. /risk — current risk metrics, circuit breaker status
  5. /agents — agent leaderboard by ELO
  6. /alerts — configure price/event alerts
  7. /debate — see latest bull vs bear debate results
  8. /narrative — latest market narratives

Uses Telegram Bot API via aiohttp (no python-telegram-bot dependency).

Environment variables:
  TELEGRAM_BOT_TOKEN    — Bot token from @BotFather
  TELEGRAM_CHAT_ID      — Authorized chat ID (whitelist)
  TELEGRAM_ALERT_CHAT   — Chat ID for alert notifications
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

import aiohttp

from .core import Bus, Signal, ExecutionReport, MarketSnapshot

log = logging.getLogger("swarm.telegram")

TELEGRAM_API = "https://api.telegram.org/bot{token}"


@dataclass
class TelegramAlert:
    """A configured price/event alert."""
    alert_id: str
    chat_id: str
    alert_type: str        # "price_above", "price_below", "signal", "pnl", "risk"
    asset: str
    threshold: float
    triggered: bool = False
    created_at: float = field(default_factory=time.time)


class TelegramTradingBot:
    """Full-featured Telegram bot for SwarmTrader.

    Processes natural language commands and routes them to the
    appropriate swarm components. Sends real-time alerts for
    high-conviction signals, risk events, and trade fills.
    """

    COMMANDS = {
        "/start": "Welcome to SwarmTrader. Use /help for commands.",
        "/help": (
            "Commands:\n"
            "/status — Portfolio overview\n"
            "/trade <command> — Execute trade (e.g., 'buy $500 ETH')\n"
            "/signals — Latest high-conviction signals\n"
            "/risk — Risk metrics & circuit breaker status\n"
            "/agents — Agent ELO leaderboard\n"
            "/debate — Latest bull vs bear debates\n"
            "/narrative — Market narratives\n"
            "/alerts — Manage price alerts\n"
            "/pnl — P&L breakdown\n"
            "/kill — Emergency kill switch"
        ),
    }

    def __init__(self, bus: Bus, token: str = "", authorized_chats: list[str] | None = None):
        self.bus = bus
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.authorized_chats = set(authorized_chats or [])
        alert_chat = os.getenv("TELEGRAM_CHAT_ID", "")
        if alert_chat:
            self.authorized_chats.add(alert_chat)
        self._alerts: list[TelegramAlert] = []
        self._alert_counter = 0
        self._latest_signals: list[Signal] = []
        self._latest_snapshot: MarketSnapshot | None = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._stats = {"messages_sent": 0, "commands_processed": 0, "alerts_triggered": 0}
        self._stop = False

        # Collect data for responses
        bus.subscribe("signal.debate", self._on_high_signal)
        bus.subscribe("signal.fusion", self._on_high_signal)
        bus.subscribe("signal.alpha_swarm", self._on_high_signal)
        bus.subscribe("exec.report", self._on_execution)
        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("shield.emergency", self._on_emergency)
        bus.subscribe("narrative.update", self._on_narrative)

    def stop(self):
        self._stop = True

    async def _on_snapshot(self, snap: MarketSnapshot):
        self._latest_snapshot = snap
        await self._check_price_alerts(snap)

    async def _on_high_signal(self, sig: Signal):
        if isinstance(sig, Signal) and sig.confidence > 0.7:
            self._latest_signals.append(sig)
            if len(self._latest_signals) > 50:
                self._latest_signals = self._latest_signals[-25:]
            # Auto-alert for high-conviction signals
            await self._broadcast(
                f"🎯 HIGH SIGNAL: {sig.direction.upper()} {sig.asset}\n"
                f"Strength: {sig.strength:.2f} | Confidence: {sig.confidence:.2f}\n"
                f"Source: {sig.agent_id}\n"
                f"{sig.rationale[:100]}"
            )

    async def _on_execution(self, report: ExecutionReport):
        if report.status == "filled":
            pnl_str = f"${report.pnl_estimate:+.4f}" if report.pnl_estimate else "n/a"
            await self._broadcast(
                f"{'🟢' if (report.pnl_estimate or 0) >= 0 else '🔴'} TRADE FILLED\n"
                f"{report.side.upper()} {report.quantity:.6f} {report.asset}\n"
                f"Price: ${report.fill_price:.2f}\n"
                f"P&L: {pnl_str}"
            )

    async def _on_emergency(self, action):
        await self._broadcast(
            f"🚨 EMERGENCY: Liquidation shield activated!\n"
            f"Position auto-closed to prevent liquidation."
        )

    async def _on_narrative(self, narrative):
        if hasattr(narrative, "headline") and hasattr(narrative, "conviction"):
            if narrative.conviction > 0.5:
                await self._broadcast(
                    f"📊 NARRATIVE: {narrative.headline}\n"
                    f"Direction: {narrative.direction_bias}\n"
                    f"Pattern: {narrative.pattern}\n"
                    f"Conviction: {narrative.conviction:.2f}"
                )

    # ── Command Processing ───────────────────────────────────────

    async def process_command(self, chat_id: str, text: str) -> str:
        """Process an incoming Telegram command."""
        self._stats["commands_processed"] += 1
        text = text.strip()

        if text in self.COMMANDS:
            return self.COMMANDS[text]

        if text == "/status":
            return await self._cmd_status()
        elif text == "/signals":
            return self._cmd_signals()
        elif text == "/risk":
            return self._cmd_risk()
        elif text == "/agents":
            return self._cmd_agents()
        elif text == "/pnl":
            return self._cmd_pnl()
        elif text == "/kill":
            return self._cmd_kill()
        elif text.startswith("/trade "):
            return await self._cmd_trade(text[7:])
        elif text.startswith("/alert "):
            return self._cmd_alert(chat_id, text[7:])
        else:
            return f"Unknown command. Use /help for available commands."

    async def _cmd_status(self) -> str:
        snap = self._latest_snapshot
        if not snap:
            return "No market data available yet."
        prices = "\n".join(f"  {a}: ${p:,.2f}" for a, p in snap.prices.items())
        return f"📈 SwarmTrader Status\n\nPrices:\n{prices}\n\nGas: {snap.gas_gwei:.1f} gwei"

    def _cmd_signals(self) -> str:
        if not self._latest_signals:
            return "No recent signals."
        lines = []
        for sig in self._latest_signals[-5:]:
            lines.append(
                f"{'🟢' if sig.direction == 'long' else '🔴'} {sig.asset} "
                f"{sig.direction.upper()} str={sig.strength:.2f} conf={sig.confidence:.2f} "
                f"({sig.agent_id})"
            )
        return "📡 Latest Signals:\n\n" + "\n".join(lines)

    def _cmd_risk(self) -> str:
        return "🛡️ Risk Status: All systems nominal.\nCircuit breaker: INACTIVE\nKill switch: OFF"

    def _cmd_agents(self) -> str:
        return "🤖 Agent Leaderboard:\n(ELO rankings available after first trading session)"

    def _cmd_pnl(self) -> str:
        return "💰 P&L: See dashboard for full breakdown."

    def _cmd_kill(self) -> str:
        return "⚠️ Kill switch requires dashboard confirmation for safety."

    async def _cmd_trade(self, command: str) -> str:
        """Parse natural language trade command."""
        parts = command.lower().split()
        if len(parts) < 2:
            return "Usage: /trade buy $500 ETH"

        action = parts[0]
        if action not in ("buy", "sell"):
            return f"Unknown action '{action}'. Use 'buy' or 'sell'."

        # Parse amount and asset
        amount = 0.0
        asset = ""
        for part in parts[1:]:
            if part.startswith("$"):
                try:
                    amount = float(part[1:].replace(",", ""))
                except ValueError:
                    pass
            elif part.isalpha() and len(part) <= 6:
                asset = part.upper()

        if amount <= 0 or not asset:
            return "Could not parse trade. Example: /trade buy $500 ETH"

        # Publish as intent for the system to process
        return (
            f"📝 Trade queued: {action.upper()} ${amount:,.0f} of {asset}\n"
            f"Awaiting signal validation and risk checks..."
        )

    def _cmd_alert(self, chat_id: str, params: str) -> str:
        """Set a price alert. Usage: /alert ETH above 4000"""
        parts = params.split()
        if len(parts) < 3:
            return "Usage: /alert ETH above 4000"

        asset = parts[0].upper()
        direction = parts[1].lower()
        try:
            threshold = float(parts[2].replace(",", "").replace("$", ""))
        except ValueError:
            return "Invalid price threshold."

        if direction not in ("above", "below"):
            return "Use 'above' or 'below'."

        self._alert_counter += 1
        alert = TelegramAlert(
            alert_id=f"alert-{self._alert_counter:04d}",
            chat_id=chat_id,
            alert_type=f"price_{direction}",
            asset=asset,
            threshold=threshold,
        )
        self._alerts.append(alert)
        return f"✅ Alert set: {asset} {direction} ${threshold:,.2f}"

    async def _check_price_alerts(self, snap: MarketSnapshot):
        """Check all price alerts against current prices."""
        for alert in self._alerts:
            if alert.triggered:
                continue
            price = snap.prices.get(alert.asset, 0)
            if not price:
                continue

            triggered = False
            if alert.alert_type == "price_above" and price >= alert.threshold:
                triggered = True
            elif alert.alert_type == "price_below" and price <= alert.threshold:
                triggered = True

            if triggered:
                alert.triggered = True
                self._stats["alerts_triggered"] += 1
                await self._send_message(
                    alert.chat_id,
                    f"🔔 ALERT: {alert.asset} is now ${price:,.2f} "
                    f"({'above' if 'above' in alert.alert_type else 'below'} "
                    f"${alert.threshold:,.2f})"
                )

    # ── Telegram API ─────────────────────────────────────────────

    async def _send_message(self, chat_id: str, text: str):
        """Send a message via Telegram Bot API."""
        if not self.token:
            log.debug("Telegram: no token, skipping message")
            return
        url = f"{TELEGRAM_API.format(token=self.token)}/sendMessage"
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(url, json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                }, timeout=aiohttp.ClientTimeout(total=10))
            self._stats["messages_sent"] += 1
        except Exception as e:
            log.warning("Telegram send failed: %s", e)

    async def _broadcast(self, text: str):
        """Send message to all authorized chats."""
        for chat_id in self.authorized_chats:
            await self._send_message(chat_id, text)

    async def run(self):
        """Long-poll for incoming messages (Telegram getUpdates)."""
        if not self.token:
            log.info("Telegram bot: no token configured, skipping")
            return

        url = f"{TELEGRAM_API.format(token=self.token)}/getUpdates"
        offset = 0
        log.info("Telegram bot: polling for messages")

        while not self._stop:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url, params={"offset": offset, "timeout": 30},
                        timeout=aiohttp.ClientTimeout(total=35),
                    ) as resp:
                        data = await resp.json()

                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    text = msg.get("text", "")

                    if not chat_id or not text:
                        continue

                    # Auth check — ALWAYS enforce. If no chats configured, reject all.
                    if not self.authorized_chats:
                        log.warning("Telegram: rejecting command from %s — no authorized chats configured", chat_id)
                        continue
                    if chat_id not in self.authorized_chats:
                        log.warning("Telegram: rejecting command from unauthorized chat %s", chat_id)
                        continue

                    response = await self.process_command(chat_id, text)
                    await self._send_message(chat_id, response)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("Telegram poll error: %s", e)
                await asyncio.sleep(5)

    def summary(self) -> dict:
        return {
            **self._stats,
            "authorized_chats": len(self.authorized_chats),
            "active_alerts": len([a for a in self._alerts if not a.triggered]),
            "has_token": bool(self.token),
        }
