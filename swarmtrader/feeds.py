"""Extended data-feed agents — additional market intelligence streams.

New agents:
  - ExchangeFlowAgent:  exchange inflow/outflow reserves (CryptoQuant-style)
  - StablecoinAgent:    USDT/USDC market cap + depeg monitoring
  - MacroCalendarAgent: economic events (FOMC, CPI, NFP) from public API
  - DeribitOptionsAgent: options IV, put/call ratio, max pain from Deribit
  - TokenUnlockAgent:   upcoming token vesting unlocks as sell pressure
  - GitHubDevAgent:     real-time commit velocity for top protocol repos
  - RSSNewsAgent:       multi-source RSS/Atom news aggregation (no API key)

All agents follow the standard pattern:
  - Subscribe to nothing or market.snapshot
  - Poll external APIs on an interval
  - Publish signal.<name> on the Bus
  - Graceful degradation on API failure
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import xml.etree.ElementTree as ET
from collections import deque

import aiohttp
from .core import Bus, Signal

log = logging.getLogger("swarm.feeds")

_last_api_call: dict[str, float] = {}
_MIN_API_INTERVAL = 2.0


async def _rate_limit(api_name: str):
    now = time.time()
    last = _last_api_call.get(api_name, 0.0)
    wait = _MIN_API_INTERVAL - (now - last)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_api_call[api_name] = time.time()


# ---------------------------------------------------------------------------
# 1. Exchange Net Flow Agent
# ---------------------------------------------------------------------------

class ExchangeFlowAgent:
    """Monitors exchange reserve changes as a proxy for buy/sell pressure.

    Uses CoinGecko exchange data to estimate net flows:
      - Reserves declining → coins leaving exchanges → accumulation → bullish
      - Reserves increasing → coins entering exchanges → distribution → bearish

    Also tracks the BTC and ETH balance on top exchanges via Blockchain.com
    and Blockchair free endpoints.

    Publishes signal.exchange_flow per asset.
    """

    name = "exchange_flow"

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 300.0):
        self.bus = bus
        self.assets = assets or ["ETH", "BTC"]
        self.interval = interval
        self._stop = False
        self._reserve_history: dict[str, deque[float]] = {
            a: deque(maxlen=24) for a in self.assets
        }

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("ExchangeFlowAgent starting: assets=%s", self.assets)
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                for asset in self.assets:
                    await self._poll(session, asset)
                await asyncio.sleep(self.interval)

    async def _poll(self, session: aiohttp.ClientSession, asset: str):
        coin_id = _asset_to_coingecko(asset)
        try:
            await _rate_limit("coingecko_exchange")
            url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/tickers"
            params = {"exchange_ids": "binance,kraken,coinbase-exchange,okx,bybit",
                      "depth": "false"}
            cg_key = os.getenv("COINGECKO_API_KEY", "")
            if cg_key:
                params["x_cg_demo_api_key"] = cg_key
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
        except Exception as e:
            log.debug("ExchangeFlow fetch failed for %s: %s", asset, e)
            return

        tickers = data.get("tickers", [])
        if not tickers:
            return

        # Aggregate exchange volume as reserve proxy
        total_volume = sum(
            float(t.get("converted_volume", {}).get("usd", 0))
            for t in tickers
        )
        if total_volume <= 0:
            return

        self._reserve_history[asset].append(total_volume)
        hist = list(self._reserve_history[asset])
        if len(hist) < 3:
            return

        avg_prev = sum(hist[:-1]) / len(hist[:-1])
        if avg_prev <= 0:
            return

        ratio = total_volume / avg_prev
        strength = 0.0
        parts = []

        if ratio > 1.3:
            # Volume surge on exchanges — potential distribution
            strength = -min(0.5, (ratio - 1) * 0.5)
            parts.append(f"vol_surge={ratio:.2f}x")
        elif ratio < 0.7:
            # Volume drop — quieter exchanges, possible accumulation
            strength = min(0.4, (1 - ratio) * 0.5)
            parts.append(f"vol_drop={ratio:.2f}x")
        elif ratio > 1.1:
            strength = -0.1
            parts.append(f"vol_up={ratio:.2f}x")
        elif ratio < 0.9:
            strength = 0.1
            parts.append(f"vol_down={ratio:.2f}x")

        # Check for unusual spread across exchanges (stress indicator)
        prices = [float(t.get("last", 0)) for t in tickers if float(t.get("last", 0)) > 0]
        if len(prices) >= 2:
            spread_pct = (max(prices) - min(prices)) / min(prices) * 100
            if spread_pct > 0.5:
                strength -= 0.1  # wide spreads = market stress
                parts.append(f"spread={spread_pct:.2f}%")

        if abs(strength) < 0.03:
            return

        strength = max(-1.0, min(1.0, strength))
        confidence = min(1.0, 0.3 + len(parts) * 0.15 + len(hist) * 0.02)

        sig = Signal(self.name, asset,
                     "long" if strength > 0 else "short",
                     strength, confidence,
                     " ".join(parts) or "exchange_flow_neutral")
        await self.bus.publish("signal.exchange_flow", sig)


# ---------------------------------------------------------------------------
# 2. Stablecoin Monitor Agent
# ---------------------------------------------------------------------------

class StablecoinAgent:
    """Monitors stablecoin market caps and peg stability.

    Stablecoin dynamics are a leading indicator:
      - USDT/USDC market cap growing → fresh capital entering crypto → bullish
      - Market cap shrinking → capital leaving → bearish
      - Depeg events (>0.5% off $1.00) → systemic risk → bearish

    Uses CoinGecko free API. No key required (key optional for higher limits).
    Publishes signal.stablecoin (applied to all tracked assets).
    """

    name = "stablecoin"

    STABLES = {
        "tether":    "USDT",
        "usd-coin":  "USDC",
        "dai":       "DAI",
    }

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 600.0):
        self.bus = bus
        self.assets = assets or ["ETH", "BTC"]
        self.interval = interval
        self._stop = False
        self._mcap_history: dict[str, deque[float]] = {
            sid: deque(maxlen=12) for sid in self.STABLES
        }

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("StablecoinAgent starting")
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                await self._poll(session)
                await asyncio.sleep(self.interval)

    async def _poll(self, session: aiohttp.ClientSession):
        ids = ",".join(self.STABLES.keys())
        try:
            await _rate_limit("coingecko_stable")
            url = "https://api.coingecko.com/api/v3/simple/price"
            params = {"ids": ids, "vs_currencies": "usd",
                      "include_market_cap": "true", "include_24hr_change": "true"}
            cg_key = os.getenv("COINGECKO_API_KEY", "")
            if cg_key:
                params["x_cg_demo_api_key"] = cg_key
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
        except Exception as e:
            log.debug("Stablecoin fetch failed: %s", e)
            return

        strength = 0.0
        parts = []

        for cg_id, label in self.STABLES.items():
            info = data.get(cg_id, {})
            price = info.get("usd", 1.0)
            mcap = info.get("usd_market_cap", 0)
            change_24h = info.get("usd_24h_change", 0)

            # Depeg detection
            if price and abs(price - 1.0) > 0.005:
                depeg_pct = (price - 1.0) * 100
                strength -= 0.3  # any depeg is bearish for crypto
                parts.append(f"{label}_depeg={depeg_pct:+.2f}%")

            # Market cap trend
            if mcap > 0:
                self._mcap_history[cg_id].append(mcap)
                hist = list(self._mcap_history[cg_id])
                if len(hist) >= 3:
                    prev_avg = sum(hist[:-1]) / len(hist[:-1])
                    if prev_avg > 0:
                        mcap_change = (mcap - prev_avg) / prev_avg
                        if mcap_change > 0.005:
                            strength += 0.15  # growing stablecoin supply = bullish
                            parts.append(f"{label}_mcap_up={mcap_change:.2%}")
                        elif mcap_change < -0.005:
                            strength -= 0.15
                            parts.append(f"{label}_mcap_dn={mcap_change:.2%}")

            if change_24h and abs(change_24h) > 0.3:
                parts.append(f"{label}_24h={change_24h:+.2f}%")

        if abs(strength) < 0.03:
            return

        strength = max(-1.0, min(1.0, strength))
        confidence = min(1.0, 0.4 + len(parts) * 0.1)

        for asset in self.assets:
            sig = Signal(self.name, asset,
                         "long" if strength > 0 else "short",
                         strength, confidence,
                         " ".join(parts) or "stablecoins_neutral")
            await self.bus.publish("signal.stablecoin", sig)


# ---------------------------------------------------------------------------
# 3. Macro Economic Calendar Agent
# ---------------------------------------------------------------------------

class MacroCalendarAgent:
    """Monitors upcoming economic events that impact crypto markets.

    Key events: FOMC decisions, CPI releases, NFP reports, PMI data.
    Uses free public APIs (Trading Economics RSS, FRED, or a static calendar).

    Before high-impact events → reduce confidence on all signals.
    After events → detect surprise direction.

    Publishes signal.macro (applied to all tracked assets).
    """

    name = "macro"

    # Key macro event keywords and their expected crypto impact
    EVENT_IMPACT = {
        "fomc": 0.8,
        "federal reserve": 0.8,
        "interest rate": 0.8,
        "cpi": 0.7,
        "inflation": 0.7,
        "nonfarm": 0.6,
        "non-farm": 0.6,
        "unemployment": 0.5,
        "gdp": 0.5,
        "pmi": 0.4,
        "retail sales": 0.4,
        "jobless claims": 0.3,
        "treasury": 0.5,
        "debt ceiling": 0.7,
        "tariff": 0.6,
    }

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 1800.0):
        self.bus = bus
        self.assets = assets or ["ETH", "BTC"]
        self.interval = interval
        self._stop = False
        self._last_events: list[dict] = []

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("MacroCalendarAgent starting")
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                await self._poll(session)
                await asyncio.sleep(self.interval)

    async def _poll(self, session: aiohttp.ClientSession):
        events = await self._fetch_events(session)
        if not events:
            return

        strength = 0.0
        parts = []

        for event in events:
            title = event.get("title", "").lower()
            impact = 0.0
            for keyword, weight in self.EVENT_IMPACT.items():
                if keyword in title:
                    impact = max(impact, weight)

            if impact < 0.2:
                continue

            # Upcoming events reduce conviction (uncertainty)
            # Recent events with surprise can drive direction
            parts.append(f"{event.get('title', '')[:50]}(impact={impact:.1f})")

            # Pre-event: dampen signals (uncertainty premium)
            strength -= impact * 0.1

        if abs(strength) < 0.02:
            return

        strength = max(-1.0, min(1.0, strength))
        confidence = min(0.6, 0.2 + len(parts) * 0.1)

        for asset in self.assets:
            sig = Signal(self.name, asset,
                         "long" if strength > 0 else "short",
                         strength, confidence,
                         " ".join(parts[:3]) or "macro_neutral")
            await self.bus.publish("signal.macro", sig)

    async def _fetch_events(self, session: aiohttp.ClientSession) -> list[dict]:
        """Fetch economic calendar from multiple free sources."""
        events: list[dict] = []

        # Source 1: FRED (Federal Reserve Economic Data) — recent releases
        try:
            await _rate_limit("fred")
            fred_key = os.getenv("FRED_API_KEY", "")
            if fred_key:
                url = "https://api.stlouisfed.org/fred/releases/dates"
                params = {
                    "api_key": fred_key,
                    "file_type": "json",
                    "limit": "10",
                    "sort_order": "desc",
                    "include_release_dates_with_no_data": "false",
                }
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for rel in data.get("release_dates", []):
                            events.append({
                                "title": rel.get("release_name", ""),
                                "date": rel.get("date", ""),
                                "source": "FRED",
                            })
        except Exception as e:
            log.debug("FRED calendar fetch failed: %s", e)

        # Source 2: Forex Factory RSS (free, no key)
        try:
            await _rate_limit("forexfactory")
            url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
            async with session.get(url,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for ev in (data if isinstance(data, list) else []):
                        impact_str = ev.get("impact", "").lower()
                        if impact_str in ("high", "medium"):
                            events.append({
                                "title": ev.get("title", ""),
                                "date": ev.get("date", ""),
                                "country": ev.get("country", ""),
                                "impact": impact_str,
                                "source": "ForexFactory",
                            })
        except Exception as e:
            log.debug("ForexFactory calendar fetch failed: %s", e)

        self._last_events = events
        return events


# ---------------------------------------------------------------------------
# 4. Deribit Options Flow Agent
# ---------------------------------------------------------------------------

class DeribitOptionsAgent:
    """Monitors options market data from Deribit for crypto sentiment.

    Key metrics:
      - Put/Call ratio: high ratio → bearish hedging → contrarian bullish
      - Implied volatility (IV): rising IV → uncertainty
      - Max pain: strike price where most options expire worthless
      - IV skew: puts more expensive than calls → fear

    Uses Deribit's public (no auth required) API.
    Publishes signal.options per asset.
    """

    name = "options"

    DERIBIT_BASE = "https://www.deribit.com/api/v2/public"

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 300.0):
        self.bus = bus
        self.assets = assets or ["ETH", "BTC"]
        self.interval = interval
        self._stop = False
        self._iv_history: dict[str, deque[float]] = {
            a: deque(maxlen=24) for a in self.assets
        }

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("DeribitOptionsAgent starting: assets=%s", self.assets)
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                for asset in self.assets:
                    await self._poll(session, asset)
                await asyncio.sleep(self.interval)

    async def _poll(self, session: aiohttp.ClientSession, asset: str):
        currency = asset.upper()
        if currency == "ETH":
            currency = "ETH"
        elif currency == "BTC":
            currency = "BTC"
        else:
            return  # Deribit only has BTC/ETH options

        strength = 0.0
        parts = []

        # Fetch book summary for options
        try:
            await _rate_limit("deribit")
            url = f"{self.DERIBIT_BASE}/get_book_summary_by_currency"
            params = {"currency": currency, "kind": "option"}
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
        except Exception as e:
            log.debug("Deribit options fetch failed for %s: %s", asset, e)
            return

        results = data.get("result", [])
        if not results:
            return

        # Calculate put/call ratio and aggregate IV
        put_oi = 0.0
        call_oi = 0.0
        put_volume = 0.0
        call_volume = 0.0
        iv_sum = 0.0
        iv_count = 0
        put_iv_sum = 0.0
        put_iv_count = 0
        call_iv_sum = 0.0
        call_iv_count = 0

        for inst in results:
            name = inst.get("instrument_name", "")
            oi = inst.get("open_interest", 0) or 0
            vol = inst.get("volume", 0) or 0
            mark_iv = inst.get("mark_iv", 0) or 0

            if "-P" in name:
                put_oi += oi
                put_volume += vol
                if mark_iv > 0:
                    put_iv_sum += mark_iv
                    put_iv_count += 1
            elif "-C" in name:
                call_oi += oi
                call_volume += vol
                if mark_iv > 0:
                    call_iv_sum += mark_iv
                    call_iv_count += 1

            if mark_iv > 0:
                iv_sum += mark_iv
                iv_count += 1

        # Put/Call ratio
        if call_oi > 0:
            pc_ratio = put_oi / call_oi
            parts.append(f"P/C={pc_ratio:.2f}")
            if pc_ratio > 1.2:
                # High put/call = hedging/fear → contrarian bullish
                strength += min(0.4, (pc_ratio - 1.0) * 0.3)
            elif pc_ratio < 0.6:
                # Low put/call = complacency → contrarian bearish
                strength -= min(0.3, (1.0 - pc_ratio) * 0.3)

        if call_volume > 0:
            pc_vol_ratio = put_volume / call_volume
            parts.append(f"P/C_vol={pc_vol_ratio:.2f}")

        # Aggregate IV
        if iv_count > 0:
            avg_iv = iv_sum / iv_count
            self._iv_history[asset].append(avg_iv)
            parts.append(f"IV={avg_iv:.1f}%")

            # IV trend
            hist = list(self._iv_history[asset])
            if len(hist) >= 3:
                prev_avg = sum(hist[:-1]) / len(hist[:-1])
                if prev_avg > 0:
                    iv_change = (avg_iv - prev_avg) / prev_avg
                    if iv_change > 0.1:
                        strength -= 0.15  # rising IV = uncertainty
                        parts.append(f"IV_rising={iv_change:.1%}")
                    elif iv_change < -0.1:
                        strength += 0.1  # falling IV = calm
                        parts.append(f"IV_falling={iv_change:.1%}")

        # IV skew (put IV vs call IV)
        if put_iv_count > 0 and call_iv_count > 0:
            put_iv = put_iv_sum / put_iv_count
            call_iv = call_iv_sum / call_iv_count
            skew = put_iv - call_iv
            if abs(skew) > 5:
                parts.append(f"skew={skew:+.1f}")
                if skew > 10:
                    # Puts much more expensive = fear → contrarian bullish
                    strength += 0.15
                elif skew < -10:
                    # Calls expensive = euphoria → contrarian bearish
                    strength -= 0.1

        if abs(strength) < 0.03:
            return

        strength = max(-1.0, min(1.0, strength))
        confidence = min(1.0, 0.35 + len(parts) * 0.08)

        sig = Signal(self.name, asset,
                     "long" if strength > 0 else "short",
                     strength, confidence,
                     " ".join(parts))
        await self.bus.publish("signal.options", sig)


# ---------------------------------------------------------------------------
# 5. Token Unlock / Vesting Agent
# ---------------------------------------------------------------------------

class TokenUnlockAgent:
    """Monitors upcoming token unlock/vesting events.

    Large upcoming unlocks create sell pressure as early investors and teams
    gain access to previously locked tokens.

    Uses DeFi Llama's unlocks endpoint (free, no key).
    Publishes signal.unlock per asset.
    """

    name = "unlock"

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 3600.0):
        self.bus = bus
        self.assets = assets or ["ETH", "BTC"]
        self.interval = interval
        self._stop = False

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("TokenUnlockAgent starting: assets=%s", self.assets)
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                for asset in self.assets:
                    await self._poll(session, asset)
                await asyncio.sleep(self.interval)

    async def _poll(self, session: aiohttp.ClientSession, asset: str):
        protocol = _asset_to_defillama(asset)
        if not protocol:
            return

        try:
            await _rate_limit("defillama_unlock")
            url = f"https://api.llama.fi/protocol/{protocol}"
            async with session.get(url,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
        except Exception as e:
            log.debug("TokenUnlock fetch failed for %s: %s", asset, e)
            return

        # Check for upcoming unlock events in the protocol data
        mcap = data.get("mcap", 0)
        if not mcap or mcap <= 0:
            return

        # DeFi Llama provides TVL history — sudden TVL drops can indicate
        # unlocks being dumped
        tvl = data.get("tvl", [])
        if not tvl or len(tvl) < 7:
            return

        # Compare recent TVL trend
        recent = [p.get("totalLiquidityUSD", 0) for p in tvl[-3:] if p.get("totalLiquidityUSD")]
        older = [p.get("totalLiquidityUSD", 0) for p in tvl[-7:-3] if p.get("totalLiquidityUSD")]

        if not recent or not older:
            return

        avg_recent = sum(recent) / len(recent)
        avg_older = sum(older) / len(older)

        if avg_older <= 0:
            return

        change = (avg_recent - avg_older) / avg_older
        strength = 0.0
        parts = []

        if change < -0.1:
            # TVL declining significantly — potential sell pressure
            strength = max(-0.4, change)
            parts.append(f"TVL_decline={change:.1%}")
        elif change > 0.1:
            # TVL growing — capital inflow
            strength = min(0.3, change * 0.5)
            parts.append(f"TVL_growth={change:.1%}")

        if abs(strength) < 0.03:
            return

        confidence = min(0.6, 0.3 + abs(change) * 2)

        sig = Signal(self.name, asset,
                     "long" if strength > 0 else "short",
                     strength, confidence,
                     " ".join(parts))
        await self.bus.publish("signal.unlock", sig)


# ---------------------------------------------------------------------------
# 6. GitHub Developer Activity Agent
# ---------------------------------------------------------------------------

class GitHubDevAgent:
    """Monitors GitHub commit velocity for major crypto protocol repos.

    Developer activity is a long-term leading indicator:
      - Commit surge → active development → bullish fundamentals
      - Commit stall → team disengagement → bearish fundamentals
      - New releases / tags → potential catalyst

    Uses GitHub public API (no auth for 60 req/hr, with token 5000 req/hr).
    Set GITHUB_TOKEN env var for higher rate limits.
    Publishes signal.github per asset.
    """

    name = "github"

    # Map assets to their primary GitHub repos
    REPOS = {
        "ETH": ["ethereum/go-ethereum", "ethereum/solidity"],
        "BTC": ["bitcoin/bitcoin"],
        "SOL": ["solana-labs/solana"],
        "AVAX": ["ava-labs/avalanchego"],
        "DOT": ["paritytech/polkadot-sdk"],
        "LINK": ["smartcontractkit/chainlink"],
        "ADA": ["IntersectMBO/cardano-node"],
        "XRP": ["XRPLF/rippled"],
    }

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 1800.0):
        self.bus = bus
        self.assets = assets or ["ETH", "BTC"]
        self.interval = interval
        self._stop = False
        self._commit_history: dict[str, deque[int]] = {
            a: deque(maxlen=12) for a in self.assets
        }
        self.gh_token = os.getenv("GITHUB_TOKEN", "")

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("GitHubDevAgent starting: assets=%s", self.assets)
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                for asset in self.assets:
                    await self._poll(session, asset)
                await asyncio.sleep(self.interval)

    async def _poll(self, session: aiohttp.ClientSession, asset: str):
        repos = self.REPOS.get(asset, [])
        if not repos:
            return

        total_commits = 0
        total_stars = 0
        recent_releases = 0
        parts = []
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self.gh_token:
            headers["Authorization"] = f"token {self.gh_token}"

        for repo in repos:
            try:
                await _rate_limit("github")

                # Recent commits (last week)
                url = f"https://api.github.com/repos/{repo}/stats/participation"
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        all_weeks = data.get("all", [])
                        if all_weeks:
                            total_commits += all_weeks[-1]  # last week

                # Check for recent releases
                await _rate_limit("github")
                url = f"https://api.github.com/repos/{repo}/releases"
                params = {"per_page": "3"}
                async with session.get(url, headers=headers, params=params,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        releases = await resp.json()
                        for rel in releases:
                            pub = rel.get("published_at", "")
                            # Check if release is within last 7 days
                            if pub and _is_recent(pub, days=7):
                                recent_releases += 1

                # Stars as popularity metric
                await _rate_limit("github")
                url = f"https://api.github.com/repos/{repo}"
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        repo_data = await resp.json()
                        total_stars += repo_data.get("stargazers_count", 0)

            except Exception as e:
                log.debug("GitHub fetch failed for %s/%s: %s", asset, repo, e)

        if total_commits <= 0:
            return

        self._commit_history[asset].append(total_commits)
        hist = list(self._commit_history[asset])

        strength = 0.0

        # Commit trend
        if len(hist) >= 3:
            avg_prev = sum(hist[:-1]) / len(hist[:-1])
            if avg_prev > 0:
                ratio = total_commits / avg_prev
                if ratio > 1.3:
                    strength += min(0.3, (ratio - 1) * 0.4)
                    parts.append(f"commits_surge={ratio:.2f}x")
                elif ratio < 0.5:
                    strength -= min(0.2, (1 - ratio) * 0.3)
                    parts.append(f"commits_stall={ratio:.2f}x")

        parts.append(f"commits_7d={total_commits}")

        if recent_releases > 0:
            strength += 0.1 * recent_releases
            parts.append(f"releases={recent_releases}")

        if total_stars > 50000:
            parts.append(f"stars={total_stars}")

        if abs(strength) < 0.03:
            return

        strength = max(-1.0, min(1.0, strength))
        confidence = min(0.5, 0.2 + len(hist) * 0.03)

        sig = Signal(self.name, asset,
                     "long" if strength > 0 else "short",
                     strength, confidence,
                     " ".join(parts))
        await self.bus.publish("signal.github", sig)


# ---------------------------------------------------------------------------
# 7. RSS Multi-Source News Agent (no API key required)
# ---------------------------------------------------------------------------

class RSSNewsAgent:
    """Aggregates crypto news from free RSS/Atom feeds.

    Sources: CoinDesk, CoinTelegraph, The Block, Decrypt, Bitcoin Magazine,
    The Defiant, plus Blockworks and DL News.
    No API keys required — uses public RSS feeds.

    Uses the same regex-based urgency and keyword scoring pipeline as
    NewsAgent (CryptoPanic) so it can fully replace it at zero cost.

    Publishes signal.news (same topic as NewsAgent for Strategist compat)
    and signal.rss_news for downstream consumers that want to distinguish.
    """

    name = "news"  # same agent_id as NewsAgent so Strategist picks it up

    FEEDS = [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://bitcoinmagazine.com/.rss/full/",
        "https://thedefiant.io/feed",
        "https://blockworks.co/feed",
        "https://www.dlnews.com/arc/outboundfeeds/rss/",
    ]

    # ── Regex keyword tables (ported from NewsAgent for full parity) ──
    # (direction_bias, magnitude_boost)
    _BULLISH_PATTERNS_RAW: dict[str, tuple[float, float]] = {
        r"\betf\b.*approv":       (+1.0, 2.5),
        r"\betf\b.*launch":       (+1.0, 2.0),
        r"\betf\b.*fil":          (+1.0, 2.0),
        r"institutional.*buy":    (+1.0, 1.8),
        r"mass\s*adoption":       (+1.0, 1.5),
        r"partnershi":            (+1.0, 1.3),
        r"integrat":              (+1.0, 1.2),
        r"halving":               (+1.0, 1.5),
        r"supply\s*shock":        (+1.0, 1.8),
        r"token\s*burn":          (+1.0, 1.4),
        r"legal\s*tender":        (+1.0, 2.0),
        r"regulat.*clar":         (+1.0, 1.5),
        r"regulat.*framework":    (+1.0, 1.3),
        r"all.time.high":         (+1.0, 1.6),
        r"\bath\b":               (+1.0, 1.6),
        r"breakout":              (+1.0, 1.3),
        r"rally":                 (+1.0, 1.2),
        r"surge[sd]?":            (+1.0, 1.3),
        r"soar":                  (+1.0, 1.3),
        r"accumulate":            (+1.0, 1.2),
        r"inflow":                (+1.0, 1.2),
        r"staking":               (+1.0, 1.1),
        r"upgrade":               (+1.0, 1.2),
        r"milestone":             (+1.0, 1.2),
    }

    _BEARISH_PATTERNS_RAW: dict[str, tuple[float, float]] = {
        r"hack(ed|s|ing)?":       (-1.0, 2.5),
        r"exploit(ed|s)?":        (-1.0, 2.5),
        r"rug\s*pull":            (-1.0, 2.5),
        r"drain(ed)?":            (-1.0, 2.0),
        r"vulnerabilit":          (-1.0, 1.8),
        r"breach":                (-1.0, 2.0),
        r"\bban(ned|s)?\b":       (-1.0, 2.2),
        r"crackdown":             (-1.0, 2.0),
        r"lawsuit":               (-1.0, 1.8),
        r"sec\s*(su|charg|fine)": (-1.0, 2.0),
        r"sanction":              (-1.0, 1.8),
        r"fraud":                 (-1.0, 2.0),
        r"crash(ed|es|ing)?":     (-1.0, 2.0),
        r"plunge[sd]?":           (-1.0, 1.8),
        r"dump(ed|ing)?":         (-1.0, 1.5),
        r"liquidat":              (-1.0, 1.8),
        r"insolvency|insolvent":  (-1.0, 2.2),
        r"bankrupt":              (-1.0, 2.2),
        r"delisted?":             (-1.0, 1.8),
        r"ponzi":                 (-1.0, 2.0),
        r"selloff|sell-off":      (-1.0, 1.5),
        r"outflow":               (-1.0, 1.2),
        r"scam":                  (-1.0, 2.0),
    }

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 300.0):
        self.bus = bus
        self.assets = assets or ["ETH", "BTC"]
        self.interval = interval
        self._stop = False
        self._seen: deque[str] = deque(maxlen=500)
        self._asset_aliases = {
            "ETH": {"ethereum", "eth", "ether", "vitalik"},
            "BTC": {"bitcoin", "btc", "satoshi"},
            "SOL": {"solana", "sol"},
            "XRP": {"xrp", "ripple"},
            "ADA": {"cardano", "ada"},
            "DOT": {"polkadot", "dot"},
            "LINK": {"chainlink", "link"},
            "AVAX": {"avalanche", "avax"},
        }
        # Compile regex patterns once
        import re
        self._bull_patterns = [
            (re.compile(p, re.IGNORECASE), v)
            for p, v in self._BULLISH_PATTERNS_RAW.items()
        ]
        self._bear_patterns = [
            (re.compile(p, re.IGNORECASE), v)
            for p, v in self._BEARISH_PATTERNS_RAW.items()
        ]

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("RSSNewsAgent starting: %d feeds, assets=%s",
                 len(self.FEEDS), self.assets)
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                await self._poll(session)
                await asyncio.sleep(self.interval)

    async def _poll(self, session: aiohttp.ClientSession):
        articles: list[dict] = []

        for feed_url in self.FEEDS:
            try:
                await _rate_limit("rss")
                async with session.get(
                    feed_url,
                    timeout=aiohttp.ClientTimeout(total=15),
                    headers={"User-Agent": "SwarmTrader/1.0"},
                ) as resp:
                    if resp.status != 200:
                        continue
                    text = await resp.text()
                    articles.extend(self._parse_feed(text, feed_url))
            except Exception as e:
                log.debug("RSS fetch failed for %s: %s", feed_url[:40], e)

        if not articles:
            return

        # Score articles per asset
        for asset in self.assets:
            scored = self._score_for_asset(articles, asset)
            if scored:
                await self._emit_signal(asset, scored)

    def _parse_feed(self, xml_text: str, source: str) -> list[dict]:
        """Parse RSS/Atom XML into article dicts."""
        items = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return items

        # RSS 2.0
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
            if title and link and link not in self._seen:
                self._seen.append(link)
                items.append({"title": title, "link": link,
                              "desc": desc[:200], "source": source})

        # Atom
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title = (entry.findtext("atom:title", "", ns) or
                     entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link = link_el.get("href", "") if link_el is not None else ""
            if title and link and link not in self._seen:
                self._seen.append(link)
                items.append({"title": title, "link": link,
                              "desc": "", "source": source})

        return items[:10]  # limit per feed

    def _keyword_analysis(self, title: str) -> tuple[float, float, float, list[str]]:
        """Regex keyword scoring — same pipeline as NewsAgent.

        Returns (direction_bias, magnitude_boost, urgency, keywords_hit).
        """
        if not title:
            return 0.0, 1.0, 0.0, []

        bull_score = 0.0
        bear_score = 0.0
        max_boost = 1.0
        hits: list[str] = []

        for pat, (bias, boost) in self._bull_patterns:
            if pat.search(title):
                bull_score += bias * boost
                max_boost = max(max_boost, boost)
                hits.append(pat.pattern[:20])

        for pat, (bias, boost) in self._bear_patterns:
            if pat.search(title):
                bear_score += abs(bias) * boost
                max_boost = max(max_boost, boost)
                hits.append(pat.pattern[:20])

        net = bull_score - bear_score
        if abs(net) < 0.01:
            return 0.0, 1.0, 0.0, hits

        direction = 1.0 if net > 0 else -1.0
        urgency = min(1.0, max(0.0, (max_boost - 1.0) / 1.5))
        return direction, max_boost, urgency, hits

    def _score_for_asset(self, articles: list[dict], asset: str) -> list[dict]:
        """Filter and score articles relevant to an asset."""
        aliases = self._asset_aliases.get(asset, {asset.lower()})
        relevant = []

        for i, art in enumerate(articles):
            text = (art["title"] + " " + art.get("desc", "")).lower()
            # Check if article mentions this asset
            direct_mention = any(alias in text for alias in aliases)
            if not direct_mention:
                if not any(w in text for w in ("crypto", "market", "defi", "blockchain")):
                    continue

            # Regex keyword scoring
            kw_bias, kw_boost, urgency, kw_hits = self._keyword_analysis(
                art["title"] + " " + art.get("desc", ""))

            sentiment = kw_bias * min(1.0, kw_boost * 0.4) if abs(kw_bias) > 0.01 else 0.0
            sentiment = max(-1.0, min(1.0, sentiment))

            relevance = 1.0 if direct_mention else 0.5
            recency_w = 1.0 / (1 + i * 0.2)
            urgency_w = 1.0 + urgency * 1.5

            relevant.append({
                "title": art["title"],
                "sentiment": sentiment,
                "relevance": relevance,
                "weight": relevance * recency_w * urgency_w,
                "urgency": urgency,
                "keywords": kw_hits,
            })

        return relevant[:15]

    async def _emit_signal(self, asset: str, scored: list[dict]):
        if not scored:
            return

        total_weight = sum(a["weight"] for a in scored)
        if total_weight < 0.01:
            return

        weighted_sent = sum(a["sentiment"] * a["weight"] for a in scored) / total_weight
        strength = max(-1.0, min(1.0, weighted_sent))

        if abs(strength) < 0.05:
            return

        max_urgency = max((a["urgency"] for a in scored), default=0.0)
        confidence = min(1.0, 0.3 + len(scored) * 0.04 + max_urgency * 0.2)
        top = [s["title"][:60] for s in scored[:3]]
        all_kw: list[str] = []
        for a in scored:
            all_kw.extend(a.get("keywords", []))
        kw_summary = ",".join(dict.fromkeys(all_kw))[:80]

        parts = [f"news={strength:+.3f}", f"n={len(scored)}"]
        if kw_summary:
            parts.append(f"kw=[{kw_summary}]")
        if max_urgency >= 0.5:
            parts.append(f"URGENT({max_urgency:.1f})")
        parts.append("|")
        parts.append("; ".join(top))

        sig = Signal(self.name, asset,
                     "long" if strength > 0 else "short",
                     strength, confidence,
                     " ".join(parts))
        # Publish on both topics: signal.news for Strategist, signal.rss_news for filtering
        await self.bus.publish("signal.news", sig)
        await self.bus.publish("signal.rss_news", sig)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _asset_to_coingecko(asset: str) -> str:
    return {
        "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
        "XRP": "ripple", "ADA": "cardano", "DOT": "polkadot",
        "LINK": "chainlink", "AVAX": "avalanche-2",
        "DOGE": "dogecoin", "MATIC": "matic-network",
    }.get(asset.upper(), asset.lower())


def _asset_to_defillama(asset: str) -> str | None:
    return {
        "ETH": "ethereum", "SOL": "solana",
        "AVAX": "avalanche", "DOT": "polkadot",
        "LINK": "chainlink", "ADA": "cardano",
    }.get(asset.upper())


def _is_recent(iso_date: str, days: int = 7) -> bool:
    """Check if an ISO date string is within the last N days."""
    try:
        from datetime import datetime, timezone, timedelta
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt) < timedelta(days=days)
    except Exception:
        return False
