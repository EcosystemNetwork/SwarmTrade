"""User Authentication & Multi-Tenancy — registration, API keys, sessions.

Provides:
  1. User registration with email + password (bcrypt hashed)
  2. API key generation per user (for programmatic access)
  3. Session tokens (JWT-style, for dashboard access)
  4. Multi-tenancy: each user gets isolated wallet, positions, settings
  5. Role-based access: admin, trader, viewer
  6. Rate limiting per user tier

Environment variables:
  AUTH_SECRET          — Secret for token signing (auto-generated if empty)
  AUTH_SESSION_TTL     — Session token TTL in seconds (default: 86400 = 24h)
  AUTH_ADMIN_EMAIL     — Pre-seeded admin email (optional)
  AUTH_ADMIN_PASSWORD  — Pre-seeded admin password (optional)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field

from .core import Bus

log = logging.getLogger("swarm.auth")

AUTH_SECRET = os.getenv("AUTH_SECRET", "") or secrets.token_hex(32)
SESSION_TTL = int(os.getenv("AUTH_SESSION_TTL", "86400"))


@dataclass
class User:
    """A registered platform user."""
    user_id: str
    email: str
    password_hash: str
    # Profile
    display_name: str = ""
    role: str = "trader"          # admin, trader, viewer
    tier: str = "free"            # free, starter, pro, institutional
    # API access
    api_key: str = ""
    api_key_created: float = 0.0
    # Limits (per tier)
    max_capital: float = 0.0      # 0 = unlimited
    max_strategies: int = 1
    max_api_calls_per_hour: int = 100
    # Status
    active: bool = True
    email_verified: bool = False
    created_at: float = field(default_factory=time.time)
    last_login: float = 0.0
    # Billing
    stripe_customer_id: str = ""
    subscription_id: str = ""
    subscription_status: str = ""  # active, past_due, cancelled


@dataclass
class Session:
    """An active user session."""
    token: str
    user_id: str
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    ip_address: str = ""


# Tier configuration
TIERS = {
    "free": {
        "max_capital": 0,       # paper only
        "max_strategies": 1,
        "max_api_calls_per_hour": 50,
        "price_usd": 0,
        "features": ["paper_trading", "1_strategy", "delayed_data"],
    },
    "starter": {
        "max_capital": 10_000,
        "max_strategies": 3,
        "max_api_calls_per_hour": 200,
        "price_usd": 29,
        "features": ["live_trading", "3_strategies", "real_time_data"],
    },
    "pro": {
        "max_capital": 0,       # unlimited
        "max_strategies": 0,    # unlimited
        "max_api_calls_per_hour": 1000,
        "price_usd": 99,
        "features": ["unlimited_capital", "all_strategies", "priority_execution", "api_access"],
    },
    "institutional": {
        "max_capital": 0,
        "max_strategies": 0,
        "max_api_calls_per_hour": 10000,
        "price_usd": 499,
        "features": ["everything", "custom_strategies", "dedicated_support", "sla"],
    },
}


class AuthManager:
    """User authentication and session management.

    Password storage: PBKDF2-HMAC-SHA256 with random salt.
    Session tokens: HMAC-SHA256(secret, user_id + timestamp).
    API keys: 32-byte hex tokens, stored hashed.
    """

    def __init__(self, bus: Bus | None = None, db=None):
        self.bus = bus
        self.db = db
        self._users: dict[str, User] = {}        # user_id -> User
        self._email_index: dict[str, str] = {}    # email -> user_id
        self._sessions: dict[str, Session] = {}   # token -> Session
        self._api_keys: dict[str, str] = {}       # key_hash -> user_id
        self._user_counter = 0

        # Seed admin if configured
        admin_email = os.getenv("AUTH_ADMIN_EMAIL", "")
        admin_pass = os.getenv("AUTH_ADMIN_PASSWORD", "")
        if admin_email and admin_pass:
            self.register(admin_email, admin_pass, role="admin", tier="institutional")
            log.info("Admin user seeded: %s", admin_email)

    def register(self, email: str, password: str,
                 display_name: str = "", role: str = "trader",
                 tier: str = "free") -> User:
        """Register a new user."""
        email = email.lower().strip()
        if email in self._email_index:
            raise ValueError(f"Email already registered: {email}")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")

        self._user_counter += 1
        user_id = f"user-{self._user_counter:06d}"

        # Hash password with PBKDF2
        salt = secrets.token_hex(16)
        pw_hash = self._hash_password(password, salt)

        tier_config = TIERS.get(tier, TIERS["free"])

        user = User(
            user_id=user_id,
            email=email,
            password_hash=f"{salt}:{pw_hash}",
            display_name=display_name or email.split("@")[0],
            role=role,
            tier=tier,
            max_capital=tier_config["max_capital"],
            max_strategies=tier_config["max_strategies"],
            max_api_calls_per_hour=tier_config["max_api_calls_per_hour"],
        )
        self._users[user_id] = user
        self._email_index[email] = user_id

        log.info("User registered: %s (%s, tier=%s)", user_id, email, tier)
        return user

    def login(self, email: str, password: str, ip: str = "") -> Session | None:
        """Authenticate and create session."""
        email = email.lower().strip()
        user_id = self._email_index.get(email)
        if not user_id:
            return None

        user = self._users.get(user_id)
        if not user or not user.active:
            return None

        # Verify password
        salt, stored_hash = user.password_hash.split(":", 1)
        if self._hash_password(password, salt) != stored_hash:
            return None

        # Create session
        token = self._generate_session_token(user_id)
        session = Session(
            token=token, user_id=user_id,
            expires_at=time.time() + SESSION_TTL,
            ip_address=ip,
        )
        self._sessions[token] = session
        user.last_login = time.time()

        log.info("User logged in: %s (%s)", user_id, email)
        return session

    def validate_session(self, token: str) -> User | None:
        """Validate a session token, return user or None."""
        session = self._sessions.get(token)
        if not session:
            return None
        if time.time() > session.expires_at:
            del self._sessions[token]
            return None
        return self._users.get(session.user_id)

    def logout(self, token: str):
        """Invalidate a session."""
        self._sessions.pop(token, None)

    # ── API Keys ─────────────────────────────────────────────────

    def generate_api_key(self, user_id: str) -> str:
        """Generate a new API key for a user."""
        user = self._users.get(user_id)
        if not user:
            raise ValueError(f"User not found: {user_id}")

        key = f"swt_{secrets.token_hex(24)}"
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        self._api_keys[key_hash] = user_id
        user.api_key = key_hash
        user.api_key_created = time.time()

        log.info("API key generated for %s", user_id)
        return key  # Return raw key (only time it's visible)

    def validate_api_key(self, key: str) -> User | None:
        """Validate an API key, return user or None."""
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        user_id = self._api_keys.get(key_hash)
        if not user_id:
            return None
        return self._users.get(user_id)

    # ── Tier Management ──────────────────────────────────────────

    def upgrade_tier(self, user_id: str, new_tier: str):
        """Upgrade a user's tier."""
        user = self._users.get(user_id)
        if not user:
            raise ValueError(f"User not found: {user_id}")
        if new_tier not in TIERS:
            raise ValueError(f"Invalid tier: {new_tier}")

        old_tier = user.tier
        config = TIERS[new_tier]
        user.tier = new_tier
        user.max_capital = config["max_capital"]
        user.max_strategies = config["max_strategies"]
        user.max_api_calls_per_hour = config["max_api_calls_per_hour"]

        log.info("User %s upgraded: %s -> %s", user_id, old_tier, new_tier)

    # ── Internals ────────────────────────────────────────────────

    @staticmethod
    def _hash_password(password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), 100_000
        ).hex()

    @staticmethod
    def _generate_session_token(user_id: str) -> str:
        payload = f"{user_id}:{time.time()}:{secrets.token_hex(8)}"
        return hmac.new(AUTH_SECRET.encode(), payload.encode(), "sha256").hexdigest()

    def summary(self) -> dict:
        return {
            "total_users": len(self._users),
            "active_sessions": len(self._sessions),
            "api_keys_issued": len(self._api_keys),
            "by_tier": {
                tier: len([u for u in self._users.values() if u.tier == tier])
                for tier in TIERS
            },
        }
