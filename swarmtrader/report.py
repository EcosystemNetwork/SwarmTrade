"""
PnL report generator — reads SQLite audit trail and produces performance metrics.

Generates:
  - Summary stats (total PnL, Sharpe, Sortino, max drawdown, win rate, profit factor)
  - Equity curve data
  - Per-trade log with attribution
  - JSON export for dashboard consumption
  - Terminal-formatted report

Usage:
  python -m swarmtrader.report [--db swarm.db] [--json] [--html report.html]
"""
from __future__ import annotations
import argparse, json, math, sqlite3, time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TradeRecord:
    ts: float
    intent_id: str
    status: str
    tx_hash: str | None
    fill_price: float | None
    slippage: float | None
    pnl: float
    note: str
    # From intents table
    asset_in: str = ""
    asset_out: str = ""
    amount_in: float = 0.0


@dataclass
class PerformanceReport:
    """Complete performance metrics for a trading session."""
    trades: list[TradeRecord] = field(default_factory=list)
    start_ts: float = 0.0
    end_ts: float = 0.0

    # ── Core Metrics ──────────────────────────────────────────
    @property
    def filled_trades(self) -> list[TradeRecord]:
        return [t for t in self.trades if t.status == "filled"]

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.filled_trades)

    @property
    def num_trades(self) -> int:
        return len(self.filled_trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.filled_trades if t.pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.filled_trades if t.pnl <= 0)

    @property
    def win_rate(self) -> float:
        return self.wins / self.num_trades if self.num_trades else 0.0

    @property
    def avg_win(self) -> float:
        w = [t.pnl for t in self.filled_trades if t.pnl > 0]
        return sum(w) / len(w) if w else 0.0

    @property
    def avg_loss(self) -> float:
        l = [t.pnl for t in self.filled_trades if t.pnl <= 0]
        return sum(l) / len(l) if l else 0.0

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl for t in self.filled_trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.filled_trades if t.pnl < 0))
        return gross_profit / gross_loss if gross_loss > 0 else float("inf")

    @property
    def equity_curve(self) -> list[tuple[float, float]]:
        """Returns [(timestamp, cumulative_pnl), ...]"""
        curve = []
        cum = 0.0
        for t in sorted(self.filled_trades, key=lambda x: x.ts):
            cum += t.pnl
            curve.append((t.ts, cum))
        return curve

    @property
    def max_drawdown(self) -> float:
        """Maximum drawdown as a dollar amount."""
        curve = self.equity_curve
        if not curve:
            return 0.0
        peak = curve[0][1]
        max_dd = 0.0
        for _, val in curve:
            peak = max(peak, val)
            dd = peak - val
            max_dd = max(max_dd, dd)
        return max_dd

    @property
    def max_drawdown_pct(self) -> float:
        """Max drawdown as percentage of peak equity."""
        curve = self.equity_curve
        if not curve:
            return 0.0
        peak = curve[0][1]
        max_dd_pct = 0.0
        for _, val in curve:
            peak = max(peak, val)
            if peak > 0:
                dd_pct = (peak - val) / peak
                max_dd_pct = max(max_dd_pct, dd_pct)
        return max_dd_pct

    @property
    def sharpe_ratio(self) -> float:
        """Annualized Sharpe ratio (assuming 0 risk-free rate)."""
        returns = [t.pnl for t in self.filled_trades]
        if len(returns) < 2:
            return 0.0
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / len(returns)
        std = max(1e-9, var ** 0.5)
        # Annualize: assume ~252 trading days
        trades_per_day = self._trades_per_day()
        return (mean_r / std) * math.sqrt(trades_per_day * 252)

    @property
    def sortino_ratio(self) -> float:
        """Sortino ratio — penalizes only downside deviation."""
        returns = [t.pnl for t in self.filled_trades]
        if len(returns) < 2:
            return 0.0
        mean_r = sum(returns) / len(returns)
        downside = [r for r in returns if r < 0]
        if not downside:
            return float("inf")
        downside_var = sum(r ** 2 for r in downside) / len(downside)
        downside_std = max(1e-9, downside_var ** 0.5)
        trades_per_day = self._trades_per_day()
        return (mean_r / downside_std) * math.sqrt(trades_per_day * 252)

    @property
    def calmar_ratio(self) -> float:
        """Annualized return / max drawdown."""
        if self.max_drawdown == 0:
            return float("inf") if self.total_pnl > 0 else 0.0
        duration_days = max(1, (self.end_ts - self.start_ts) / 86400)
        annual_pnl = self.total_pnl * (365 / duration_days)
        return annual_pnl / self.max_drawdown

    @property
    def longest_win_streak(self) -> int:
        streak = max_streak = 0
        for t in sorted(self.filled_trades, key=lambda x: x.ts):
            if t.pnl > 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        return max_streak

    @property
    def longest_loss_streak(self) -> int:
        streak = max_streak = 0
        for t in sorted(self.filled_trades, key=lambda x: x.ts):
            if t.pnl <= 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        return max_streak

    @property
    def rejected_count(self) -> int:
        return sum(1 for t in self.trades if t.status == "rejected")

    @property
    def expired_count(self) -> int:
        return sum(1 for t in self.trades if t.status == "expired")

    def _trades_per_day(self) -> float:
        if self.start_ts == 0 or self.end_ts == 0:
            return 1.0
        duration_days = max(1 / 1440, (self.end_ts - self.start_ts) / 86400)
        return self.num_trades / duration_days

    # ── Output Formats ────────────────────────────────────────

    def to_dict(self) -> dict:
        """JSON-serializable summary."""
        return {
            "generated_at": time.time(),
            "duration_s": self.end_ts - self.start_ts,
            "metrics": {
                "total_pnl": round(self.total_pnl, 4),
                "num_trades": self.num_trades,
                "wins": self.wins,
                "losses": self.losses,
                "win_rate": round(self.win_rate, 4),
                "avg_win": round(self.avg_win, 4),
                "avg_loss": round(self.avg_loss, 4),
                "profit_factor": round(self.profit_factor, 4) if self.profit_factor != float("inf") else "inf",
                "sharpe_ratio": round(self.sharpe_ratio, 4),
                "sortino_ratio": round(self.sortino_ratio, 4) if self.sortino_ratio != float("inf") else "inf",
                "calmar_ratio": round(self.calmar_ratio, 4) if self.calmar_ratio != float("inf") else "inf",
                "max_drawdown_usd": round(self.max_drawdown, 4),
                "max_drawdown_pct": round(self.max_drawdown_pct, 4),
                "longest_win_streak": self.longest_win_streak,
                "longest_loss_streak": self.longest_loss_streak,
                "rejected": self.rejected_count,
                "expired": self.expired_count,
            },
            "equity_curve": [
                {"ts": ts, "pnl": round(pnl, 4)} for ts, pnl in self.equity_curve
            ],
            "trades": [
                {
                    "ts": t.ts, "intent_id": t.intent_id, "status": t.status,
                    "asset_in": t.asset_in, "asset_out": t.asset_out,
                    "amount_in": round(t.amount_in, 2),
                    "fill_price": t.fill_price, "pnl": round(t.pnl, 4),
                    "note": t.note,
                }
                for t in sorted(self.filled_trades, key=lambda x: x.ts)
            ],
        }

    def summary(self) -> str:
        """Terminal-formatted summary report."""
        dur = self.end_ts - self.start_ts
        dur_str = f"{int(dur // 3600)}h {int((dur % 3600) // 60)}m {int(dur % 60)}s" if dur > 0 else "N/A"
        pf = f"{self.profit_factor:.2f}" if self.profit_factor != float("inf") else "INF"
        sharpe = f"{self.sharpe_ratio:.3f}"
        sortino = f"{self.sortino_ratio:.3f}" if self.sortino_ratio != float("inf") else "INF"
        calmar = f"{self.calmar_ratio:.3f}" if self.calmar_ratio != float("inf") else "INF"

        lines = [
            "",
            "=" * 64,
            "  SWARMTRADER PERFORMANCE REPORT",
            "=" * 64,
            f"  Duration          : {dur_str}",
            f"  Total Trades      : {self.num_trades}  (rejected: {self.rejected_count}, expired: {self.expired_count})",
            f"  Wins / Losses     : {self.wins} / {self.losses}",
            f"  Win Rate          : {self.win_rate:.1%}",
            "-" * 64,
            f"  Total PnL         : ${self.total_pnl:+,.4f}",
            f"  Avg Win           : ${self.avg_win:+,.4f}",
            f"  Avg Loss          : ${self.avg_loss:+,.4f}",
            f"  Profit Factor     : {pf}",
            "-" * 64,
            f"  Sharpe Ratio      : {sharpe}",
            f"  Sortino Ratio     : {sortino}",
            f"  Calmar Ratio      : {calmar}",
            f"  Max Drawdown      : ${self.max_drawdown:,.4f}  ({self.max_drawdown_pct:.2%})",
            "-" * 64,
            f"  Win Streak (best) : {self.longest_win_streak}",
            f"  Loss Streak (worst): {self.longest_loss_streak}",
            "=" * 64,
            "",
        ]
        return "\n".join(lines)


