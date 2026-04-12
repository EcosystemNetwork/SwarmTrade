"""Stripe Subscription Billing — tiered pricing for the platform.

Tiers:
  Free ($0/mo)           — Paper trading, 1 strategy, delayed data
  Starter ($29/mo)       — Live trading, $10K max capital, 3 strategies
  Pro ($99/mo)           — Unlimited capital, all strategies, priority execution
  Institutional ($499/mo) — Everything + custom strategies + dedicated support

Also supports:
  - Usage-based billing: 0.1% of profit (performance fee, optional)
  - Annual discount: 2 months free on yearly plans
  - Referral credits: 1 month free per referred paying user
  - Free trial: 14 days of Pro tier

Environment variables:
  STRIPE_SECRET_KEY      — Stripe API secret key
  STRIPE_WEBHOOK_SECRET  — Stripe webhook signing secret
  STRIPE_PRICE_STARTER   — Stripe price ID for Starter tier
  STRIPE_PRICE_PRO       — Stripe price ID for Pro tier
  STRIPE_PRICE_INST      — Stripe price ID for Institutional tier
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

import aiohttp

from .core import Bus

log = logging.getLogger("swarm.billing")

STRIPE_API = "https://api.stripe.com/v1"
STRIPE_KEY = os.getenv("STRIPE_SECRET_KEY", "")

# Stripe price IDs (set these after creating products in Stripe dashboard)
PRICE_IDS = {
    "starter_monthly": os.getenv("STRIPE_PRICE_STARTER", ""),
    "pro_monthly": os.getenv("STRIPE_PRICE_PRO", ""),
    "institutional_monthly": os.getenv("STRIPE_PRICE_INST", ""),
}


@dataclass
class Subscription:
    """A user's subscription state."""
    user_id: str
    stripe_customer_id: str = ""
    stripe_subscription_id: str = ""
    tier: str = "free"
    status: str = "active"        # active, past_due, cancelled, trialing
    current_period_start: float = 0.0
    current_period_end: float = 0.0
    # Trial
    trial_end: float = 0.0
    is_trial: bool = False
    # Revenue tracking
    total_paid: float = 0.0
    months_active: int = 0
    # Referral
    referral_code: str = ""
    referred_by: str = ""
    referral_credits: int = 0     # free months earned


@dataclass
class Invoice:
    """A billing invoice."""
    invoice_id: str
    user_id: str
    amount_usd: float
    tier: str
    status: str = "pending"       # pending, paid, failed
    stripe_invoice_id: str = ""
    ts: float = field(default_factory=time.time)


