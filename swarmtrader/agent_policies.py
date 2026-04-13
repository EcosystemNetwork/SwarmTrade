"""Per-Agent Spending Policies — enforce trade limits at the agent level.

Inspired by AgentPass (ETHGlobal Cannes 2026) and LeAgent spending controls.
Each agent in the swarm gets a policy that constrains:
  - Max trade size per transaction
  - Daily spending cap
  - Whitelisted tokens and contracts
  - Rate limits (trades per hour)
  - Auto-approve threshold (small trades skip human approval)

Policies are enforced locally as a risk check and can optionally be
mirrored on-chain via an AgentExecutor smart contract (ERC-4337).

Environment variables:
  AGENT_POLICY_STRICT    — if "true", reject all trades without policy (default: false)
  AGENT_POLICY_DEFAULT_DAILY_CAP — default daily cap per agent in USD (default: 1000)
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .core import Bus, TradeIntent, RiskVerdict

log = logging.getLogger("swarm.policies")


class ExecutionSandbox:
    """Sandboxed Agent Execution — agents trade but can't withdraw.

    Inspired by Meme Sentinels (ETHGlobal HackMoney 2026). Enforces
    permission scoping at the action level. Even if an agent's decision
    logic is compromised (prompt injection, bad data), it physically
    cannot perform destructive operations.

    Permission levels:
      TRADE_ONLY  — place_order, cancel_order, amend_order (DEFAULT)
      READ_ONLY   — view positions, balances, market data
      FULL_ACCESS — all operations (human operator only, requires 2FA)

    Blocked actions for TRADE_ONLY:
      - withdraw, transfer, send
      - change_api_keys, modify_risk_params
      - delete_agent, modify_policy
    """

    TRADE_ONLY = "trade_only"
    READ_ONLY = "read_only"
    FULL_ACCESS = "full_access"

    # Actions allowed per permission level
    ALLOWED_ACTIONS: dict[str, set[str]] = {
        "trade_only": {
            "place_order", "cancel_order", "amend_order",
            "view_positions", "view_balances", "view_market",
            "publish_signal", "subscribe_topic",
        },
        "read_only": {
            "view_positions", "view_balances", "view_market",
            "subscribe_topic",
        },
        "full_access": {
            "place_order", "cancel_order", "amend_order",
            "view_positions", "view_balances", "view_market",
            "publish_signal", "subscribe_topic",
            "withdraw", "transfer", "send",
            "change_api_keys", "modify_risk_params",
            "delete_agent", "modify_policy",
        },
    }

    BLOCKED_ACTIONS_LOG: list[dict] = []

    def __init__(self):
        self._agent_permissions: dict[str, str] = {}  # agent_id -> level
        self._blocked_attempts: list[dict] = []

    def set_permission(self, agent_id: str, level: str):
        """Set permission level for an agent."""
        if level not in self.ALLOWED_ACTIONS:
            raise ValueError(f"Invalid permission level: {level}")
        self._agent_permissions[agent_id] = level
        log.info("Sandbox: agent %s set to %s", agent_id, level)

    def get_permission(self, agent_id: str) -> str:
        """Get permission level, defaulting to TRADE_ONLY."""
        return self._agent_permissions.get(agent_id, self.TRADE_ONLY)

    def check_action(self, agent_id: str, action: str) -> tuple[bool, str]:
        """Check if an agent is allowed to perform an action.

        Returns (allowed, reason).
        """
        level = self.get_permission(agent_id)
        allowed = self.ALLOWED_ACTIONS.get(level, set())

        if action in allowed:
            return True, "ok"

        # Block and log the attempt
        record = {
            "agent_id": agent_id,
            "action": action,
            "permission_level": level,
            "ts": time.time(),
        }
        self._blocked_attempts.append(record)
        if len(self._blocked_attempts) > 500:
            self._blocked_attempts = self._blocked_attempts[-250:]

        log.warning(
            "SANDBOX BLOCKED: agent %s attempted '%s' (level=%s)",
            agent_id, action, level,
        )
        return False, f"action '{action}' not allowed for permission level '{level}'"

    def summary(self) -> dict:
        return {
            "agents": {
                aid: level for aid, level in self._agent_permissions.items()
            },
            "blocked_attempts": len(self._blocked_attempts),
            "recent_blocks": self._blocked_attempts[-5:],
        }


@dataclass
class AgentPolicy:
    """Spending and trading policy for a single agent."""
    agent_id: str
    # Spending limits
    max_trade_usd: float = 500.0          # max single trade
    daily_cap_usd: float = 1000.0         # max daily total
    auto_approve_usd: float = 50.0        # trades below this skip manual approval
    # Rate limits
    max_trades_per_hour: int = 20
    max_trades_per_day: int = 100
    # Whitelists (empty = allow all)
    allowed_tokens: list[str] = field(default_factory=list)
    allowed_contracts: list[str] = field(default_factory=list)
    # Restrictions
    blocked_tokens: list[str] = field(default_factory=list)
    can_go_short: bool = True
    can_use_leverage: bool = False
    max_position_pct: float = 25.0        # max % of portfolio in one position
    # Sandbox permission level
    sandbox_level: str = ExecutionSandbox.TRADE_ONLY
    # Metadata
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    created_by: str = "system"
    notes: str = ""


@dataclass
class PolicyViolation:
    """Record of a policy violation."""
    agent_id: str
    rule: str
    detail: str
    trade_intent_id: str
    timestamp: float = field(default_factory=time.time)
    severity: str = "warning"  # "warning", "block", "alert"


class AgentPolicyEngine:
    """Enforces per-agent spending and trading policies.

    Integrates as a risk check in the existing risk pipeline.
    Each agent gets a policy (custom or default), and every
    TradeIntent is validated against the originating agent's policy.

    Usage:
      engine = AgentPolicyEngine(bus)
      engine.set_policy("momentum", AgentPolicy(agent_id="momentum", max_trade_usd=200))
      # Then use policy_check(engine) as a RiskAgent check function
    """

    def __init__(self, bus: Bus, strict: bool = False, default_daily_cap: float = 1000.0):
        self.bus = bus
        self.strict = strict
        self.default_daily_cap = default_daily_cap
        self.policies: dict[str, AgentPolicy] = {}
        self._daily_spend: dict[str, float] = {}  # agent_id -> USD spent today
        self._daily_trades: dict[str, int] = {}
        self._hourly_trades: dict[str, list[float]] = {}
        self._violations: list[PolicyViolation] = []
        self._intent_agents: OrderedDict[str, list[str]] = OrderedDict()  # intent_id -> agent_ids
        self._day_start: float = _day_start()

        # Subscribe to execution reports to track spending
        bus.subscribe("exec.report", self._on_execution)

    def set_policy(self, agent_id: str, policy: AgentPolicy):
        """Set or update policy for an agent."""
        policy.updated_at = time.time()
        self.policies[agent_id] = policy
        log.info(
            "Policy set for %s: max_trade=$%.0f daily_cap=$%.0f auto_approve=$%.0f",
            agent_id, policy.max_trade_usd, policy.daily_cap_usd, policy.auto_approve_usd,
        )

    def get_policy(self, agent_id: str) -> AgentPolicy:
        """Get policy for an agent, creating default if needed."""
        if agent_id not in self.policies:
            self.policies[agent_id] = AgentPolicy(
                agent_id=agent_id,
                daily_cap_usd=self.default_daily_cap,
            )
        return self.policies[agent_id]

    def check_intent(self, intent: TradeIntent) -> tuple[bool, str]:
        """Check a trade intent against the originating agent's policy.

        Returns (approved, reason).
        """
        self._maybe_reset_daily()

        # Determine which agent originated this intent
        agent_ids = [s.agent_id for s in intent.supporting] if intent.supporting else []
        if not agent_ids:
            if self.strict:
                return False, "no originating agent identified (strict mode)"
            return True, "ok (no agent attribution)"

        # Track intent -> agents for attribution when execution reports arrive
        self._intent_agents[intent.id] = agent_ids
        if len(self._intent_agents) > 5000:
            while len(self._intent_agents) > 2500:
                self._intent_agents.popitem(last=False)

        # Check against each supporting agent's policy
        for agent_id in agent_ids:
            policy = self.get_policy(agent_id)

            # 1. Token whitelist check
            if policy.allowed_tokens:
                if intent.asset_in not in policy.allowed_tokens and intent.asset_out not in policy.allowed_tokens:
                    self._record_violation(agent_id, "token_whitelist",
                                           f"{intent.asset_in}/{intent.asset_out} not in whitelist",
                                           intent.id, "block")
                    return False, f"agent {agent_id}: tokens not whitelisted"

            # 2. Token blocklist check
            if intent.asset_in in policy.blocked_tokens or intent.asset_out in policy.blocked_tokens:
                self._record_violation(agent_id, "token_blocklist",
                                       f"{intent.asset_in}/{intent.asset_out} is blocked",
                                       intent.id, "block")
                return False, f"agent {agent_id}: token is blocked"

            # 3. Trade size check
            trade_usd = intent.amount_in  # Approximate USD value
            if trade_usd > policy.max_trade_usd:
                self._record_violation(agent_id, "max_trade_size",
                                       f"${trade_usd:.0f} > max ${policy.max_trade_usd:.0f}",
                                       intent.id, "block")
                return False, f"agent {agent_id}: trade ${trade_usd:.0f} exceeds max ${policy.max_trade_usd:.0f}"

            # 4. Daily cap check
            daily = self._daily_spend.get(agent_id, 0.0)
            if daily + trade_usd > policy.daily_cap_usd:
                self._record_violation(agent_id, "daily_cap",
                                       f"daily ${daily + trade_usd:.0f} > cap ${policy.daily_cap_usd:.0f}",
                                       intent.id, "block")
                return False, f"agent {agent_id}: daily cap exceeded (${daily:.0f} + ${trade_usd:.0f} > ${policy.daily_cap_usd:.0f})"

            # 5. Hourly rate limit
            now = time.time()
            hourly = self._hourly_trades.get(agent_id, [])
            hourly = [t for t in hourly if now - t < 3600]
            self._hourly_trades[agent_id] = hourly
            if len(hourly) >= policy.max_trades_per_hour:
                self._record_violation(agent_id, "hourly_rate",
                                       f"{len(hourly)} trades in last hour >= {policy.max_trades_per_hour}",
                                       intent.id, "block")
                return False, f"agent {agent_id}: hourly rate limit ({policy.max_trades_per_hour}/hr)"

            # 6. Daily trade count
            daily_count = self._daily_trades.get(agent_id, 0)
            if daily_count >= policy.max_trades_per_day:
                self._record_violation(agent_id, "daily_trade_count",
                                       f"{daily_count} trades today >= {policy.max_trades_per_day}",
                                       intent.id, "block")
                return False, f"agent {agent_id}: daily trade limit ({policy.max_trades_per_day}/day)"

        return True, "ok"

    async def _on_execution(self, report):
        """Track actual spending from execution reports."""
        if not hasattr(report, "status") or report.status != "filled":
            return
        trade_usd = getattr(report, "quantity", 0) * (getattr(report, "fill_price", 0) or 0)
        if trade_usd <= 0:
            return

        # Attribute spend to the originating agent(s) via intent_id lookup
        intent_id = getattr(report, "intent_id", "")
        agent_ids = self._intent_agents.get(intent_id, [])
        if not agent_ids:
            log.debug("Policy engine: no agent attribution for intent %s", intent_id)
            return

        # Split spend evenly across contributing agents
        per_agent_usd = trade_usd / len(agent_ids)
        now = time.time()
        for agent_id in agent_ids:
            self._daily_spend[agent_id] = self._daily_spend.get(agent_id, 0) + per_agent_usd
            self._daily_trades[agent_id] = self._daily_trades.get(agent_id, 0) + 1
            self._hourly_trades.setdefault(agent_id, []).append(now)

    def _record_violation(self, agent_id: str, rule: str, detail: str,
                          intent_id: str, severity: str):
        v = PolicyViolation(
            agent_id=agent_id, rule=rule, detail=detail,
            trade_intent_id=intent_id, severity=severity,
        )
        self._violations.append(v)
        if len(self._violations) > 1000:
            self._violations = self._violations[-500:]
        log.warning("POLICY VIOLATION [%s] %s: %s — %s", severity, agent_id, rule, detail)

    def _maybe_reset_daily(self):
        """Reset daily counters at midnight."""
        today = _day_start()
        if today > self._day_start:
            self._day_start = today
            self._daily_spend.clear()
            self._daily_trades.clear()

    def summary(self) -> dict:
        return {
            "total_policies": len(self.policies),
            "violations_today": len([
                v for v in self._violations
                if v.timestamp > self._day_start
            ]),
            "total_violations": len(self._violations),
            "daily_spend": {
                aid: round(amt, 2)
                for aid, amt in self._daily_spend.items()
                if amt > 0
            },
            "policies": {
                aid: {
                    "max_trade": p.max_trade_usd,
                    "daily_cap": p.daily_cap_usd,
                    "auto_approve": p.auto_approve_usd,
                    "trades_today": self._daily_trades.get(aid, 0),
                    "spent_today": round(self._daily_spend.get(aid, 0), 2),
                }
                for aid, p in self.policies.items()
            },
        }


def _day_start() -> float:
    """Return timestamp of start of current UTC day."""
    import calendar, datetime
    now = datetime.datetime.utcnow()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return calendar.timegm(midnight.timetuple())


def policy_check(engine: AgentPolicyEngine):
    """Risk check function for the RiskAgent pipeline."""
    def check(intent: TradeIntent) -> RiskVerdict:
        approved, reason = engine.check_intent(intent)
        return RiskVerdict(
            intent_id=intent.id,
            agent_id="policy",
            approve=approved,
            reason=reason,
        )
    return check