def load_from_db(db_path: Path) -> PerformanceReport:
    """Load trades from SQLite audit database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Load reports
    reports = conn.execute("SELECT * FROM reports ORDER BY ts ASC").fetchall()

    # Load intents for asset info
    intents = {}
    try:
        for row in conn.execute("SELECT * FROM intents ORDER BY ts ASC").fetchall():
            intents[row["id"]] = dict(row)
    except Exception:
        pass

    conn.close()

    trades = []
    for r in reports:
        rd = dict(r)
        intent = intents.get(rd.get("intent_id", ""), {})
        trades.append(TradeRecord(
            ts=rd.get("ts", 0.0),
            intent_id=rd.get("intent_id", ""),
            status=rd.get("status", ""),
            tx_hash=rd.get("tx", None),
            fill_price=rd.get("fill_price", None),
            slippage=rd.get("slippage", None),
            pnl=rd.get("pnl", 0.0) or 0.0,
            note=rd.get("note", "") or "",
            asset_in=intent.get("asset_in", rd.get("asset", "")),
            asset_out=intent.get("asset_out", ""),
            amount_in=intent.get("amount_in", rd.get("quantity", 0.0) or 0.0),
        ))

    report = PerformanceReport(trades=trades)
    if trades:
        report.start_ts = trades[0].ts
        report.end_ts = trades[-1].ts

    return report


def generate_html_report(report: PerformanceReport, output_path: Path):
    """Generate a standalone HTML performance report."""
    data = report.to_dict()
    m = data["metrics"]
    equity = json.dumps(data["equity_curve"])
    trades_json = json.dumps(data["trades"])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>SwarmTrader Performance Report</title>
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Inter:wght@400;600;700&display=swap" rel="stylesheet"/>
<script>
tailwind.config = {{
  theme: {{
    extend: {{
      colors: {{
        void: "#0a0e17", surface: "#0f131c", "s-low": "#181b25",
        neon: "#00ff88", electric: "#00d4ff", signal: "#ff3366",
      }},
      fontFamily: {{ mono: ["JetBrains Mono", "monospace"], body: ["Inter", "sans-serif"] }},
    }},
  }},
}}
</script>
<style>body {{ background: #0a0e17; color: #dfe2ef; }}</style>
</head>
<body class="font-body p-8">
<div class="max-w-6xl mx-auto">
  <!-- Header -->
  <div class="flex items-center justify-between mb-8">
    <div>
      <h1 class="text-3xl font-bold font-mono text-neon">SWARMTRADER</h1>
      <p class="text-gray-400 text-sm mt-1">Performance Report</p>
    </div>
    <div class="text-right text-sm text-gray-500 font-mono">
      <div>Generated: {time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}</div>
      <div>Duration: {int((report.end_ts - report.start_ts) // 60)}m</div>
    </div>
  </div>

  <!-- Metric Cards -->
  <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
    <div class="bg-surface border border-neon/20 p-4 rounded">
      <div class="text-[10px] uppercase text-gray-500 font-mono">Total PnL</div>
      <div class="text-2xl font-bold font-mono {'text-neon' if report.total_pnl >= 0 else 'text-signal'}">${'{:+,.4f}'.format(report.total_pnl)}</div>
    </div>
    <div class="bg-surface border border-electric/20 p-4 rounded">
      <div class="text-[10px] uppercase text-gray-500 font-mono">Sharpe Ratio</div>
      <div class="text-2xl font-bold font-mono text-electric">{m['sharpe_ratio']}</div>
    </div>
    <div class="bg-surface border border-electric/20 p-4 rounded">
      <div class="text-[10px] uppercase text-gray-500 font-mono">Sortino Ratio</div>
      <div class="text-2xl font-bold font-mono text-electric">{m['sortino_ratio']}</div>
    </div>
    <div class="bg-surface border border-signal/20 p-4 rounded">
      <div class="text-[10px] uppercase text-gray-500 font-mono">Max Drawdown</div>
      <div class="text-2xl font-bold font-mono text-signal">${'{:,.4f}'.format(report.max_drawdown)}</div>
    </div>
  </div>

  <div class="grid grid-cols-2 md:grid-cols-6 gap-4 mb-8">
    <div class="bg-s-low p-3 rounded text-center">
      <div class="text-[9px] uppercase text-gray-500 font-mono">Trades</div>
      <div class="text-lg font-bold font-mono">{m['num_trades']}</div>
    </div>
    <div class="bg-s-low p-3 rounded text-center">
      <div class="text-[9px] uppercase text-gray-500 font-mono">Win Rate</div>
      <div class="text-lg font-bold font-mono text-neon">{'{:.1%}'.format(report.win_rate)}</div>
    </div>
    <div class="bg-s-low p-3 rounded text-center">
      <div class="text-[9px] uppercase text-gray-500 font-mono">Profit Factor</div>
      <div class="text-lg font-bold font-mono">{m['profit_factor']}</div>
    </div>
    <div class="bg-s-low p-3 rounded text-center">
      <div class="text-[9px] uppercase text-gray-500 font-mono">Avg Win</div>
      <div class="text-lg font-bold font-mono text-neon">${'{:+,.4f}'.format(report.avg_win)}</div>
    </div>
    <div class="bg-s-low p-3 rounded text-center">
      <div class="text-[9px] uppercase text-gray-500 font-mono">Avg Loss</div>
      <div class="text-lg font-bold font-mono text-signal">${'{:,.4f}'.format(report.avg_loss)}</div>
    </div>
    <div class="bg-s-low p-3 rounded text-center">
      <div class="text-[9px] uppercase text-gray-500 font-mono">Calmar</div>
      <div class="text-lg font-bold font-mono">{m['calmar_ratio']}</div>
    </div>
  </div>

  <!-- Equity Curve -->
  <div class="bg-surface border border-white/10 rounded p-4 mb-8">
    <h2 class="font-mono text-sm font-bold text-electric uppercase mb-4">Equity Curve</h2>
    <canvas id="equity-chart" class="w-full" height="300"></canvas>
  </div>

  <!-- PnL Distribution -->
  <div class="bg-surface border border-white/10 rounded p-4 mb-8">
    <h2 class="font-mono text-sm font-bold text-electric uppercase mb-4">PnL Distribution</h2>
    <canvas id="pnl-dist" class="w-full" height="200"></canvas>
  </div>

  <!-- Trade Log -->
  <div class="bg-surface border border-white/10 rounded p-4 mb-8">
    <h2 class="font-mono text-sm font-bold text-electric uppercase mb-4">Trade Log</h2>
    <div class="overflow-x-auto">
      <table class="w-full text-sm font-mono text-left">
        <thead class="text-[10px] uppercase text-gray-500 border-b border-white/10">
          <tr>
            <th class="py-2 px-3">Time</th>
            <th class="py-2 px-3">ID</th>
            <th class="py-2 px-3">Route</th>
            <th class="py-2 px-3 text-right">Amount</th>
            <th class="py-2 px-3 text-right">Fill Price</th>
            <th class="py-2 px-3 text-right">PnL</th>
            <th class="py-2 px-3">Note</th>
          </tr>
        </thead>
        <tbody id="trade-log" class="divide-y divide-white/5"></tbody>
      </table>
    </div>
  </div>

  <!-- Risk Guardrails Summary -->
  <div class="bg-surface border border-amber-500/20 rounded p-4 mb-8">
    <h2 class="font-mono text-sm font-bold text-amber-400 uppercase mb-4">Risk Guardrails Active</h2>
    <div class="grid grid-cols-2 md:grid-cols-3 gap-3 text-sm">
      <div class="flex items-center gap-2">
        <span class="w-2 h-2 rounded-full bg-neon"></span>
        <span>Multi-agent risk quorum (unanimous)</span>
      </div>
      <div class="flex items-center gap-2">
        <span class="w-2 h-2 rounded-full bg-neon"></span>
        <span>Circuit breaker (5 consecutive losses)</span>
      </div>
      <div class="flex items-center gap-2">
        <span class="w-2 h-2 rounded-full bg-neon"></span>
        <span>Rate limiter (20/hr max)</span>
      </div>
      <div class="flex items-center gap-2">
        <span class="w-2 h-2 rounded-full bg-neon"></span>
        <span>Volatility spike halt (5% threshold)</span>
      </div>
      <div class="flex items-center gap-2">
        <span class="w-2 h-2 rounded-full bg-neon"></span>
        <span>Position flattener (emergency close)</span>
      </div>
      <div class="flex items-center gap-2">
        <span class="w-2 h-2 rounded-full bg-neon"></span>
        <span>Dead man's switch (auto cancel-all)</span>
      </div>
      <div class="flex items-center gap-2">
        <span class="w-2 h-2 rounded-full bg-neon"></span>
        <span>Daily drawdown limit (auto-reset UTC)</span>
      </div>
      <div class="flex items-center gap-2">
        <span class="w-2 h-2 rounded-full bg-neon"></span>
        <span>Kill switch file (instant halt)</span>
      </div>
      <div class="flex items-center gap-2">
        <span class="w-2 h-2 rounded-full bg-signal"></span>
        <span>Intents rejected: {report.rejected_count}</span>
      </div>
    </div>
  </div>

  <div class="text-center text-gray-600 text-xs font-mono py-4">
    SwarmTrader // AI Trading Agents Hackathon 2026 // Multi-Agent Autonomous Trading Platform
  </div>
</div>

<script>
const equityData = {equity};
const tradesData = {trades_json};

// ── Equity Curve ─────────────────────────────────────────────
function drawEquity() {{
  const canvas = document.getElementById("equity-chart");
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = 300 * dpr;
  ctx.scale(dpr, dpr);
  const w = rect.width, h = 300;

  if (equityData.length < 2) {{
    ctx.fillStyle = "#6b7280";
    ctx.font = "14px JetBrains Mono";
    ctx.textAlign = "center";
    ctx.fillText("Not enough data", w / 2, h / 2);
    return;
  }}

  const pnls = equityData.map(d => d.pnl);
  const minP = Math.min(0, ...pnls);
  const maxP = Math.max(0, ...pnls);
  const range = maxP - minP || 1;
  const pad = 30;

  // Zero line
  const zeroY = h - pad - ((0 - minP) / range) * (h - pad * 2);
  ctx.strokeStyle = "rgba(255,255,255,0.1)";
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(pad, zeroY); ctx.lineTo(w - pad, zeroY); ctx.stroke();
  ctx.setLineDash([]);

  // Fill
  ctx.beginPath();
  ctx.moveTo(pad, zeroY);
  equityData.forEach((d, i) => {{
    const x = pad + (i / (equityData.length - 1)) * (w - pad * 2);
    const y = h - pad - ((d.pnl - minP) / range) * (h - pad * 2);
    ctx.lineTo(x, y);
  }});
  ctx.lineTo(w - pad, zeroY);
  ctx.closePath();
  const lastPnl = pnls[pnls.length - 1];
  const gradColor = lastPnl >= 0 ? "rgba(0,255,136," : "rgba(255,51,102,";
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, gradColor + "0.2)");
  grad.addColorStop(1, gradColor + "0.02)");
  ctx.fillStyle = grad;
  ctx.fill();

  // Line
  ctx.beginPath();
  equityData.forEach((d, i) => {{
    const x = pad + (i / (equityData.length - 1)) * (w - pad * 2);
    const y = h - pad - ((d.pnl - minP) / range) * (h - pad * 2);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }});
  ctx.strokeStyle = lastPnl >= 0 ? "#00ff88" : "#ff3366";
  ctx.lineWidth = 2;
  ctx.stroke();

  // Labels
  ctx.fillStyle = "rgba(255,255,255,0.3)";
  ctx.font = "10px JetBrains Mono";
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {{
    const v = minP + (range * i / 4);
    const y = h - pad - ((v - minP) / range) * (h - pad * 2);
    ctx.fillText(`${{v >= 0 ? "+" : ""}}${{v.toFixed(4)}}`, w - 4, y + 3);
  }}
}}

// ── PnL Distribution ─────────────────────────────────────────
function drawDist() {{
  const canvas = document.getElementById("pnl-dist");
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = 200 * dpr;
  ctx.scale(dpr, dpr);
  const w = rect.width, h = 200;

  const pnls = tradesData.map(t => t.pnl);
  if (pnls.length < 1) return;

  const min = Math.min(...pnls);
  const max = Math.max(...pnls);
  const bins = 20;
  const binW = (max - min) / bins || 0.001;
  const counts = new Array(bins).fill(0);
  pnls.forEach(p => {{
    const idx = Math.min(bins - 1, Math.floor((p - min) / binW));
    counts[idx]++;
  }});
  const maxCount = Math.max(...counts);
  const pad = 30;
  const barW = (w - pad * 2) / bins;

  counts.forEach((c, i) => {{
    const x = pad + i * barW;
    const barH = (c / maxCount) * (h - pad * 2);
    const binCenter = min + (i + 0.5) * binW;
    ctx.fillStyle = binCenter >= 0 ? "rgba(0,255,136,0.5)" : "rgba(255,51,102,0.5)";
    ctx.fillRect(x + 1, h - pad - barH, barW - 2, barH);
  }});

  // Zero line
  if (min < 0 && max > 0) {{
    const zeroX = pad + ((0 - min) / (max - min)) * (w - pad * 2);
    ctx.strokeStyle = "rgba(255,255,255,0.3)";
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(zeroX, pad); ctx.lineTo(zeroX, h - pad); ctx.stroke();
    ctx.setLineDash([]);
  }}
}}

// ── Trade Log ────────────────────────────────────────────────
function renderTradeLog() {{
  const body = document.getElementById("trade-log");
  let html = "";
  for (const t of tradesData) {{
    const time = new Date(t.ts * 1000).toISOString().slice(11, 19);
    const pnlColor = t.pnl > 0 ? "text-green-400" : t.pnl < 0 ? "text-red-400" : "text-gray-500";
    html += `<tr class="hover:bg-white/5">
      <td class="py-2 px-3 text-gray-400">${{time}}</td>
      <td class="py-2 px-3 text-blue-400">${{t.intent_id}}</td>
      <td class="py-2 px-3">${{t.asset_in}} → ${{t.asset_out}}</td>
      <td class="py-2 px-3 text-right">${{t.amount_in.toFixed(2)}}</td>
      <td class="py-2 px-3 text-right">${{t.fill_price ? "$" + t.fill_price.toFixed(2) : "--"}}</td>
      <td class="py-2 px-3 text-right ${{pnlColor}} font-bold">${{t.pnl >= 0 ? "+" : ""}}${{t.pnl.toFixed(4)}}</td>
      <td class="py-2 px-3 text-gray-600">${{t.note}}</td>
    </tr>`;
  }}
  body.innerHTML = html;
}}

drawEquity();
drawDist();
renderTradeLog();
window.addEventListener("resize", () => {{ drawEquity(); drawDist(); }});
</script>
</body>
</html>"""
    output_path.write_text(html)


def main():
    p = argparse.ArgumentParser(description="SwarmTrader Performance Report")
    p.add_argument("--db", default="swarm.db", help="SQLite database path")
    p.add_argument("--json", action="store_true", help="Output JSON to stdout")
    p.add_argument("--html", type=str, default=None, help="Generate HTML report to file")
    args = p.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"Database not found: {db}")
        return

    report = load_from_db(db)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    elif args.html:
        out = Path(args.html)
        generate_html_report(report, out)
        print(f"HTML report written to: {out}")
    else:
        print(report.summary())


if __name__ == "__main__":
    main()
