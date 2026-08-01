"""Mock read-side storage — run the whole web app with NO database.

    OVERSEER_MOCK=1 flask --app web:create_app run --debug
    log in as  mock@overseer.local  /  mock

Duck-types ReadStorage: every query the views use returns realistic, fully
deterministic data (seeded from series name + bar index, so refreshes and
pans are stable, prices are venue-consistent for sane-looking basis, and the
funding grid gets a proper spread of green/red cells). Auth works through the
real code path via one built-in account. Nothing here touches the network or
disk; swap to live by unsetting OVERSEER_MOCK.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from werkzeug.security import generate_password_hash

Row = dict[str, Any]
UTC = timezone.utc

MOCK_EMAIL = "mock@overseer.local"
MOCK_PASSWORD = "mock"

# the live venue/asset matrix (binance_spot deliberately lacks HYPE, as in prod)
_BASES = {"BTC": 102_500.0, "ETH": 3_260.0, "SOL": 219.0, "AAVE": 312.0, "HYPE": 44.0,
          "XRP": 3.1, "DOGE": 0.42, "LINK": 26.5, "AVAX": 55.0}

# wide-universe extras: NOT in the asset registry, so they render under their
# raw venue symbols — exactly what funding = "all" produces for memecoins and
# fresh listings, where the biggest dislocations live.
_WIDE_FUNDING = [
    # (exchange, symbol, interval_h, apr_pct, oi_notional)
    ("hyperliquid", "FARTCOIN", 1,  212.4,  8_400_000),
    ("lighter",     "FARTCOIN", 1,  148.9,  2_100_000),
    ("hyperliquid", "PUMP",     1, -164.2,  5_900_000),
    ("extended",    "PUMP-USD", 1,  -31.0,    740_000),
    ("lighter",     "WIF",      1,   96.3,  3_300_000),
    ("bybit",       "WIF/USDT", 8,   41.7, 12_800_000),
    ("hyperliquid", "NEWLISTING", 1, 305.8,   410_000),
]

MOCK_FILLS = {
    "0x1b7e1a1e8f6a2c9d4e5f60718293a4b5c6d7e8f9": "hlp",
    "281474976710654": "llp",
}
_VENUES = [
    # venue_id        exchange       market_type  symbol pattern      funding_h
    ("binance_spot",  "binance",     "spot", "{a}/USDT", None),
    ("binance_perp",  "binance",     "perp", "{a}/USDT", 8),
    ("bybit_spot",    "bybit",       "spot", "{a}/USDT", None),
    ("bybit_perp",    "bybit",       "perp", "{a}/USDT", 8),
    ("hyperliquid",   "hyperliquid", "perp", "{a}",      1),
    ("lighter",       "lighter",     "perp", "{a}",      1),
    ("extended",      "extended",    "perp", "{a}-USD",  1),
    ("rise",          "rise",        "perp", "{a}/USDC", 1),
    ("bullet",        "bullet",      "perp", "{a}-USD",  1),
]


def _u(seed: str, i: int) -> float:
    """Deterministic uniform in [-1, 1) from (seed, index)."""
    h = hashlib.blake2b(f"{seed}:{i}".encode(), digest_size=8).digest()
    return int.from_bytes(h, "big") / 2**63 - 1.0


def _px(seed: str, base: float, i: int) -> float:
    """Price of bar i (minutes since epoch): layered waves + hash noise —
    stable for any window, mildly trending, venue-consistent via shared base."""
    wave = (0.018 * math.sin(i / 240) + 0.007 * math.sin(i / 37 + _u(seed, 0) * 3)
            + 0.003 * math.sin(i / 9 + _u(seed, 1) * 3))
    noise = 0.0012 * _u(seed, i)
    drift = 0.00001 * math.sin(i / 3000)
    return base * (1 + wave + noise + drift * i % 1_000_000 * 0)  # bounded


class MockStorage:
    """Drop-in for storage.queries.ReadStorage."""

    def __init__(self, *args, **kwargs) -> None:
        self._pwhash = generate_password_hash(MOCK_PASSWORD)

    def close(self) -> None:
        pass

    # -- auth plumbing (web/auth.py goes through _fetch for users) --------------

    def _fetch(self, sql: str, params: tuple = ()) -> list[Row]:
        if "FROM users" in sql:
            return [{
                "id": 1, "email": MOCK_EMAIL, "password_hash": self._pwhash,
                "can_view_internal": True,
            }]
        return []

    @property
    def _pool(self):
        raise RuntimeError("mock storage has no pool — create-user is disabled in mock mode")

    # -- series / candles ---------------------------------------------------------

    def _series(self):
        for venue, exchange, mt, pat, fh in _VENUES:
            for asset, base in _BASES.items():
                if venue == "binance_spot" and asset == "HYPE":
                    continue
                yield venue, exchange, mt, pat.format(a=asset), asset, base, fh

    def series_list(self) -> list[Row]:
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        out = []
        for _, exchange, mt, symbol, _a, _b, _f in self._series():
            out.append({
                "exchange": exchange, "market_type": mt, "symbol": symbol,
                "interval": "1m", "bars": 4320,
                "first_ts": now - timedelta(days=3), "last_ts": now,
            })
        out.sort(key=lambda r: (r["exchange"], r["market_type"], r["symbol"]))
        return out

    def candles(self, exchange, market_type, symbol, interval,
                since=None, until=None, limit: int = 1000) -> list[Row]:
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        until = min(until or now, now)
        since = since or (until - timedelta(minutes=limit))
        n = min(limit, max(2, int((until - since).total_seconds() // 60)))
        base = next((b for *_x, sym, _a, b, _f in
                     ((v, e, m, s, a, bb, f) for v, e, m, s, a, bb, f in self._series())
                     if sym == symbol), 100.0)
        seed = f"{exchange}:{market_type}:{symbol}"
        end_i = int(until.timestamp() // 60)
        rows: list[Row] = []
        for k in range(n):
            i = end_i - (n - 1 - k)
            c, o = _px(seed, base, i), _px(seed, base, i - 1)
            hi = max(o, c) * (1 + 0.0008 * abs(_u(seed, i + 7)))
            lo = min(o, c) * (1 - 0.0008 * abs(_u(seed, i + 13)))
            vol = base * 0.02 * (1.2 + _u(seed, i + 29))
            rows.append({
                "ts": datetime.fromtimestamp(i * 60, tz=UTC),
                "open": Decimal(f"{o:.6f}"), "high": Decimal(f"{hi:.6f}"),
                "low": Decimal(f"{lo:.6f}"), "close": Decimal(f"{c:.6f}"),
                "volume": Decimal(f"{abs(vol):.4f}"),
            })
        return rows

    def basis(self, spot_exchange, spot_symbol, perp_exchange, perp_symbol,
              interval, since=None, limit: int = 1000) -> list[Row]:
        spot = self.candles(spot_exchange, "spot", spot_symbol, interval, since, None, limit)
        perp = self.candles(perp_exchange, "perp", perp_symbol, interval, since, None, limit)
        out: list[Row] = []
        for s, p in zip(spot, perp):
            sc, pc = s["close"], p["close"]
            out.append({
                "ts": s["ts"], "spot_close": sc, "perp_close": pc,
                "basis": pc - sc,
                "basis_pct": (pc - sc) / sc * 100,
            })
        return out

    # -- funding table -------------------------------------------------------------

    @staticmethod
    def _last_settlement(now: datetime, interval_hours: int) -> datetime:
        """Newest settlement boundary at or before `now`.

        Real venues settle on fixed boundaries — hourly venues on the hour,
        8h venues at 00/08/16 UTC — not at an arbitrary offset from now. It
        matters beyond realism: the flow page derives settlement markers from
        this timestamp, and the mock's flow generator builds its pre/post
        pattern on the same boundaries, so a random offset would put the
        markers where nothing happens.
        """
        period = interval_hours * 3600
        return datetime.fromtimestamp(
            (int(now.timestamp()) // period) * period, tz=UTC)

    def funding_table(self) -> list[Row]:
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        rows: list[Row] = []
        for venue, exchange, mt, symbol, asset, base, fh in self._series():
            if mt != "perp" or fh is None:
                continue
            # per (venue, asset) APR in roughly ±35%, mostly positive
            apr = 8.0 + 18.0 * _u(f"apr:{venue}:{asset}", 1)
            if _u(f"neg:{venue}:{asset}", 2) > 0.55:
                apr = -abs(apr) * 0.8
            rate = Decimal(f"{apr / 100 / (8760 / fh):.10f}")
            oi_base = {"BTC": 48_000, "ETH": 310_000, "SOL": 2_400_000,
                       "AAVE": 260_000, "HYPE": 4_100_000}.get(
                asset, 900_000_000 / base            # sensible default: ~$900M notional
            ) * (1 + 0.4 * _u(f"oi:{venue}:{asset}", 3))
            mark = Decimal(f"{_px(f'{exchange}:perp:{symbol}', base, int(now.timestamp() // 60)):.4f}")
            missing_liq = _u(f"liq:{venue}:{asset}", 4) > 0.92     # a couple of '—' cells
            # ±30% swing, occasionally beyond ±20% to exercise the squeeze case
            oi_delta = 30.0 * _u(f"oidelta:{venue}:{asset}", 6)
            settled_at = self._last_settlement(now, fh)
            next_funding = settled_at + timedelta(hours=fh)
            rows.append({
                "exchange": exchange, "symbol": symbol,
                "funding_ts": settled_at,
                "rate": rate, "interval_hours": fh,
                "apr_pct": Decimal(f"{apr:.4f}"),
                "rate_kind": "settled" if missing_liq else "current",
                "rate_ts": settled_at if missing_liq else now,
                "settled_rate": rate,
                "settled_interval_hours": fh,
                "settled_apr_pct": Decimal(f"{apr:.4f}"),
                "current_funding_rate": None if missing_liq else rate,
                "current_interval_hours": None if missing_liq else fh,
                "next_funding_at": None if missing_liq else next_funding,
                "funding_premium": None,
                "index_price": None if missing_liq else mark * Decimal("1.0002"),
                "open_interest": None if missing_liq else Decimal(f"{oi_base:.2f}"),
                "oi_notional": None if missing_liq else Decimal(f"{oi_base:.2f}") * mark,
                "volume_24h": None if missing_liq else Decimal(f"{float(mark) * oi_base * 2.4:.0f}"),
                "mark_price": None if missing_liq else mark,
                "liq_ts": now,
                "oi_delta_pct": None if missing_liq else Decimal(f"{oi_delta:.2f}"),
            })
        for exchange, symbol, fh, apr, oi_ntl in _WIDE_FUNDING:
            rate = Decimal(f"{apr / 100 / (8760 / fh):.10f}")
            settled_at = self._last_settlement(now, fh)
            rows.append({
                "exchange": exchange, "symbol": symbol,
                "funding_ts": self._last_settlement(now, fh),
                "rate": rate, "interval_hours": fh,
                "apr_pct": Decimal(f"{apr:.4f}"),
                "rate_kind": "current",
                "rate_ts": now,
                "settled_rate": rate,
                "settled_interval_hours": fh,
                "settled_apr_pct": Decimal(f"{apr:.4f}"),
                "current_funding_rate": rate,
                "current_interval_hours": fh,
                "next_funding_at": settled_at + timedelta(hours=fh),
                "funding_premium": None,
                "index_price": Decimal("1.0002"),
                "open_interest": Decimal("1"),
                "oi_notional": Decimal(str(oi_ntl)),
                "volume_24h": Decimal(str(oi_ntl * 3.1)),
                "mark_price": Decimal("1"),
                "liq_ts": now,
                # fresh listings/memecoins: OI usually still building fast
                "oi_delta_pct": Decimal(f"{60.0 * abs(_u(f'oidelta:{exchange}:{symbol}', 6)):.2f}"),
            })
        rows.sort(key=lambda r: (r["symbol"], r["exchange"]))
        return rows

    # -- health ---------------------------------------------------------------------

    def freshness(self) -> list[Row]:
        now = datetime.now(UTC)
        out: list[Row] = []
        for _v, exchange, mt, symbol, asset, _b, _f in self._series():
            stale = 182.4 if (exchange, symbol) == ("extended", "AAVE-USD") else \
                    abs(_u(f"fresh:{exchange}:{symbol}", 1)) * 1.4 + 0.2
            age = timedelta(seconds=int(stale * 60))
            out.append({
                "exchange": exchange, "market_type": mt, "symbol": symbol,
                "interval": "1m", "last_ts": now - age, "age": age,
                "stale_factor": stale,
            })
        out.sort(key=lambda r: -r["stale_factor"])
        return out

    def job_health(self) -> list[Row]:
        now = datetime.now(UTC)
        rows: list[Row] = []

        def job(job_id, status="ok", fetched=2, new=1, err=None, ago_s=40):
            rows.append({
                "job_id": job_id, "last_run_at": now - timedelta(seconds=20),
                "last_success_at": None if status == "fail" and err == "never" else
                                   now - timedelta(seconds=ago_s),
                "last_status": status, "fetched": fetched, "new_rows": new,
                "last_error": err, "since_success": timedelta(seconds=ago_s),
            })

        for venue, _e, mt, symbol, _a, _b, fh in self._series():
            job(f"ohlcv:{venue}:{symbol}:1m", fetched=3, new=2)
            if mt == "perp" and fh:
                job(f"funding:{venue}:{symbol}", fetched=1, new=0, ago_s=420)
        for venue in ("binance_perp", "bybit_perp", "hyperliquid", "lighter", "extended"):
            job(f"liquidity:{venue}", fetched=5, new=5, ago_s=110)
        job("fills:hyperliquid:hlp", fetched=87, new=81, ago_s=25)
        job("fills:lighter:llp", fetched=100, new=96, ago_s=25)
        # two representative failures so the red path is visible
        for r in rows:
            if r["job_id"] == "ohlcv:extended:AAVE-USD:1m":
                r.update(last_status="fail", fetched=0, new_rows=0,
                         last_error="extended error NOT_FOUND: Market not found",
                         last_success_at=now - timedelta(hours=3))
            if r["job_id"] == "funding:bybit_perp:SOL/USDT":
                r.update(last_status="fail", fetched=0, new_rows=0,
                         last_error="bybit error 10006: Too many visits. Exceeded the API Rate Limit.",
                         last_success_at=now - timedelta(minutes=31))
        rows.sort(key=lambda r: (r["last_status"] != "fail", r["job_id"]))
        return rows


    def venue_volume(self, days: int = 30) -> dict:
        now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        vols = {"binance": (14.2e9, 21.5e9), "bybit": (4.1e9, 8.9e9),
                "hyperliquid": (0.4e9, 9.8e9), "lighter": (0.15e9, 6.4e9),
                "extended": (None, 1.9e9)}
        latest = []
        for ex, (sp, pp) in vols.items():
            latest.append({"exchange": ex, "ts": now,
                           "volume_total": Decimal(str((sp or 0) + pp)),
                           "volume_spot": None if sp is None else Decimal(str(sp)),
                           "volume_perp": Decimal(str(pp))})
        series = []
        for d in range(days, -1, -1):
            ts = now - timedelta(days=d)
            wob = 4.0 * _u("cexshare", d)
            series.append({"ts": ts,
                           "cex_share_pct": Decimal(f"{55.0 + 6*math.sin(d/6) + wob:.2f}"),
                           "total": Decimal(str(60e9 * (1 + 0.2 * _u("totvol", d))))})
        return {"latest": latest, "series": series}



    def funding_series(self, exchange: str, symbol: str,
                       hours: int = 48, limit: int = 2000) -> list[Row]:
        fh = 8 if exchange in ("binance", "bybit") else 1
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        n = min(limit, hours // fh)
        seed = f"fund:{exchange}:{symbol}"
        out: list[Row] = []
        for k in range(n, 0, -1):
            ts = now - timedelta(hours=k * fh)
            i = int(ts.timestamp() // 3600)
            # mostly positive carry with occasional negative prints
            apr = 12.0 + 14.0 * math.sin(i / 40) + 9.0 * _u(seed, i)
            rate = Decimal(f"{apr / 100 / (8760 / fh):.10f}")
            out.append({"ts": ts, "rate": rate, "interval_hours": fh,
                        "apr_pct": Decimal(f"{apr:.4f}")})
        return out

    def funding_series_multi(
        self, symbols: list[str], hours: int = 48, limit: int = 5000
    ) -> list[Row]:
        wanted = set(symbols)
        out: list[Row] = []
        for venue, exchange, mt, pat, fh in _VENUES:
            if fh is None:
                continue                          # spot venues never fund
            for asset in _BASES:
                if venue == "binance_spot" and asset == "HYPE":
                    continue
                sym = pat.format(a=asset)
                if sym not in wanted:
                    continue
                for r in self.funding_series(exchange, sym, hours=hours, limit=limit):
                    out.append({"exchange": exchange, "symbol": sym, **r})
        return out

    def liquidity_series_multi(
        self, symbols: list[str], hours: int = 48,
        bucket_minutes: int | None = None, limit: int = 20_000,
    ) -> list[Row]:
        wanted = set(symbols)
        bucket_min = bucket_minutes or (5 if hours <= 48 else 30 if hours <= 168 else 120)
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        out: list[Row] = []
        oi_bases = {"BTC": 48_000, "ETH": 310_000, "SOL": 2_400_000,
                    "AAVE": 260_000, "HYPE": 4_100_000}
        for venue, exchange, mt, pat, fh in _VENUES:
            if fh is None:
                continue                          # spot venues carry no OI here
            for asset, base in _BASES.items():
                if venue == "binance_spot" and asset == "HYPE":
                    continue
                sym = pat.format(a=asset)
                if sym not in wanted:
                    continue
                oi_base = oi_bases.get(asset, 900_000_000 / base)
                seed = f"oi:{exchange}:{sym}"
                n = min(limit, (hours * 60) // bucket_min)
                for k in range(n, 0, -1):
                    ts = now - timedelta(minutes=k * bucket_min)
                    i = int(ts.timestamp() // 60)
                    oi = oi_base * (1 + 0.35 * math.sin(i / 500) + 0.15 * _u(seed, i))
                    mark = Decimal(f"{_px(f'{exchange}:perp:{sym}', base, i):.4f}")
                    out.append({"exchange": exchange, "symbol": sym, "ts": ts,
                                "oi_notional": Decimal(f"{oi:.2f}") * mark})
        return out

    # Only venues with a websocket adapter produce flow, so the mock mirrors
    # that rather than inventing it for all nine — and it keeps the page to the
    # three-colour venue palette that validates cleanly on the dark surface.
    _FLOW_VENUES = [
        ("hyperliquid", "hyperliquid", "{a}",      1),
        ("bybit_perp",  "bybit",       "{a}/USDT", 8),
        ("binance_perp", "binance",    "{a}/USDT", 8),
    ]

    def trade_flow_multi(
        self, symbols: list[str], hours: int = 6,
        bucket_minutes: int | None = None, limit: int = 20_000,
    ) -> list[Row]:
        """Synthetic order flow that actually shows the behaviour the page is
        for: takers flattening ahead of a funding settlement and re-entering
        after it, with size participating more heavily than usual.

        Flat noise would make the page look plausible while demonstrating
        nothing, so the generator models the dodge explicitly — direction is
        deterministic per (venue, asset) so a refresh never changes the story.
        """
        wanted = set(symbols)
        bucket_min = bucket_minutes or (1 if hours <= 6 else 5 if hours <= 48 else 60)
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        out: list[Row] = []

        for venue, exchange, pattern, fh in self._FLOW_VENUES:
            period = fh * 60                      # settlement cadence, minutes
            for asset, base in _BASES.items():
                sym = pattern.format(a=asset)
                if sym not in wanted:
                    continue
                seed = f"flow:{exchange}:{sym}"
                # which way this market dodges: deterministic, not random
                direction = 1 if _u(seed, 7) > 0 else -1
                liquidity = 1.0 if asset in ("BTC", "ETH") else 0.35

                n = min(limit, (hours * 60) // bucket_min)
                for k in range(n, 0, -1):
                    ts = now - timedelta(minutes=k * bucket_min)
                    i = int(ts.timestamp() // 60)
                    offset = i % period
                    to_next, since_last = period - offset, offset

                    # intensity ramps into the settlement and decays out of it
                    if to_next <= 12:
                        phase, intensity = "pre", (13 - to_next) / 13
                    elif since_last <= 6:
                        phase, intensity = "post", (7 - since_last) / 7
                    else:
                        phase, intensity = "flat", 0.0

                    burst = 1 + 3.2 * intensity
                    trades = max(1, int((40 + 220 * liquidity) * burst
                                        * (1 + 0.25 * _u(seed, i))))
                    px = _px(f"{exchange}:perp:{sym}", base, i)
                    volume = Decimal(f"{trades * (900 * liquidity / px) * (1 + 0.3 * _u(seed, i + 1)):.6f}")

                    skew = 0.06 * _u(seed, i + 2)
                    if phase == "pre":
                        skew += -direction * 0.72 * intensity
                    elif phase == "post":
                        skew += direction * 0.61 * intensity
                    skew = max(-0.95, min(0.95, skew))
                    buy = volume * Decimal(str((1 + skew) / 2))
                    sell = volume - buy

                    # size shows up disproportionately around the settlement
                    large_share = 0.10 + 0.34 * intensity
                    large_n = int(trades * large_share * 0.22)
                    large_vol = volume * Decimal(str(large_share))
                    large_skew = max(-0.95, min(0.95, skew * 1.25))
                    large_buy = large_vol * Decimal(str((1 + large_skew) / 2))
                    large_sell = large_vol - large_buy

                    notional = volume * Decimal(f"{px:.4f}")
                    max_trade = Decimal(f"{(9_000 + 145_000 * intensity) * (1 + 0.4 * _u(seed, i + 3)):.2f}")
                    total_large = large_buy + large_sell
                    out.append({
                        "exchange": exchange, "symbol": sym, "ts": ts,
                        "trades": trades, "volume": volume,
                        "buy_volume": buy, "sell_volume": sell,
                        "notional": notional,
                        "large_trades": large_n,
                        "large_buy_volume": large_buy,
                        "large_sell_volume": large_sell,
                        "max_trade_notional": max_trade,
                        "imbalance_pct": Decimal(f"{skew * 100:.4f}"),
                        "large_imbalance_pct": (
                            (large_buy - large_sell) / total_large * 100
                            if total_large else None),
                    })
        return out

    def fills_pulse(self) -> list[Row]:
        now = datetime.now(UTC)
        addr = list(MOCK_FILLS)
        return [
            {"wallet_address": addr[0], "fills_24h": 4218, "buys": 2190, "sells": 2028,
             "last_ts": now - timedelta(seconds=18)},
            {"wallet_address": addr[1], "fills_24h": 11804, "buys": 5710, "sells": 6094,
             "last_ts": now - timedelta(seconds=6)},
        ]

    def ingest_freshness(self) -> Row | None:
        now = datetime.now(UTC)
        return {"last_ts": now - timedelta(seconds=70), "age_seconds": 70.0}

    def wallet_symbols(self, addresses: list[str]) -> list[Row]:
        return [{"symbol": s, "fills": f}
                for s, f in [("BTC", 8200), ("ETH", 5100), ("SOL", 2400)]]

    def wallet_flows(self, symbol: str, addresses: list[str], since) -> list[Row]:
        base = _BASES.get(symbol, 100.0)
        start = int(since.timestamp() // 300) * 300
        end = int(datetime.now(UTC).timestamp() // 300) * 300
        out: list[Row] = []
        for addr in addresses:
            for i, t in enumerate(range(start, end, 300)):
                if _u(f"{addr}:{symbol}:gap", i) > 0.6:
                    continue                     # quiet bucket — no row, as live
                out.append({
                    "wallet_address": addr,
                    "bucket": datetime.fromtimestamp(t, tz=UTC),
                    "net": Decimal(f"{250_000 / base * _u(f'{addr}:{symbol}:flow', i):.4f}"),
                })
        out.sort(key=lambda r: r["bucket"])
        return out

    def wallet_volume_24h(self, addresses: list[str]) -> list[Row]:
        return [{"wallet_address": a,
                 "notional": Decimal("184000000") if a.startswith("0x") else Decimal("96000000"),
                 "fills": 4218 if a.startswith("0x") else 11804}
                for a in addresses]

    def venue_volume_latest(self, exchanges: list[str]) -> dict[str, Any]:
        # same numbers as venue_volume()'s mock latest, keyed the way the real
        # query returns them
        vols = {"binance": 35.7e9, "bybit": 13.0e9,
                "hyperliquid": 10.2e9, "lighter": 6.55e9, "extended": 1.9e9}
        return {ex: Decimal(str(vols[ex])) for ex in exchanges if ex in vols}

    def fills_summary(self, wallet_address: str) -> list[Row]:
        now = datetime.now(UTC)
        out = []
        for i, (sym, mt) in enumerate([("BTC", "perp"), ("ETH", "perp"), ("SOL", "perp")]):
            for side in ("buy", "sell"):
                out.append({
                    "symbol": sym, "market_type": mt, "side": side,
                    "fills": 400 - i * 90 + (25 if side == "buy" else 0),
                    "total_amount": Decimal(f"{120.5 - i * 30:.2f}"),
                    "first_ts": now - timedelta(days=2), "last_ts": now,
                })
        return out