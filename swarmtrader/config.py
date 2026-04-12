"""Centralized configuration management.

Phase 4: All trading parameters in one place instead of scattered hardcoded
defaults. Supports loading from environment variables and a JSON config file.

Usage:
    from swarmtrader.config import TradingConfig
    cfg = TradingConfig.from_env()           # env vars + defaults
    cfg = TradingConfig.from_file("cfg.json") # JSON file
"""
from __future__ import annotations
import json, logging, os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("swarm.config")


@dataclass
class RiskConfig:
    """Risk management parameters."""
    max_daily_loss_usd: float = 100.0
    max_intraday_drawdown_usd: float = 150.0
    max_trades_per_hour: int = 20
    max_notional_per_hour: float = 50_000.0
    max_concentration_pct: float = 0.30      # max 30% in one asset
    max_gross_exposure_pct: float = 0.80     # max 80% of equity deployed
    min_order_usd: float = 10.0
    max_order_usd: float = 5_000.0
    max_positions: int = 5
    max_exposure_per_asset: float = 2_000.0
    max_total_exposure: float = 5_000.0
    hard_stop_pct: float = 0.05              # 5% hard stop
    trail_stop_pct: float = 0.03             # 3% trailing stop
    max_hold_seconds: float = 3600.0         # 1 hour max hold


@dataclass
class KrakenAPIConfig:
    """Kraken API connection configuration."""
    api_key: str = ""
    api_secret: str = ""
    tier: str = "starter"                    # starter | intermediate | pro
    use_rest_api: bool = False               # False = CLI primary, True = REST primary
    base_url: str = "https://api.kraken.com"


@dataclass
class ExecutionConfig:
    """Execution and exchange parameters."""
    mode: str = "mock"                       # mock, paper, live
    taker_fee_rate: float = 0.0026           # 0.26% Kraken taker
    maker_fee_rate: float = 0.0016           # 0.16% Kraken maker
    slippage_bps_per_1k: float = 5.0         # 5 bps per $1k notional
    order_ttl_seconds: float = 30.0
    max_retries: int = 3
    cli_timeout_seconds: float = 15.0
    poll_interval: float = 2.0               # REST ticker poll
    ws_stale_threshold: float = 10.0         # WS no-data warning
    default_order_type: str = "market"       # market | limit


@dataclass
class CircuitBreakerConfig:
    """Circuit breaker parameters."""
    max_consecutive_losses: int = 5
    max_drawdown_usd: float = 200.0
    vol_halt_threshold: float = 0.05         # 5% single-tick move
    vol_spike_count: int = 2                 # require sustained spikes
    cooldown_seconds: float = 300.0


@dataclass
class StrategyConfig:
    """Strategy and signal parameters."""
    base_size_usd: float = 1_000.0
    kelly_fraction: float = 0.25             # quarter-Kelly
    cooldown_seconds: float = 2.0
    rsi_period: int = 7
    rsi_overbought: float = 75.0
    rsi_oversold: float = 25.0
    macd_fast: int = 8
    macd_slow: int = 21
    macd_signal: int = 5
    bollinger_period: int = 20
    bollinger_std: float = 2.5


@dataclass
class DashboardConfig:
    """Web dashboard parameters."""
    host: str = "0.0.0.0"
    port: int = 8080
    token: str = ""                          # empty = auto-generate