class BillingManager:
    """Manages Stripe subscriptions and billing.

    Flow:
      1. User registers (free tier)
      2. User selects paid tier -> create Stripe checkout session
      3. Stripe processes payment -> webhook confirms
      4. BillingManager upgrades user tier
      5. Monthly renewal handled by Stripe automatically
      6. Cancellation/downgrade handled via webhook

    When Stripe key is not configured, billing runs in simulation mode
    (all upgrades succeed immediately, no actual charges).
    """

    def __init__(self, bus: Bus | None = None, auth_manager=None):
        self.bus = bus
        self.auth = auth_manager
        self._subscriptions: dict[str, Subscription] = {}  # user_id -> sub
        self._invoices: list[Invoice] = []
        self._invoice_counter = 0
        self._simulate = not STRIPE_KEY
        self._stats = {
            "subscriptions": 0, "total_revenue": 0.0,
            "mrr": 0.0, "trials_active": 0,
        }

        if self._simulate:
            log.info("Billing: simulation mode (no STRIPE_SECRET_KEY)")
        else:
            log.info("Billing: Stripe connected")

    def get_subscription(self, user_id: str) -> Subscription:
        if user_id not in self._subscriptions:
            self._subscriptions[user_id] = Subscription(user_id=user_id)
        return self._subscriptions[user_id]

    # ── Checkout ─────────────────────────────────────────────────

    async def create_checkout(self, user_id: str, tier: str,
                               success_url: str = "", cancel_url: str = "") -> str:
        """Create a Stripe checkout session. Returns checkout URL."""
        if self._simulate:
            # Simulate: instant upgrade
            self._upgrade(user_id, tier)
            return f"/billing/success?tier={tier}"

        price_id = PRICE_IDS.get(f"{tier}_monthly", "")
        if not price_id:
            raise ValueError(f"No Stripe price configured for tier: {tier}")

        sub = self.get_subscription(user_id)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{STRIPE_API}/checkout/sessions",
                headers={"Authorization": f"Bearer {STRIPE_KEY}"},
                data={
                    "mode": "subscription",
                    "line_items[0][price]": price_id,
                    "line_items[0][quantity]": "1",
                    "success_url": success_url or "https://swarmtrade.app/billing/success",
                    "cancel_url": cancel_url or "https://swarmtrade.app/billing/cancel",
                    "client_reference_id": user_id,
                    "customer": sub.stripe_customer_id or "",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                return data.get("url", "")

    async def cancel_subscription(self, user_id: str) -> bool:
        """Cancel a subscription."""
        sub = self.get_subscription(user_id)

        if self._simulate:
            sub.status = "cancelled"
            sub.tier = "free"
            if self.auth:
                self.auth.upgrade_tier(user_id, "free")
            return True

        if not sub.stripe_subscription_id:
            return False

        async with aiohttp.ClientSession() as session:
            async with session.delete(
                f"{STRIPE_API}/subscriptions/{sub.stripe_subscription_id}",
                headers={"Authorization": f"Bearer {STRIPE_KEY}"},
            ) as resp:
                if resp.status == 200:
                    sub.status = "cancelled"
                    sub.tier = "free"
                    if self.auth:
                        self.auth.upgrade_tier(user_id, "free")
                    return True
        return False

    # ── Webhook Processing ───────────────────────────────────────

    async def handle_webhook(self, event_type: str, data: dict):
        """Process Stripe webhook events."""
        if event_type == "checkout.session.completed":
            user_id = data.get("client_reference_id", "")
            customer_id = data.get("customer", "")
            subscription_id = data.get("subscription", "")

            if user_id and subscription_id:
                sub = self.get_subscription(user_id)
                sub.stripe_customer_id = customer_id
                sub.stripe_subscription_id = subscription_id
                sub.status = "active"
                # Determine tier from subscription metadata
                log.info("Subscription activated: %s", user_id)

        elif event_type == "invoice.paid":
            customer_id = data.get("customer", "")
            amount = data.get("amount_paid", 0) / 100  # cents to dollars
            # Find user by customer ID
            for sub in self._subscriptions.values():
                if sub.stripe_customer_id == customer_id:
                    sub.total_paid += amount
                    sub.months_active += 1
                    self._stats["total_revenue"] += amount
                    break

        elif event_type == "customer.subscription.deleted":
            sub_id = data.get("id", "")
            for sub in self._subscriptions.values():
                if sub.stripe_subscription_id == sub_id:
                    sub.status = "cancelled"
                    sub.tier = "free"
                    if self.auth:
                        self.auth.upgrade_tier(sub.user_id, "free")
                    break

    # ── Trial ────────────────────────────────────────────────────

    def start_trial(self, user_id: str, days: int = 14):
        """Start a free trial of Pro tier."""
        sub = self.get_subscription(user_id)
        sub.tier = "pro"
        sub.status = "trialing"
        sub.is_trial = True
        sub.trial_end = time.time() + (days * 86400)
        self._stats["trials_active"] += 1

        if self.auth:
            self.auth.upgrade_tier(user_id, "pro")

        log.info("Trial started: %s (%d days of Pro)", user_id, days)

    def check_trial_expiry(self):
        """Check and expire ended trials."""
        now = time.time()
        for sub in self._subscriptions.values():
            if sub.is_trial and now > sub.trial_end:
                sub.is_trial = False
                sub.tier = "free"
                sub.status = "active"
                self._stats["trials_active"] -= 1
                if self.auth:
                    self.auth.upgrade_tier(sub.user_id, "free")
                log.info("Trial expired: %s -> free tier", sub.user_id)

    # ── Referrals ────────────────────────────────────────────────

    def apply_referral(self, user_id: str, referral_code: str) -> bool:
        """Apply a referral code (1 month free for both parties)."""
        # Find referrer
        for sub in self._subscriptions.values():
            if sub.referral_code == referral_code and sub.user_id != user_id:
                sub.referral_credits += 1
                new_sub = self.get_subscription(user_id)
                new_sub.referred_by = sub.user_id
                new_sub.referral_credits += 1
                log.info("Referral applied: %s referred by %s", user_id, sub.user_id)
                return True
        return False

    # ── Internal ─────────────────────────────────────────────────

    def _upgrade(self, user_id: str, tier: str):
        """Internal upgrade (simulation mode)."""
        from .auth import TIERS
        sub = self.get_subscription(user_id)
        sub.tier = tier
        sub.status = "active"
        self._stats["subscriptions"] += 1

        price = TIERS.get(tier, {}).get("price_usd", 0)
        self._stats["mrr"] += price

        if self.auth:
            self.auth.upgrade_tier(user_id, tier)

        self._invoice_counter += 1
        self._invoices.append(Invoice(
            invoice_id=f"inv-{self._invoice_counter:06d}",
            user_id=user_id,
            amount_usd=price,
            tier=tier,
            status="paid",
        ))

        log.info("Subscription: %s -> %s ($%d/mo)", user_id, tier, price)

    def _update_mrr(self):
        """Recalculate MRR from active subscriptions."""
        from .auth import TIERS
        self._stats["mrr"] = sum(
            TIERS.get(sub.tier, {}).get("price_usd", 0)
            for sub in self._subscriptions.values()
            if sub.status == "active" and sub.tier != "free"
        )

    def summary(self) -> dict:
        self._update_mrr()
        return {
            **self._stats,
            "by_tier": {
                tier: len([s for s in self._subscriptions.values() if s.tier == tier])
                for tier in ["free", "starter", "pro", "institutional"]
            },
            "recent_invoices": [
                {"id": i.invoice_id, "tier": i.tier, "amount": i.amount_usd, "status": i.status}
                for i in self._invoices[-5:]
            ],
        }