@dataclass
class TradingConfig:
    """Top-level configuration container."""
    starting_capital: float = 10_000.0
    pairs: list[str] = field(default_factory=lambda: ["ETHUSD"])
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    kraken: KrakenAPIConfig = field(default_factory=KrakenAPIConfig)

    @classmethod
    def from_env(cls) -> TradingConfig:
        """Load config from environment variables with sensible defaults.

        Env var naming: SWARM_<SECTION>_<PARAM> (e.g., SWARM_RISK_MAX_DAILY_LOSS_USD)
        """
        cfg = cls()

        # Top-level
        cfg.starting_capital = float(os.getenv("SWARM_CAPITAL", cfg.starting_capital))
        pairs_str = os.getenv("SWARM_PAIRS", "")
        if pairs_str:
            cfg.pairs = [p.strip() for p in pairs_str.split(",")]

        # Risk
        cfg.risk.max_daily_loss_usd = float(
            os.getenv("SWARM_RISK_MAX_DAILY_LOSS", cfg.risk.max_daily_loss_usd))
        cfg.risk.max_trades_per_hour = int(
            os.getenv("SWARM_RISK_MAX_TRADES", cfg.risk.max_trades_per_hour))
        cfg.risk.max_concentration_pct = float(
            os.getenv("SWARM_RISK_MAX_CONCENTRATION", cfg.risk.max_concentration_pct))
        cfg.risk.max_gross_exposure_pct = float(
            os.getenv("SWARM_RISK_MAX_GROSS_EXPOSURE", cfg.risk.max_gross_exposure_pct))
        cfg.risk.max_order_usd = float(
            os.getenv("SWARM_RISK_MAX_ORDER", cfg.risk.max_order_usd))
        cfg.risk.max_positions = int(
            os.getenv("SWARM_RISK_MAX_POSITIONS", cfg.risk.max_positions))

        # Execution
        cfg.execution.mode = os.getenv("SWARM_MODE", cfg.execution.mode)
        cfg.execution.max_retries = int(
            os.getenv("SWARM_EXEC_MAX_RETRIES", cfg.execution.max_retries))
        cfg.execution.default_order_type = os.getenv(
            "SWARM_DEFAULT_ORDER_TYPE", cfg.execution.default_order_type)

        # Kraken API
        cfg.kraken.api_key = os.getenv("KRAKEN_API_KEY", cfg.kraken.api_key)
        cfg.kraken.api_secret = os.getenv("KRAKEN_PRIVATE_KEY", cfg.kraken.api_secret)
        cfg.kraken.tier = os.getenv("KRAKEN_TIER", cfg.kraken.tier)
        cfg.kraken.use_rest_api = os.getenv(
            "KRAKEN_USE_REST_API", "0").lower() in ("1", "true", "yes")

        # Circuit breaker
        cfg.circuit_breaker.max_drawdown_usd = float(
            os.getenv("SWARM_CB_MAX_DRAWDOWN", cfg.circuit_breaker.max_drawdown_usd))
        cfg.circuit_breaker.cooldown_seconds = float(
            os.getenv("SWARM_CB_COOLDOWN", cfg.circuit_breaker.cooldown_seconds))

        # Dashboard
        cfg.dashboard.port = int(os.getenv("SWARM_DASHBOARD_PORT", cfg.dashboard.port))
        cfg.dashboard.token = os.getenv("SWARM_DASHBOARD_TOKEN", cfg.dashboard.token)

        return cfg

    @classmethod
    def from_file(cls, path: str | Path) -> TradingConfig:
        """Load config from a JSON file."""
        p = Path(path)
        if not p.exists():
            log.warning("Config file %s not found, using defaults", p)
            return cls()

        with open(p) as f:
            raw = json.load(f)

        cfg = cls()
        cfg.starting_capital = raw.get("starting_capital", cfg.starting_capital)
        cfg.pairs = raw.get("pairs", cfg.pairs)

        for section_name, section_cls in [
            ("risk", cfg.risk),
            ("execution", cfg.execution),
            ("circuit_breaker", cfg.circuit_breaker),
            ("strategy", cfg.strategy),
            ("dashboard", cfg.dashboard),
        ]:
            section_data = raw.get(section_name, {})
            for key, val in section_data.items():
                if hasattr(section_cls, key):
                    setattr(section_cls, key, type(getattr(section_cls, key))(val))

        log.info("Loaded config from %s", p)
        return cfg

    def to_dict(self) -> dict:
        """Serialize config to dict (for logging/persistence)."""
        import dataclasses
        return dataclasses.asdict(self)

    def save(self, path: str | Path):
        """Save config to JSON file."""
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        log.info("Config saved to %s", path)
